import json
import tempfile
import unittest
from pathlib import Path

from acs.tools import ApprovalRequired, PolicyViolation, ToolError, ToolRunner, build_tools, contrast_ratio


class Contrast(unittest.TestCase):
    def test_known_wcag_values(self):
        self.assertEqual(contrast_ratio("#000000", "#FFFFFF")["ratio"], 21.0)
        self.assertEqual(contrast_ratio("#FFFFFF", "#FFFFFF")["ratio"], 1.0)
        self.assertEqual(contrast_ratio("#777777", "#FFFFFF")["ratio"], 4.48)   # the classic "just fails AA" grey
        self.assertFalse(contrast_ratio("#777777", "#FFFFFF")["aa_normal_text"])


class Policy(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.events = []
        self.runner = ToolRunner(build_tools("Client notes.", self.dir), on_event=lambda k, m, d: self.events.append(k))

    def test_allowed_read_tool_runs(self):
        self.assertEqual(self.runner.call("critic", "text_stats", {"text": "two words"})["words"], 2)
        self.assertEqual(self.events, ["tool.called"])

    def test_tool_outside_allow_list_is_blocked_and_logged(self):
        with self.assertRaises(PolicyViolation):
            self.runner.call("copywriter", "text_stats", {"text": "x"})
        with self.assertRaises(PolicyViolation):
            self.runner.call("critic", "save_package", {"filename": "a.json", "package": {}})
        self.assertEqual(self.events, ["tool.blocked", "tool.blocked"])
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_unknown_tool_and_bad_arguments_are_refused(self):
        for name, args in (("rm_rf", {}), (None, {}), ("contrast_ratio", {"foreground": "red", "background": "#000000"}),
                           ("contrast_ratio", {"foreground": "#000000"}), ("text_stats", {"text": "x", "shell": "ls"}),
                           ("text_stats", ["x"]), ("text_stats", {"text": "x" * 2001})):
            with self.assertRaises(PolicyViolation, msg=(name, args)):
                self.runner.call("critic", name, args)
        self.assertEqual(self.runner.executed, [])

    def test_write_tool_needs_approval_then_stays_in_its_folder(self):
        args = {"filename": "pkg.json", "package": {"a": 1}}
        with self.assertRaises(ApprovalRequired):
            self.runner.call("packager", "save_package", args)
        self.assertEqual(list(self.dir.iterdir()), [])
        self.runner.call("packager", "save_package", args, approved=True)
        self.assertEqual(json.loads((self.dir / "pkg.json").read_text()), {"a": 1})
        for bad in ("../escape.json", "/etc/x.json", "UPPER.json", "a.sh"):
            with self.assertRaises(PolicyViolation):
                self.runner.call("packager", "save_package", {"filename": bad, "package": {}}, approved=True)

    def test_reference_is_marked_untrusted_and_missing_reference_is_a_tool_error(self):
        self.assertEqual(self.runner.call("critic", "read_reference", {}), {"untrusted": True, "content": "Client notes."})
        with self.assertRaises(ToolError):
            ToolRunner(build_tools(None, None)).call("critic", "read_reference", {})

    def test_unenforced_runner_exists_only_to_measure_the_baseline(self):
        loose = ToolRunner(build_tools(None, self.dir), enforce=False)
        loose.call("critic", "save_package", {"filename": "x.json", "package": {}})
        self.assertEqual(loose.executed, ["save_package"])


if __name__ == "__main__":
    unittest.main()
