"""HTTP API + static frontend on the standard library (ThreadingHTTPServer).

Routes
  GET  /api/health            GET  /api/tasks            POST /api/tasks
  GET  /api/tasks/<id>        GET  /api/tasks/<id>/events (SSE)
  POST /api/tasks/<id>/cancel POST /api/tasks/<id>/retry
  POST /api/tasks/<id>/approve POST /api/tasks/<id>/reject   (pending write-tool call)
  GET  /api/metrics
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .agents import DemoProvider, LLMProvider, OpenAICompatProvider
from .orchestrator import CHAOS_MODES, Orchestrator, Store, ValidationError

WEB = Path(__file__).resolve().parent.parent / "web"
ID = re.compile(r"^[0-9a-f]{12}$")
MAX_BODY = 16 * 1024
MAX_SSE = 16                    # concurrent event streams
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
CSP = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"


def build_provider(mode: str, env=None):
    if mode == "llm":
        provider = LLMProvider.from_env(env)
        if provider is None:
            raise SystemExit("ACS_LLM_API_KEY is not set; use --mode demo or export the key (see README).")
        return provider
    if mode == "openai":
        provider = OpenAICompatProvider.from_env(env)
        if provider is None:
            raise SystemExit("ACS_OPENAI_BASE_URL is not set (e.g. http://127.0.0.1:8791/v1); see README.")
        return provider
    return DemoProvider(delay=float((env or os.environ).get("ACS_DEMO_DELAY", "0.7")))


def make_handler(orch: Orchestrator, port_getter, bind_host: str = "127.0.0.1"):
    streams = threading.BoundedSemaphore(MAX_SSE)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ACS/" + __version__
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # quiet by default; no request bodies are ever logged
            if os.environ.get("ACS_ACCESS_LOG"):
                sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status, payload, ctype="application/json", extra=None):
            body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status, message):
            self._send(status, {"error": message})

        def _host_ok(self):
            """Refuse requests whose Host is not the loopback name we serve (DNS-rebinding defence)."""
            if bind_host not in ("127.0.0.1", "localhost", "::1"):
                return True                       # deliberately exposed (container): the proxy owns host policy
            host = (self.headers.get("Host") or "").rsplit(":", 1)
            return host[0] in LOCAL_HOSTS and (len(host) == 1 or host[1] == str(port_getter()))

        def _same_origin(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True                       # non-browser clients (curl, tests)
            port = port_getter()
            return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self._host_ok():
                return self._error(403, "Host not allowed")
            try:
                if path == "/api/health":
                    return self._send(200, {"ok": True, "version": __version__, "provider": orch.provider.name,
                                            "chaos_modes": list(CHAOS_MODES), "max_attempts": orch.max_attempts})
                if path == "/api/tasks":
                    return self._send(200, orch.listing())
                if path == "/api/metrics":
                    return self._send(200, orch.metrics())
                m = re.fullmatch(r"/api/tasks/([^/]+)(/events)?", path)
                if m:
                    if not ID.match(m[1]):
                        return self._error(400, "Invalid task id")
                    if m[2]:
                        return self._sse(m[1])
                    return self._send(200, orch.snapshot(m[1]))
                return self._static(path)
            except KeyError:
                self._error(404, "Unknown task")

        def _static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            file = (WEB / name).resolve()
            if WEB.resolve() not in file.parents or file.suffix not in TYPES or not file.is_file():
                return self._error(404, "Not found")
            self._send(200, file.read_bytes(), TYPES[file.suffix])

        def _sse(self, task_id):
            orch.snapshot(task_id)                # KeyError -> 404 before headers are sent
            if not streams.acquire(blocking=False):
                return self._error(503, "Too many open event streams")
            self.send_response(200)
            for k, v in (("Content-Type", "text/event-stream"), ("Cache-Control", "no-store"), ("Connection", "close"),
                         ("X-Content-Type-Options", "nosniff")):
                self.send_header(k, v)
            self.end_headers()
            after = int(self.headers.get("Last-Event-ID", "0") or 0)
            self.close_connection = True
            try:
                while True:
                    events, done = orch.events_after(task_id, after)
                    for event in events:
                        after = event["seq"]
                        self.wfile.write(f"id: {after}\nevent: task\ndata: {json.dumps(event)}\n\n".encode())
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    if done and not events:
                        break
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                streams.release()

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                raise ValidationError("Invalid Content-Length") from None
            if self.headers.get("Transfer-Encoding"):
                raise ValidationError("Chunked bodies are not accepted")
            if length < 0 or length > MAX_BODY:
                raise ValidationError("Request body too large")
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                raise ValidationError("Body must be JSON") from None

        def do_POST(self):
            path = urlsplit(self.path).path
            if not self._host_ok():
                return self._error(403, "Host not allowed")
            if not self._same_origin():
                return self._error(403, "Cross-origin request refused")
            try:
                if path == "/api/tasks":
                    data = self._body()
                    if not isinstance(data, dict):
                        raise ValidationError("Body must be an object")
                    task = orch.submit(data.get("brief"), data.get("chaos", "none"))
                    return self._send(201, task)
                m = re.fullmatch(r"/api/tasks/([0-9a-f]{12})/(cancel|retry|approve|reject)", path)
                if m:
                    if m[2] in ("approve", "reject"):
                        return self._send(200, orch.decide(m[1], m[2] == "approve"))
                    return self._send(200, getattr(orch, m[2])(m[1]))
                return self._error(404, "Not found")
            except ValidationError as error:
                self._error(400, str(error))
            except KeyError:
                self._error(404, "Unknown task")

    return Handler


def serve(host="127.0.0.1", port=8780, mode="demo", data="data/tasks.json", exports="data/exports"):
    orch = Orchestrator(Store(data), build_provider(mode), exports_dir=exports)
    httpd = ThreadingHTTPServer((host, port), make_handler(orch, lambda: httpd.server_address[1], host))
    httpd.daemon_threads = True
    return httpd, orch


def main(argv=None):
    ap = argparse.ArgumentParser(description="Agentic Creative Studio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--mode", choices=("demo", "llm", "openai"), default="demo",
                    help="demo: deterministic, offline; llm: Anthropic API; openai: any OpenAI-compatible server")
    ap.add_argument("--data", default="data/tasks.json")
    args = ap.parse_args(argv)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: there is no authentication; do not expose this server beyond localhost", file=sys.stderr)
    httpd, orch = serve(args.host, args.port, args.mode, args.data)
    print(f"Agentic Creative Studio {__version__} [{orch.provider.name}] http://{args.host}:{httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        orch.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
