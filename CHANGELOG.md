# Changelog

## 0.2.0 - 2026-10-19

- Real model support: `OpenAICompatProvider` for any `/chat/completions` server, plus a minimal local server (`scripts/local_model_server.py`) for Hugging Face chat models. Live runs measured with Qwen2.5 on a GTX 1060.
- Output contracts per agent (`acs/schemas.py`), validated before an answer is accepted; invalid answers retried with the validator's feedback in the prompt.
- Tool calling with allow-lists, argument validation, untrusted reference reading and human approval for write tools (`awaiting_approval` state, `/approve`, `/reject`).
- Per-task token budget, jittered back-off, run ids on every event, per-task metrics and `/api/metrics`.
- Versioned 100-case evaluation suite with scripted and live modes and three configurations; generated report.
- UI: approval panel, client reference field, per-step timing and per-task metrics.

## 0.1.0 - 2026-10-12

- Five-agent pipeline, state machine, retries, cancellation, manual retry, SSE, deterministic demo provider, failure drills, Anthropic provider tested with an injected transport, security hardening, CI and container.
