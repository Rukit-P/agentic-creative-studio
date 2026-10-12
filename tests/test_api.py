import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from acs.agents import DemoProvider
from acs.orchestrator import Orchestrator, Store
from acs.server import make_handler
from http.server import ThreadingHTTPServer

BRIEF = {"title": "Lumen Coffee", "text": "A small roastery launching a subscription."}


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.orch = Orchestrator(Store(Path(cls.tmp.name) / "t.json"), DemoProvider(), backoff=0.001)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.orch, lambda: cls.httpd.server_address[1]))
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.orch.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = json.dumps(body) if body is not None else None
        conn.request(method, path, data, {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        try:
            return response.status, json.loads(raw), response
        except ValueError:
            return response.status, raw, response

    def test_health_and_static_frontend(self):
        status, body, response = self.call("GET", "/api/health")
        self.assertEqual((status, body["provider"]), (200, "demo"))
        self.assertIn("Content-Security-Policy", response.headers)
        status, html, _ = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Agentic Creative Studio", html)

    def test_create_poll_and_result(self):
        status, task, _ = self.call("POST", "/api/tasks", {"brief": BRIEF})
        self.assertEqual(status, 201)
        self.orch.wait(task["id"])
        status, done, _ = self.call("GET", "/api/tasks/" + task["id"])
        self.assertEqual((status, done["state"]), (200, "succeeded"))
        self.assertEqual(done["result"]["title"], "Lumen Coffee")
        self.assertEqual(self.call("GET", "/api/tasks")[1][0]["id"], task["id"])

    def test_validation_errors_are_400(self):
        self.assertEqual(self.call("POST", "/api/tasks", {"brief": {"title": "x"}})[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks", {"brief": BRIEF, "chaos": "x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks", ["not", "an", "object"])[0], 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/tasks", "{not json", {"Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_oversized_body_refused(self):
        big = {"brief": {"title": "Fine", "text": "x" * 20000}}
        self.assertEqual(self.call("POST", "/api/tasks", big)[0], 400)

    def test_cross_origin_post_refused_same_origin_allowed(self):
        self.assertEqual(self.call("POST", "/api/tasks", {"brief": BRIEF}, {"Origin": "https://evil.example"})[0], 403)
        ok = self.call("POST", "/api/tasks", {"brief": BRIEF}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(ok[0], 201)

    def test_foreign_host_header_refused_dns_rebinding(self):
        self.assertEqual(self.call("GET", "/api/tasks", None, {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/api/tasks", None, {"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertEqual(self.call("POST", "/api/tasks", {"brief": BRIEF}, {"Host": "attacker.test"})[0], 403)
        self.assertEqual(self.call("GET", "/api/health", None, {"Host": f"localhost:{self.port}"})[0], 200)

    def test_negative_and_chunked_bodies_refused(self):
        for headers in ({"Content-Length": "-1"}, {"Content-Length": "abc"}, {"Transfer-Encoding": "chunked"}):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.putrequest("POST", "/api/tasks")
            for k, v in headers.items():
                conn.putheader(k, v)
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 400, headers)
            conn.close()

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self.call("GET", "/api/tasks/000000000000")[0], 404)
        self.assertEqual(self.call("GET", "/api/tasks/..%2f..%2fetc")[0], 400)
        self.assertEqual(self.call("POST", "/api/tasks/zzzz/cancel")[0], 404)

    def test_static_path_traversal_blocked(self):
        for path in ("/../acs/server.py", "/%2e%2e/acs/server.py", "/app.py", "/data/tasks.json"):
            self.assertEqual(self.call("GET", path)[0], 404, path)

    def test_cancel_and_retry_over_http(self):
        status, task, _ = self.call("POST", "/api/tasks", {"brief": BRIEF, "chaos": "broken"})
        self.orch.wait(task["id"])
        self.assertEqual(self.call("GET", "/api/tasks/" + task["id"])[1]["state"], "failed")
        status, body, _ = self.call("POST", f"/api/tasks/{task['id']}/retry")
        self.assertEqual(status, 200)
        self.orch.wait(task["id"])
        self.assertEqual(self.call("GET", "/api/tasks/" + task["id"])[1]["state"], "succeeded")
        self.assertEqual(self.call("POST", f"/api/tasks/{task['id']}/cancel")[0], 400)

    def test_sse_stream_delivers_events_until_terminal(self):
        _, task, _ = self.call("POST", "/api/tasks", {"brief": BRIEF})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", f"/api/tasks/{task['id']}/events")
        text = conn.getresponse().read().decode()
        conn.close()
        self.assertIn("task.succeeded", text)
        self.assertIn("step.started", text)


if __name__ == "__main__":
    unittest.main()
