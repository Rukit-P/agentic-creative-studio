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
| Prompt injection | In LLM mode the brief is passed as data in the user message and the system prompt marks it untrusted; output must parse as JSON and is only rendered as text | `test_invalid_json_is_permanent` |

## Limitations (read before deploying)

1. **No authentication or authorisation.** Anyone who can reach the port can create, cancel and retry tasks. Do not expose it to the internet without a reverse proxy that authenticates. When started with `--host 0.0.0.0` (the container) the `Host` check is disabled and host policy belongs to the proxy.
2. **No per-client rate limiting.** Global caps exist (above), but a single client can use all of them; in LLM mode a client can spend your API budget. Put limits in front of it.
3. **Prompt injection is mitigated, not solved.** A hostile brief can still steer model output (content). The agents have no tools, file or network access, which bounds the impact to text.
4. **Cancellation is cooperative.** An in-flight model call is not aborted; its cost is incurred.
5. **JSON-file storage** is single-process and has no migrations or encryption. Briefs and results are stored in plain text under `data/` (git-ignored).
6. **Single node.** Task state lives in one process; there is no queue or horizontal scaling.
7. **The LLM provider has been tested with an injected transport and mocked HTTP errors, not against the live API** in this repository.
8. **`ThreadingHTTPServer`** is adequate for local use and demos, not for hostile traffic.
9. **The critic is rule-based.** Its score checks structure (length, presence, counts); it is not an assessment of creative quality.
10. **Demo content is template-based** and deliberately generic.

## Dependencies and licences

No runtime dependencies. Python standard library only. Project licence: MIT.

## Reporting

Open an issue on the repository, or contact the author privately for anything sensitive.
