import json
import unittest

from acs.agents import AGENTS, DemoProvider, LLMProvider, PermanentError, TransientError

BRIEF = {"title": "Lumen Coffee", "text": "A small roastery launching a subscription.", "tone": "warm"}


def pipeline(provider):
    ctx = {}
    for agent in AGENTS:
        ctx[agent] = provider.run(agent, BRIEF, ctx)
    return ctx


class Demo(unittest.TestCase):
    def test_deterministic_and_brief_dependent(self):
        self.assertEqual(pipeline(DemoProvider()), pipeline(DemoProvider()))
        other = dict(BRIEF, title="Atlas Bikes")
        self.assertNotEqual(DemoProvider().run("copywriter", other, {"planner": pipeline(DemoProvider())["planner"]}),
                            pipeline(DemoProvider())["copywriter"])

    def test_package_shape(self):
        pkg = pipeline(DemoProvider())["packager"]
        self.assertEqual(len(pkg["palette"]["colours"]), 4)
        self.assertTrue(all(len(c) == 7 and c.startswith("#") for c in pkg["palette"]["colours"]))
        self.assertGreaterEqual(len(pkg["shot_list"]), 4)
        self.assertEqual(len(set(pkg["shot_list"])), len(pkg["shot_list"]))
        for title in ("Atlas Bikes", "Nova Tea", "Orbit Fitness", "x" * 9):
            ctx = {"planner": {"angle": "a"}}
            shots = DemoProvider().run("art_director", dict(BRIEF, title=title), ctx)["shot_list"]
            self.assertEqual(len(set(shots)), 4, title)
        json.dumps(pkg)


class LLM(unittest.TestCase):
    def test_from_env_requires_key(self):
        self.assertIsNone(LLMProvider.from_env({}))
        self.assertEqual(LLMProvider.from_env({"ACS_LLM_API_KEY": "k"}).model, "claude-sonnet-5-5")

    def test_request_shape_and_json_extraction(self):
        seen = {}

        def transport(payload):
            seen.update(payload)
            return {"content": [{"type": "text", "text": 'Here you go: {"objective": "o", "angle": "a", "pillars": [], "deliverables": []}'}]}

        out = LLMProvider("secret", "m", transport).run("planner", BRIEF, {})
        self.assertEqual(out["angle"], "a")
        self.assertEqual(seen["model"], "m")
        self.assertIn("untrusted", seen["system"])
        self.assertNotIn("secret", json.dumps(seen))

    def test_invalid_json_is_permanent(self):
        provider = LLMProvider("k", "m", lambda p: {"content": [{"type": "text", "text": "no json"}]})
        with self.assertRaises(PermanentError):
            provider.run("planner", BRIEF, {})

    def test_http_errors_map_to_retry_policy(self):
        import io
        import urllib.error
        import urllib.request
        from unittest import mock

        def failing(code):
            return urllib.error.HTTPError("u", code, "m", {}, io.BytesIO(b""))

        provider = LLMProvider("k", "m")
        with mock.patch.object(urllib.request, "urlopen", side_effect=failing(429)):
            with self.assertRaises(TransientError):
                provider.run("planner", BRIEF, {})
        with mock.patch.object(urllib.request, "urlopen", side_effect=failing(401)):
            with self.assertRaises(PermanentError):
                provider.run("planner", BRIEF, {})
        with mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError()):
            with self.assertRaises(TransientError):
                provider.run("planner", BRIEF, {})

    def test_packager_needs_no_model_call(self):
        def boom(_):
            raise AssertionError("no call expected")
        ctx = pipeline(DemoProvider())
        del ctx["packager"]
        self.assertIn("palette", LLMProvider("k", "m", boom).run("packager", BRIEF, ctx))


if __name__ == "__main__":
    unittest.main()
