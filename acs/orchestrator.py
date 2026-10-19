"""Task state machine, retries, cancellation and an event log.

States of a task:  queued -> running -> [awaiting_approval ->] succeeded | failed | cancelled
States of a step:  pending -> running -> succeeded | failed | cancelled
`retry` re-queues a failed or cancelled task and re-runs only the steps that did not succeed.

`Policy` groups the reliability and safety switches. The defaults are what the server
runs; the evaluation suite (acs/evals) turns them off one by one to measure each.
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .agents import AGENTS, InvalidOutputError, PermanentError, TransientError
from .schemas import validate as validate_output
from .tools import ApprovalRequired, PolicyViolation, ToolError, ToolRunner, build_tools

TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"queued", "running", "awaiting_approval"}
CHAOS_MODES = ("none", "flaky", "broken")
MAX_BRIEF_TEXT = 2000
MAX_REFERENCE = 4000
MAX_ACTIVE = 50            # queued + running tasks accepted at once
MAX_KEPT = 200             # finished tasks kept in memory and on disk


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    validate_outputs: bool = True       # check every agent output against acs/schemas.py
    retry_invalid: bool = True          # treat an invalid model answer as retryable (bounded by max_attempts)
    enforce_tools: bool = True          # allow-lists, argument validation and approval for write tools
    budget_tokens: int | None = 20000   # per task, across runs; None disables
    jitter: bool = True                 # randomise back-off (0.5x-1.5x) so retries do not synchronise


class AgentTools:
    """The callable handed to tool-using providers. It is bound to one agent and one task."""

    def __init__(self, runner: ToolRunner, agent: str):
        self.runner, self.agent = runner, agent
        self.specs = runner.specs_for(agent) if runner.enforce else [t.spec() for t in runner.tools.values()]

    def __call__(self, name, arguments):
        return self.runner.call(self.agent, name, arguments)

    def safe_call(self, name, arguments) -> dict:
        """For model-driven calls: policy and tool errors become a result the model can read, never an execution."""
        try:
            return {"ok": True, "result": self.runner.call(self.agent, name, arguments)}
        except PolicyViolation as error:
            return {"ok": False, "error": "refused by policy: " + str(error)}
        except ApprovalRequired:
            return {"ok": False, "error": "refused: write tools need human approval and are not available to this agent"}
        except ToolError as error:
            return {"ok": False, "error": str(error)}


class Store:
    """JSON file with atomic writes.  Tasks that were running when the process died are marked failed."""

    def __init__(self, path: str | os.PathLike | None):
        self.path, self.lock = (Path(path) if path else None), threading.RLock()
        self.tasks: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self.tasks = {t["id"]: t for t in json.loads(self.path.read_text("utf-8"))}
            except (OSError, ValueError, KeyError):
                self.tasks = {}
            for task in self.tasks.values():
                if task["state"] in ("queued", "running"):
                    task["state"], task["error"] = "failed", "Interrupted by a server restart. Retry to continue."
                    for step in task["steps"]:
                        if step["state"] in ("running", "pending"):
                            step["state"] = "failed" if step["state"] == "running" else "pending"

    def save(self):
        if not self.path:
            return
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = sorted(self.tasks.values(), key=lambda t: t["created"])[-200:]
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tasks-")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp, self.path)


class Orchestrator:
    def __init__(self, store: Store, provider, *, max_attempts: int = 3, backoff: float = 0.4, workers: int = 2,
                 policy: Policy | None = None, exports_dir: str | os.PathLike | None = None, seed: int | None = None):
        self.store, self.provider = store, provider
        self.max_attempts, self.backoff = max_attempts, backoff
        self.policy = policy or Policy()
        self.exports_dir = Path(exports_dir) if exports_dir else None
        self.rng = random.Random(seed)
        self.cond = threading.Condition(threading.RLock())
        self.cancel_flags: dict[str, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acs-worker")

    # ---- public API -------------------------------------------------
    def submit(self, brief: dict, chaos: str = "none") -> dict:
        brief = self.validate(brief)
        if chaos not in CHAOS_MODES:
            raise ValidationError("chaos must be one of " + ", ".join(CHAOS_MODES))
        with self.cond:
            if sum(t["state"] in ACTIVE for t in self.store.tasks.values()) >= MAX_ACTIVE:
                raise ValidationError("Too many active tasks; wait for some to finish")
        now = time.time()
        task = {"id": uuid.uuid4().hex[:12], "brief": brief, "chaos": chaos, "provider": self.provider.name,
                "state": "queued", "runs": 0, "created": now, "updated": now, "error": None, "result": None, "events": [],
                "run_id": None, "approval": None, "policy": asdict(self.policy),
                "metrics": {"attempts": 0, "retries": 0, "input_tokens": 0, "output_tokens": 0, "model_calls": 0,
                            "model_ms": 0.0, "tool_calls": 0, "tools_blocked": 0, "invalid_outputs": 0},
                "steps": [{"agent": a, "state": "pending", "attempts": 0, "output": None, "error": None} for a in AGENTS]}
        with self.cond:
            self.store.tasks[task["id"]] = task
            finished = sorted((t for t in self.store.tasks.values() if t["state"] in TERMINAL), key=lambda t: t["created"])
            for old in finished[:max(0, len(finished) - MAX_KEPT)]:
                self.store.tasks.pop(old["id"], None)
                self.cancel_flags.pop(old["id"], None)
            self._event(task, "task.queued", "Task queued")
        self._enqueue(task["id"])
        return self.snapshot(task["id"])

    def cancel(self, task_id: str) -> dict:
        with self.cond:
            task = self._get(task_id)
            if task["state"] in TERMINAL:
                raise ValidationError(f"Task already {task['state']}")
            self.cancel_flags.setdefault(task_id, threading.Event()).set()
            if task["state"] == "awaiting_approval":
                task["approval"]["status"] = "cancelled"
                self._finish(task, "cancelled", "Cancelled while waiting for approval")
            self.cond.notify_all()
        return self.snapshot(task_id)

    def decide(self, task_id: str, approve: bool, reviewer: str = "local user") -> dict:
        """Human decision on a pending write-tool call. Approval executes exactly the call that was shown."""
        with self.cond:
            task = self._get(task_id)
            if task["state"] != "awaiting_approval":
                raise ValidationError("This task is not waiting for approval")
            request = task["approval"]
            request.update(status="approved" if approve else "rejected", decided=time.time(), reviewer=reviewer)
            self._event(task, "approval." + request["status"], f"{request['tool']} {request['status']} by {reviewer}")
            if not approve:
                task["result"]["export"] = {"status": "rejected"}
                return self._finish(task, "succeeded", "Completed; export rejected by reviewer") or self.snapshot(task_id)
            runner = self._tool_runner(task)
            try:
                saved = runner.call("packager", request["tool"], request["arguments"], approved=True)
                task["result"]["export"] = {"status": "saved", **saved}
                self._finish(task, "succeeded", "Completed and exported after approval")
            except (ToolError, PolicyViolation) as error:
                task["result"]["export"] = {"status": "error", "error": str(error)}
                self._finish(task, "failed", f"Export failed: {error}")
        return self.snapshot(task_id)

    def retry(self, task_id: str) -> dict:
        with self.cond:
            task = self._get(task_id)
            if task["state"] not in ("failed", "cancelled"):
                raise ValidationError("Only failed or cancelled tasks can be retried")
            if task.get("approval") and task["approval"].get("status") == "cancelled":
                task["approval"] = None
            for step in task["steps"]:
                if step["state"] != "succeeded":
                    step.update(state="pending", error=None, attempts=0)
            task.update(state="queued", error=None, result=None)
            self.cancel_flags.pop(task_id, None)
            self._event(task, "task.retry", "Retry requested; completed steps are kept")
        self._enqueue(task_id)
        return self.snapshot(task_id)

    def snapshot(self, task_id: str) -> dict:
        with self.cond:
            return json.loads(json.dumps(self._get(task_id)))

    def listing(self) -> list[dict]:
        with self.cond:
            rows = sorted(self.store.tasks.values(), key=lambda t: t["created"], reverse=True)
            return [{"id": t["id"], "title": t["brief"]["title"], "state": t["state"], "chaos": t["chaos"], "provider": t["provider"],
                     "created": t["created"], "updated": t["updated"],
                     "progress": sum(s["state"] == "succeeded" for s in t["steps"]), "total": len(t["steps"])} for t in rows]

    def metrics(self) -> dict:
        """Aggregate counters over the tasks kept in memory (for /api/metrics)."""
        with self.cond:
            tasks = list(self.store.tasks.values())
            states = {}
            for t in tasks:
                states[t["state"]] = states.get(t["state"], 0) + 1
            keys = ("attempts", "retries", "input_tokens", "output_tokens", "model_calls", "tool_calls", "tools_blocked", "invalid_outputs")
            totals = {k: sum(t.get("metrics", {}).get(k, 0) for t in tasks) for k in keys}
            return {"tasks": len(tasks), "states": states, "totals": totals, "provider": self.provider.name, "policy": asdict(self.policy)}

    def events_after(self, task_id: str, after: int, timeout: float = 15.0) -> tuple[list[dict], bool]:
        """Block up to `timeout` for events with seq > after.  Returns (events, task_is_terminal)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                task = self._get(task_id)
                fresh = [e for e in task["events"] if e["seq"] > after]
                if fresh or task["state"] in TERMINAL:
                    return json.loads(json.dumps(fresh)), task["state"] in TERMINAL
                left = deadline - time.monotonic()
                if left <= 0:
                    return [], False
                self.cond.wait(left)

    def wait(self, task_id: str, timeout: float = 30.0) -> dict:
        """Test helper: block until the task is terminal."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self._get(task_id)["state"] not in TERMINAL:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(task_id)
                self.cond.wait(left)
        return self.snapshot(task_id)

    def shutdown(self):
        for flag in self.cancel_flags.values():
            flag.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---- internals --------------------------------------------------
    @staticmethod
    def validate(brief) -> dict:
        if not isinstance(brief, dict):
            raise ValidationError("brief must be an object")
        clean = {}
        for key, limit, required in (("title", 80, True), ("text", MAX_BRIEF_TEXT, True), ("audience", 120, False), ("tone", 60, False)):
            value = brief.get(key, "")
            if not isinstance(value, str):
                raise ValidationError(f"{key} must be text")
            value = " ".join(value.split()) if key != "text" else value.strip()
            if required and len(value) < 3:
                raise ValidationError(f"{key} is required (at least 3 characters)")
            if len(value) > limit:
                raise ValidationError(f"{key} is longer than {limit} characters")
            if any(ord(c) < 32 and c not in "\n\t" for c in value):
                raise ValidationError(f"{key} contains control characters")
            clean[key] = value
        reference = brief.get("reference", "")
        if not isinstance(reference, str) or len(reference) > MAX_REFERENCE:
            raise ValidationError(f"reference must be text up to {MAX_REFERENCE} characters")
        if reference.strip():
            clean["reference"] = reference.strip()
        if brief.get("export") not in (None, False, True):
            raise ValidationError("export must be true or false")
        if brief.get("export"):
            clean["export"] = True
        return clean

    def _get(self, task_id: str) -> dict:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _event(self, task: dict, kind: str, message: str, step: str | None = None):
        with self.cond:
            seq = (task["events"][-1]["seq"] + 1) if task["events"] else 1
            task["events"].append({"seq": seq, "time": time.time(), "type": kind, "step": step, "message": message,
                                   "run": task.get("run_id")})
            task["updated"] = time.time()
            self.store.save()
            self.cond.notify_all()

    def _enqueue(self, task_id: str):
        self.cancel_flags.setdefault(task_id, threading.Event())
        self.pool.submit(self._run, task_id)

    def _fault(self, task: dict, agent: str, attempt: int):
        """Deterministic fault injection so retries and failures can be demonstrated and tested (demo provider only)."""
        if task["provider"] != "demo":
            return
        if task["chaos"] == "flaky" and agent == "art_director" and attempt < 3 and task["runs"] == 1:
            raise TransientError(f"Simulated tool timeout (attempt {attempt})")
        if task["chaos"] == "broken" and agent == "critic" and task["runs"] == 1:
            raise PermanentError("Simulated permanent failure in critic (first run only)")

    def _run(self, task_id: str):
        flag = self.cancel_flags[task_id]
        with self.cond:
            task = self._get(task_id)
            if flag.is_set():
                return self._finish(task, "cancelled", "Cancelled before start")
            task["state"], task["runs"] = "running", task["runs"] + 1
            task["run_id"] = f"{task_id}-r{task['runs']}"
            self._event(task, "task.started", f"Run {task['runs']} started")
        runner = self._tool_runner(task)
        context = {s["agent"]: s["output"] for s in task["steps"] if s["state"] == "succeeded"}
        for step in task["steps"]:
            if step["state"] == "succeeded":
                continue
            if flag.is_set():
                return self._finish(task, "cancelled", "Cancelled by user", step)
            error = self._run_step(task, step, context, flag, runner)
            if error == "cancelled":
                return self._finish(task, "cancelled", "Cancelled by user", step)
            if error:
                return self._finish(task, "failed", f"{step['agent']} failed: {error}", step)
            context[step["agent"]] = step["output"]
        with self.cond:
            task["result"] = task["steps"][-1]["output"]
            if task["brief"].get("export"):
                try:   # always goes through the policy; with enforcement on this asks for approval
                    saved = runner.call("packager", "save_package", {"filename": f"package-{task_id}.json", "package": task["result"]})
                    task["result"]["export"] = {"status": "saved", **saved}
                except ApprovalRequired as pending:
                    task["approval"] = {"tool": pending.name, "arguments": pending.arguments, "status": "pending",
                                        "requested": time.time(), "summary": f"Write {pending.arguments['filename']} to the exports folder"}
                    task["state"] = "awaiting_approval"
                    self._event(task, "approval.requested", f"{pending.name} waits for a human decision", "packager")
                    return None
                except (ToolError, PolicyViolation) as error:
                    task["result"]["export"] = {"status": "error", "error": str(error)}
        self._finish(task, "succeeded", "Task completed")

    def _tool_runner(self, task) -> ToolRunner:
        def on_event(kind, message, data):
            with self.cond:
                task["metrics"]["tool_calls" if kind == "tool.called" else "tools_blocked"] += 1
                self._event(task, kind, message, data.get("agent"))
        exports = self.exports_dir
        return ToolRunner(build_tools(task["brief"].get("reference"), exports), enforce=self.policy.enforce_tools, on_event=on_event)

    def _delay(self, attempt: int) -> float:
        base = self.backoff * 2 ** (attempt - 1)
        return base * (0.5 + self.rng.random()) if self.policy.jitter else base

    def _call_provider(self, task, agent, context, runner):
        if getattr(self.provider, "uses_tools", False):
            return self.provider.run(agent, task["brief"], context, tools=AgentTools(runner, agent))
        return self.provider.run(agent, task["brief"], context)

    def _account(self, task, attempt):
        meta = self.provider.last_meta() if hasattr(self.provider, "last_meta") else {}
        with self.cond:
            m = task["metrics"]
            m["attempts"] += 1
            m["retries"] += attempt > 1
            for key in ("input_tokens", "output_tokens", "model_calls"):
                m[key] += int(meta.get(key, 0))
            m["model_ms"] = round(m["model_ms"] + float(meta.get("model_ms", 0.0)), 1)
            used = m["input_tokens"] + m["output_tokens"]
        return meta, used

    def _run_step(self, task, step, context, flag, runner):
        agent = step["agent"]
        for attempt in range(1, self.max_attempts + 1):
            with self.cond:
                step.update(state="running", attempts=attempt, error=None)
                self._event(task, "step.started", f"{agent} started (attempt {attempt})", agent)
            started = time.perf_counter()
            try:
                self._fault(task, agent, attempt)
                try:
                    output = self._call_provider(task, agent, context, runner)
                finally:
                    meta, used = self._account(task, attempt)
                problems = validate_output(agent, output) if self.policy.validate_outputs else []
                if problems:
                    raise InvalidOutputError("; ".join(problems)[:300])
                budget = self.policy.budget_tokens
                if budget is not None and used > budget:
                    raise PermanentError(f"Token budget exceeded ({used} > {budget})")
                with self.cond:
                    step.update(state="succeeded", output=output, ms=round((time.perf_counter() - started) * 1000, 1),
                                tokens=meta.get("input_tokens", 0) + meta.get("output_tokens", 0))
                    self._event(task, "step.succeeded", f"{agent} finished", agent)
                return None
            except TransientError as error:
                message, retry = str(error), attempt < self.max_attempts
            except InvalidOutputError as error:
                with self.cond:
                    task["metrics"]["invalid_outputs"] += 1
                message, retry = f"Invalid output: {error}", self.policy.retry_invalid and attempt < self.max_attempts
            except PermanentError as error:
                message, retry = str(error), False
            except Exception as error:                      # a provider bug must not kill the worker silently
                message, retry = f"Unexpected {type(error).__name__}", False
            with self.cond:
                step.update(state="failed", error=message)
                self._event(task, "step.failed", f"{agent}: {message}" + (" - retrying" if retry else ""), agent)
            if not retry:
                return message
            if flag.wait(self._delay(attempt)):   # interruptible backoff
                return "cancelled"
        return "exhausted retries"

    def _finish(self, task, state, message, step=None):
        with self.cond:
            if state == "cancelled" and step is not None and step["state"] != "succeeded":
                step["state"] = "cancelled"
            task["state"] = state
            task["error"] = message if state != "succeeded" else None
            self._event(task, "task." + state, message)
