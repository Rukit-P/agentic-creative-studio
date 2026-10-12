import tempfile
import threading
import time
import unittest
from pathlib import Path

from acs.agents import AGENTS, DemoProvider, PermanentError, TransientError
from acs.orchestrator import Orchestrator, Store, ValidationError

BRIEF = {"title": "Lumen Coffee", "text": "A small roastery launching a subscription for office teams.", "tone": "warm"}


def make(provider=None, path=None, **kw):
    kw.setdefault("backoff", 0.001)
    return Orchestrator(Store(path), provider or DemoProvider(), **kw)


class Scripted:
    """Provider whose behaviour per agent is a list of outcomes consumed call by call."""
    name = "demo"

    def __init__(self, script):
        self.script, self.calls = script, []

    def run(self, agent, brief, context):
        self.calls.append(agent)
        outcomes = self.script.get(agent, [])
        outcome = outcomes.pop(0) if outcomes else "ok"
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "sleep":
            time.sleep(0.25)
        return {"agent": agent, "seen": sorted(context)}


class HappyPath(unittest.TestCase):
    def test_demo_run_succeeds_in_order_and_is_deterministic(self):
        orch = make()
        a = orch.wait(orch.submit(BRIEF)["id"])
        b = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(a["state"], "succeeded")
        self.assertEqual([s["agent"] for s in a["steps"]], list(AGENTS))
        self.assertTrue(all(s["state"] == "succeeded" and s["attempts"] == 1 for s in a["steps"]))
        self.assertEqual(a["result"], b["result"])
        self.assertEqual(a["result"]["score"], 100)
        starts = [e["step"] for e in a["events"] if e["type"] == "step.started"]
        self.assertEqual(starts, list(AGENTS))

    def test_each_agent_sees_only_previous_outputs(self):
        provider = Scripted({})
        orch = make(provider)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["steps"][0]["output"]["seen"], [])
        self.assertEqual(task["steps"][2]["output"]["seen"], ["copywriter", "planner"])


