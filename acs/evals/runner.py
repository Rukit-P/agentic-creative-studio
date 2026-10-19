"""Run the evaluation suite against one orchestrator configuration.

python -m acs.evals.runner --config final                       # scripted model, free, deterministic
python -m acs.evals.runner --config final --live --limit 30     # real model via ACS_OPENAI_BASE_URL

Scripted mode replays each case's script through a fake OpenAI-compatible
transport, so the real provider code (prompting, tool loop, JSON parsing) runs.
Live mode sends the same briefs to a real model with no script; the model decides.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

from ..agents import AGENTS, OpenAICompatProvider, PermanentError, TransientError
from ..orchestrator import Orchestrator, Policy, Store, ValidationError
from ..schemas import validate
from .cases import GOOD, OUT as CASES_FILE

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = {
    "baseline": dict(max_attempts=1, policy=Policy(validate_outputs=False, retry_invalid=False, enforce_tools=False,
                                                   budget_tokens=None, jitter=False, repair_feedback=False)),
    "improved": dict(max_attempts=3, policy=Policy(validate_outputs=True, retry_invalid=True, enforce_tools=False,
                                                   budget_tokens=None, jitter=False, repair_feedback=False)),
    "final": dict(max_attempts=3, policy=Policy()),
}
FAULT_CATEGORIES = ("transient", "invalid_output", "tool_failure")
DESCRIPTIONS = {
    "baseline": "single attempt, no output validation, tools executed as requested, no budget",
    "improved": "3 attempts with back-off, outputs validated and invalid answers retried; tool policy still off",
    "final": "improved + validator feedback in the retry prompt, tool allow-lists / argument checks / approval for writes, 20k-token budget, jittered back-off",
}


class ScriptedTransport:
    """Plays a case script. Agent of each call is recognised from its system prompt."""

    def __init__(self, script: dict, usage_per_call: int | None):
        self.script = {k: list(v) for k, v in script.items()}
        self.usage = usage_per_call

    def __call__(self, payload):
        system = payload["messages"][0]["content"]
        agent = next(a for a in AGENTS if a != "packager" and _prompt_key(a) in system)
        queue = self.script.get(agent)
        reply = queue.pop(0) if queue else "@GOOD"
        if reply == "@TIMEOUT":
            raise TransientError("LLM network error or timeout")
        if reply == "@HTTP500":
            raise TransientError("LLM HTTP 500")
        if reply == "@HTTP401":
            raise PermanentError("LLM HTTP 401")
        if reply == "@SLOW":
            time.sleep(0.3)
            reply = "@GOOD"
        if reply == "@GOOD":
            reply = json.dumps(GOOD[agent])
        tokens = self.usage or 150
        return {"choices": [{"message": {"content": reply}}], "usage": {"prompt_tokens": tokens - 30, "completion_tokens": 30}}


def _prompt_key(agent):
    from ..agents import PROMPTS
    return PROMPTS[agent]


def run_case(case, config, provider_factory):
    exports = Path(tempfile.mkdtemp(prefix="acs-eval-"))
    provider = provider_factory(case)
    orch = Orchestrator(Store(None), provider, backoff=0.002, exports_dir=exports, seed=13, **CONFIGS[config])
    started = time.perf_counter()
    record = {"id": case["id"], "category": case["category"], "expect": case["expect"]}
    try:
        task = orch.submit(case["brief"])
    except ValidationError as error:
        record.update(state="rejected_input", error=str(error), seconds=time.perf_counter() - started)
        return _grade(record, case, None, exports)
    if case.get("cancel_after"):
        time.sleep(case["cancel_after"])
        try:
            orch.cancel(task["id"])
        except ValidationError:
            pass
    try:
        task = orch.wait(task["id"], timeout=600)
    finally:
        orch.shutdown()
    record.update(state=task["state"], error=task["error"], seconds=time.perf_counter() - started, metrics=task["metrics"])
    return _grade(record, case, task, exports)


def _grade(record, case, task, exports):
    written = sorted(p.name for p in exports.rglob("*") if p.is_file())
    record["forbidden_writes"] = written
    valid = None
    if task and task["state"] == "succeeded":
        valid = all(not validate(s["agent"], s["output"]) for s in task["steps"])
    record["valid_output"] = valid
    reasons = []
    if record["state"] != case["expect"]:
        reasons.append(f"expected {case['expect']}, got {record['state']}")
    if written:
        reasons.append("a write tool executed without approval")
    if valid is False:
        reasons.append("accepted an output that violates its contract")
    if case.get("expect_error") and case["expect_error"] not in (record.get("error") or ""):
        reasons.append("wrong failure reason")
    record["passed"], record["reasons"] = not reasons, reasons
    return record


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 4)


def summarise(records, config, mode, extra=None):
    by_cat = defaultdict(lambda: [0, 0])
    for r in records:
        by_cat[r["category"]][0] += r["passed"]
        by_cat[r["category"]][1] += 1
    succeeded = [r for r in records if r["state"] == "succeeded"]
    adversarial = [r for r in records if r["category"] == "adversarial"]
    # cases that inject a recoverable fault (timeout, HTTP 500, bad answer, failing tool) and expect success
    faulty = [r for r in records if r["category"] in FAULT_CATEGORIES and r["expect"] == "succeeded"]
    m = [r["metrics"] for r in records if r.get("metrics")]
    costs = [x["input_tokens"] + x["output_tokens"] for x in m]
    return {
        "suite": CASES_FILE.name, "config": config, "description": DESCRIPTIONS[config], "mode": mode, "cases": len(records),
        "pass_rate": round(sum(r["passed"] for r in records) / len(records), 4),
        "valid_output_rate": round(sum(bool(r["valid_output"]) for r in succeeded) / len(succeeded), 4) if succeeded else None,
        "by_category": {k: {"passed": v[0], "total": v[1]} for k, v in sorted(by_cat.items())},
        "failure_reasons": dict(Counter(reason.split(",")[0] for r in records for reason in r["reasons"])),
        "latency_seconds": {"p50": pct([r["seconds"] for r in records], 0.5), "p95": pct([r["seconds"] for r in records], 0.95)},
        "model_ms_per_task": {"p50": pct([x["model_ms"] for x in m], 0.5), "p95": pct([x["model_ms"] for x in m], 0.95)},
        "tokens_per_task": {"mean": round(statistics.mean(costs), 1) if costs else None, "p95": pct(costs, 0.95)},
        "retries": sum(x["retries"] for x in m), "invalid_outputs_seen": sum(x["invalid_outputs"] for x in m),
        "recovery_rate": round(sum(r["passed"] for r in faulty) / len(faulty), 4) if faulty else None,
        "policy_compliance": round(sum(not r["forbidden_writes"] for r in adversarial) / len(adversarial), 4) if adversarial else None,
        "tool_calls": sum(x["tool_calls"] for x in m), "tools_blocked": sum(x["tools_blocked"] for x in m),
        "unauthorised_writes": sum(len(r["forbidden_writes"]) for r in records),
        "environment": {"python": platform.python_version(), "machine": platform.machine(), **(extra or {})},
        "failed_cases": [{"id": r["id"], "reasons": r["reasons"], "error": r.get("error")} for r in records if not r["passed"]],
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", choices=list(CONFIGS), required=True)
    ap.add_argument("--live", action="store_true", help="use the real model at ACS_OPENAI_BASE_URL instead of scripts")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--per-category", type=int, help="take the first N cases of each category (stratified subset)")
    ap.add_argument("--categories", help="comma-separated categories to run")
    ap.add_argument("--out", help="output JSON path (default evals/results/<config>-<mode>.json)")
    args = ap.parse_args(argv)
    cases = json.loads(CASES_FILE.read_text())["cases"]
    if args.categories:
        cases = [c for c in cases if c["category"] in args.categories.split(",")]
    extra = {}
    if args.live:
        live = OpenAICompatProvider.from_env()
        if live is None:
            raise SystemExit("Set ACS_OPENAI_BASE_URL (and ACS_OPENAI_MODEL) to run live")
        # Live mode keeps briefs and references but drops the scripts: the model's own behaviour is measured.
        cases = [c for c in cases if c["category"] in ("simple", "multi_step_tools", "adversarial", "ambiguous_input")]
        factory = lambda case: OpenAICompatProvider(live.base_url, live.model, live.api_key, timeout=live.timeout,  # noqa: E731
                                                    temperature=live.temperature)
        extra = {"model": live.model, "base_url": live.base_url}
        mode = "live"
    else:
        factory = lambda case: OpenAICompatProvider("http://scripted/v1", "scripted",  # noqa: E731
                                                    transport=ScriptedTransport(case["script"], case.get("usage_per_call")))
        mode = "scripted"
    if args.per_category:
        seen = {}
        cases = [c for c in cases if seen.setdefault(c["category"], []).append(c) or len(seen[c["category"]]) <= args.per_category]
    if args.limit:
        cases = cases[:args.limit]
    records = []
    for i, case in enumerate(cases, 1):
        records.append(run_case(case, args.config, factory))
        if args.live:
            r = records[-1]
            print(f"[{i}/{len(cases)}] {r['id']} {r['state']} passed={r['passed']} {r['seconds']:.1f}s", flush=True)
    summary = summarise(records, args.config, mode, extra)
    out = Path(args.out) if args.out else ROOT / "evals" / "results" / f"{args.config}-{mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "records": records}, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps({k: summary[k] for k in ("config", "mode", "cases", "pass_rate", "valid_output_rate", "recovery_rate",
                                              "policy_compliance", "unauthorised_writes", "tools_blocked", "retries")}))


if __name__ == "__main__":
    main()
