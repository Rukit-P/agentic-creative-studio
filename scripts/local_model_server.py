"""Minimal OpenAI-compatible server for a local Hugging Face chat model (dev and evaluation only).

Implements POST /v1/chat/completions (non-streaming, greedy or sampled) and GET /v1/models,
with real token counts in `usage`. One request at a time; binds to 127.0.0.1 only.

    pip install torch transformers accelerate      # not needed by ACS itself
    python scripts/local_model_server.py --model Qwen/Qwen2.5-1.5B-Instruct --port 8791
    export ACS_OPENAI_BASE_URL=http://127.0.0.1:8791/v1 ACS_OPENAI_MODEL=Qwen/Qwen2.5-1.5B-Instruct
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

MAX_BODY = 256 * 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="Qwen/Qwen2.5-1.5B-Instruct")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = ap.parse_args()

    dtype = torch.float16 if args.device == "cuda" else torch.float32
    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=dtype).to(args.device).eval()
    lock = threading.Lock()
    device_name = torch.cuda.get_device_name(0) if args.device == "cuda" else "cpu"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/v1/models":
                return self._send(200, {"data": [{"id": args.model, "object": "model", "device": device_name}]})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.rstrip("/") != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": "body too large"})
            try:
                req = json.loads(self.rfile.read(length))
                messages = req["messages"]
            except (ValueError, KeyError):
                return self._send(400, {"error": "invalid request"})
            max_new = min(int(req.get("max_tokens", 512)), 1024)
            temperature = float(req.get("temperature", 0.0))
            prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            enc = tok(prompt, return_tensors="pt").to(args.device)
            with lock, torch.no_grad():
                started = time.perf_counter()
                kw = {"do_sample": True, "temperature": temperature, "top_p": 0.9} if temperature > 0 else {"do_sample": False}
                out = model.generate(**enc, max_new_tokens=max_new, pad_token_id=tok.eos_token_id, **kw)
                elapsed = time.perf_counter() - started
            new = out[0, enc["input_ids"].shape[1]:]
            text = tok.decode(new, skip_special_tokens=True)
            self._send(200, {"id": f"local-{int(time.time() * 1000)}", "object": "chat.completion", "model": args.model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": int(enc["input_ids"].shape[1]), "completion_tokens": int(new.shape[0]),
                                       "total_tokens": int(enc["input_ids"].shape[1] + new.shape[0])},
                             "timing": {"generation_seconds": round(elapsed, 3), "device": device_name}})

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serving {args.model} on http://127.0.0.1:{args.port}/v1 ({device_name})", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