class Retries(unittest.TestCase):
    def test_transient_error_is_retried_then_succeeds(self):
        provider = Scripted({"copywriter": [TransientError("rate limit"), TransientError("rate limit")]})
        orch = make(provider, max_attempts=3)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(task["steps"][1]["attempts"], 3)
        self.assertEqual(provider.calls.count("copywriter"), 3)

    def test_transient_error_exhausts_attempts(self):
        provider = Scripted({"copywriter": [TransientError("x")] * 5})
        orch = make(provider, max_attempts=2)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(provider.calls.count("copywriter"), 2)
        self.assertEqual(task["steps"][2]["state"], "pending")      # later steps never ran

    def test_permanent_error_is_not_retried(self):
        provider = Scripted({"planner": [PermanentError("bad input")]})
        orch = make(provider, max_attempts=3)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["state"], "failed")
        self.assertEqual(provider.calls.count("planner"), 1)
        self.assertIn("bad input", task["error"])

    def test_unexpected_exception_fails_the_task_not_the_worker(self):
        provider = Scripted({"planner": [RuntimeError("boom with secret-token")]})
        orch = make(provider)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["state"], "failed")
        self.assertNotIn("secret-token", task["error"])
        self.assertEqual(orch.wait(orch.submit(BRIEF)["id"])["state"], "succeeded")   # worker pool still alive

    def test_manual_retry_keeps_completed_steps(self):
        provider = Scripted({"critic": [PermanentError("nope")]})
        orch = make(provider)
        task = orch.wait(orch.submit(BRIEF)["id"])
        self.assertEqual(task["state"], "failed")
        before = list(provider.calls)
        orch.retry(task["id"])
        task = orch.wait(task["id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(provider.calls[len(before):], ["critic", "packager"])
        self.assertEqual(task["runs"], 2)

    def test_retry_rejected_for_running_or_succeeded(self):
        orch = make()
        task = orch.wait(orch.submit(BRIEF)["id"])
        with self.assertRaises(ValidationError):
            orch.retry(task["id"])


class ChaosModes(unittest.TestCase):
    def test_flaky_mode_shows_retries_then_completes(self):
        orch = make()
        task = orch.wait(orch.submit(BRIEF, chaos="flaky")["id"])
        self.assertEqual(task["state"], "succeeded")
        self.assertEqual(task["steps"][2]["attempts"], 3)
        self.assertEqual(len([e for e in task["events"] if e["type"] == "step.failed"]), 2)

    def test_broken_mode_fails_first_run_and_recovers_on_retry(self):
        orch = make()
        task = orch.wait(orch.submit(BRIEF, chaos="broken")["id"])
        self.assertEqual((task["state"], task["steps"][3]["state"]), ("failed", "failed"))
        orch.retry(task["id"])
        self.assertEqual(orch.wait(task["id"])["state"], "succeeded")


class Cancellation(unittest.TestCase):
    def test_cancel_during_run_stops_before_next_step(self):
        provider = Scripted({"planner": ["sleep"]})
        orch = make(provider)
        task = orch.submit(BRIEF)
        time.sleep(0.05)
        orch.cancel(task["id"])
        task = orch.wait(task["id"])
        self.assertEqual(task["state"], "cancelled")
        self.assertNotIn("copywriter", provider.calls)

    def test_cancel_interrupts_backoff_wait(self):
        provider = Scripted({"planner": [TransientError("x")] * 5})
        orch = make(provider, backoff=30)
        task = orch.submit(BRIEF)
        time.sleep(0.1)
        started = time.monotonic()
        orch.cancel(task["id"])
        self.assertEqual(orch.wait(task["id"], 5)["state"], "cancelled")
        self.assertLess(time.monotonic() - started, 2)

    def test_cancelled_task_can_be_retried(self):
        provider = Scripted({"planner": ["sleep"]})
        orch = make(provider)
        task = orch.submit(BRIEF)
        time.sleep(0.05)
        orch.cancel(task["id"])
        orch.wait(task["id"])
        orch.retry(task["id"])
        self.assertEqual(orch.wait(task["id"])["state"], "succeeded")

    def test_cancel_finished_task_is_rejected(self):
        orch = make()
        task = orch.wait(orch.submit(BRIEF)["id"])
        with self.assertRaises(ValidationError):
            orch.cancel(task["id"])

    def test_queued_task_cancelled_before_start(self):
        gate = threading.Event()

        class Blocking(Scripted):
            def run(self, agent, brief, context):
                gate.wait(2)
                return super().run(agent, brief, context)

        orch = make(Blocking({}), workers=1)
        first = orch.submit(BRIEF)
        second = orch.submit(BRIEF)
        orch.cancel(second["id"])
        gate.set()
        self.assertEqual(orch.wait(first["id"])["state"], "succeeded")
        self.assertEqual(orch.wait(second["id"])["state"], "cancelled")


class Validation(unittest.TestCase):
    def test_rejects_bad_briefs(self):
        orch = make()
        for bad in (None, "x", {"title": "ab", "text": "long enough text"}, {"title": "Fine", "text": ""},
                    {"title": "t" * 81, "text": "long enough text"}, {"title": "Fine", "text": "x" * 2001},
                    {"title": "Fine", "text": "bad \x00 byte"}, {"title": 5, "text": "long enough text"}):
            with self.assertRaises(ValidationError, msg=repr(bad)[:40]):
                orch.submit(bad)
        with self.assertRaises(ValidationError):
            orch.submit(BRIEF, chaos="nuclear")


class Limits(unittest.TestCase):
    def test_active_task_cap_and_finished_pruning(self):
        import acs.orchestrator as mod
        gate = threading.Event()

        class Blocking(Scripted):
            def run(self, agent, brief, context):
                gate.wait(3)
                return super().run(agent, brief, context)

        old = mod.MAX_ACTIVE, mod.MAX_KEPT
        mod.MAX_ACTIVE, mod.MAX_KEPT = 3, 2
        try:
            orch = make(Blocking({}), workers=1)
            ids = [orch.submit(BRIEF)["id"] for _ in range(3)]
            with self.assertRaises(ValidationError):
                orch.submit(BRIEF)
            gate.set()
            for i in ids:
                orch.wait(i)
            orch.submit(BRIEF)
            self.assertLessEqual(len([t for t in orch.listing() if t["state"] in ("succeeded", "failed", "cancelled")]), 2)
        finally:
            mod.MAX_ACTIVE, mod.MAX_KEPT = old


class Persistence(unittest.TestCase):
    def test_tasks_survive_restart_and_interrupted_ones_are_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tasks.json"
            orch = make(path=path)
            done = orch.wait(orch.submit(BRIEF)["id"])
            store = Store(path)
            store.tasks[done["id"]]["state"] = "running"          # simulate a crash mid-run
            store.tasks[done["id"]]["steps"][4]["state"] = "running"
            store.save()
            again = make(path=path)
            task = again.snapshot(done["id"])
            self.assertEqual(task["state"], "failed")
            self.assertIn("restart", task["error"])
            again.retry(done["id"])
            self.assertEqual(again.wait(done["id"])["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
