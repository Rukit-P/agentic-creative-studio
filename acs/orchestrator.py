"""Task state machine, retries, cancellation and an event log.

States of a task:  queued -> running -> succeeded | failed | cancelled
States of a step:  pending -> running -> succeeded | failed | cancelled
`retry` re-queues a failed or cancelled task and re-runs only the steps that did not succeed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .agents import AGENTS, PermanentError, TransientError

TERMINAL = {"succeeded", "failed", "cancelled"}
CHAOS_MODES = ("none", "flaky", "broken")
MAX_BRIEF_TEXT = 2000


class ValidationError(ValueError):
    pass


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
    def __init__(self, store: Store, provider, *, max_attempts: int = 3, backoff: float = 0.4, workers: int = 2):
        self.store, self.provider = store, provider
        self.max_attempts, self.backoff = max_attempts, backoff
        self.cond = threading.Condition(threading.RLock())
        self.cancel_flags: dict[str, threading.Event] = {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="acs-worker")

    # ---- public API -------------------------------------------------
    def submit(self, brief: dict, chaos: str = "none") -> dict:
        brief = self.validate(brief)
        if chaos not in CHAOS_MODES:
            raise ValidationError("chaos must be one of " + ", ".join(CHAOS_MODES))
        now = time.time()
        task = {"id": uuid.uuid4().hex[:12], "brief": brief, "chaos": chaos, "provider": self.provider.name,
                "state": "queued", "runs": 0, "created": now, "updated": now, "error": None, "result": None, "events": [],
                "steps": [{"agent": a, "state": "pending", "attempts": 0, "output": None, "error": None} for a in AGENTS]}
        with self.cond:
            self.store.tasks[task["id"]] = task
            self._event(task, "task.queued", "Task queued")
        self._enqueue(task["id"])
        return self.snapshot(task["id"])

    def cancel(self, task_id: str) -> dict:
        with self.cond:
            task = self._get(task_id)
            if task["state"] in TERMINAL:
                raise ValidationError(f"Task already {task['state']}")
            self.cancel_flags.setdefault(task_id, threading.Event()).set()
            self.cond.notify_all()
        return self.snapshot(task_id)

    def retry(self, task_id: str) -> dict:
        with self.cond:
            task = self._get(task_id)
            if task["state"] not in ("failed", "cancelled"):
                raise ValidationError("Only failed or cancelled tasks can be retried")
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
        return clean

    def _get(self, task_id: str) -> dict:
        task = self.store.tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _event(self, task: dict, kind: str, message: str, step: str | None = None):
        with self.cond:
            seq = (task["events"][-1]["seq"] + 1) if task["events"] else 1
            task["events"].append({"seq": seq, "time": time.time(), "type": kind, "step": step, "message": message})
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
            self._event(task, "task.started", f"Run {task['runs']} started")
        context = {s["agent"]: s["output"] for s in task["steps"] if s["state"] == "succeeded"}
        for step in task["steps"]:
            if step["state"] == "succeeded":
                continue
            if flag.is_set():
                return self._finish(task, "cancelled", "Cancelled by user", step)
            error = self._run_step(task, step, context, flag)
            if error == "cancelled":
                return self._finish(task, "cancelled", "Cancelled by user", step)
            if error:
                return self._finish(task, "failed", f"{step['agent']} failed: {error}", step)
            context[step["agent"]] = step["output"]
        with self.cond:
            task["result"] = task["steps"][-1]["output"]
        self._finish(task, "succeeded", "Task completed")

    def _run_step(self, task, step, context, flag):
        agent = step["agent"]
        for attempt in range(1, self.max_attempts + 1):
            with self.cond:
                step.update(state="running", attempts=attempt, error=None)
                self._event(task, "step.started", f"{agent} started (attempt {attempt})", agent)
            try:
                self._fault(task, agent, attempt)
                output = self.provider.run(agent, task["brief"], context)
                with self.cond:
                    step.update(state="succeeded", output=output)
                    self._event(task, "step.succeeded", f"{agent} finished", agent)
                return None
            except TransientError as error:
                message, retry = str(error), attempt < self.max_attempts
            except PermanentError as error:
                message, retry = str(error), False
            except Exception as error:                      # a provider bug must not kill the worker silently
                message, retry = f"Unexpected {type(error).__name__}", False
            with self.cond:
                step.update(state="failed", error=message)
                self._event(task, "step.failed", f"{agent}: {message}" + (" - retrying" if retry else ""), agent)
            if not retry:
                return message
            if flag.wait(self.backoff * 2 ** (attempt - 1)):   # interruptible backoff
                return "cancelled"
        return "exhausted retries"

    def _finish(self, task, state, message, step=None):
        with self.cond:
            if state == "cancelled" and step is not None and step["state"] != "succeeded":
                step["state"] = "cancelled"
            task["state"] = state
            task["error"] = message if state != "succeeded" else None
            self._event(task, "task." + state, message)
