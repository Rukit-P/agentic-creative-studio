# Security report and limitations

Scope: this repository, run locally. It has not had an external audit. The statements below describe what the code does and what was tested.

## Controls in place

| Area | Control | Test |
| --- | --- | --- |
| Network exposure | Binds to `127.0.0.1` by default; warns when started on another host | manual |
| DNS rebinding | `Host` must be a loopback name with the served port when bound to loopback (403 otherwise), for GET and POST | `test_foreign_host_header_refused_dns_rebinding` |
| Resource caps | Negative, non-numeric or chunked bodies refused; 16 KiB body limit; max 50 active tasks; finished tasks pruned to 200; max 16 concurrent SSE streams | `test_negative_and_chunked_bodies_refused`, `test_active_task_cap_and_finished_pruning` |
| CSRF / cross-origin | State-changing requests with a foreign `Origin` header are refused (403) | `test_cross_origin_post_refused_same_origin_allowed` |
| Input validation | Brief fields have type, length and control-character checks; body limited to 16 KiB; task ids must match `[0-9a-f]{12}` | `test_rejects_bad_briefs`, `test_oversized_body_refused`, `test_unknown_and_malformed_ids` |
| Path traversal | Static files are served only from `web/`, only for `.html .js .css .svg` | `test_static_path_traversal_blocked` |
| XSS | The UI writes untrusted text with `textContent`/DOM APIs; CSP `default-src 'self'` with no inline script or style | review + CSP header test |
| Secrets | LLM key is read from the server environment; it is never returned by the API, written to the store or logged | `test_request_shape_and_json_extraction` |
| Error leakage | Unexpected exceptions are reported by class name only | `test_unexpected_exception_fails_the_task_not_the_worker` |
| Prompt injection | Brief and client reference are passed as data; the reference reaches a model only through the `read_reference` tool, wrapped as `{"untrusted": true}`. Whatever the model then asks for is checked by the tool policy below, so injected text can change *content* but not *permissions* | `test_injected_write_request_is_refused_and_nothing_is_written`, 15 adversarial eval cases |
| Tool permissions | Every tool call goes through `ToolRunner`: per-agent allow-list, unknown tools refused, arguments type/format-checked, unexpected arguments refused. Refusals are logged as `tool.blocked` events and returned to the model as an error, never executed | `tests/test_tools.py` |
| Human approval | `write` tools (`save_package`) raise `ApprovalRequired`; the task waits in `awaiting_approval` and only `POST /approve` executes exactly the call that was shown. Reject and cancel write nothing. The export path is a fixed folder, kebab-case `.json` names only, checked again inside the tool | `Approval` tests in `tests/test_reliability.py` |
| Output contracts | Every agent answer is validated against `acs/schemas.py` before it is stored or passed on | `Validation` tests, 15 `invalid_output` eval cases |
| Cost limits | Per-task token budget (default 20,000) stops a task that overspends; tool loop limited to 4 calls per step | `Budget`, `test_tool_loop_is_bounded`, 5 `budget` eval cases |

## Limitations (read before deploying)

1. **No authentication or authorisation.** Anyone who can reach the port can create, cancel and retry tasks. Do not expose it to the internet without a reverse proxy that authenticates. When started with `--host 0.0.0.0` (the container) the `Host` check is disabled and host policy belongs to the proxy.
2. **No per-client rate limiting.** Global caps exist (above), but a single client can use all of them; in LLM mode a client can spend your API budget. Put limits in front of it.
3. **Prompt injection is mitigated, not solved.** A hostile brief or reference can still steer model *output* (the copy it writes). What it cannot do is call a tool outside the agent's allow-list or write a file without a human approval. The agents have no network or shell tools at all.
4. **Cancellation is cooperative.** An in-flight model call is not aborted; its cost is incurred.
5. **JSON-file storage** is single-process and has no migrations or encryption. Briefs and results are stored in plain text under `data/` (git-ignored).
6. **Single node.** Task state lives in one process; there is no queue or horizontal scaling.
7. **Live testing used a small local model** (Qwen2.5 via `scripts/local_model_server.py`, see `docs/EVALUATION_RESULTS.md`). The Anthropic provider is unit-tested with an injected transport and mocked HTTP errors, not against the paid API.
8. **`ThreadingHTTPServer`** is adequate for local use and demos, not for hostile traffic.
9. **The critic is rule-based.** Its score checks structure (length, presence, counts); it is not an assessment of creative quality.
10. **Demo content is template-based** and deliberately generic.

## Dependencies and licences

No runtime dependencies. Python standard library only. Project licence: MIT.

## Reporting

Open an issue on the repository, or contact the author privately for anything sensitive.
