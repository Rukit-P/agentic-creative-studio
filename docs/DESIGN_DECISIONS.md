# Design decisions

Decisions I made and why. Each one is something I would defend, or change, in a review.

| # | Decision | Why | Trade-off I accept |
|---|---|---|---|
| 1 | Python standard library only for the core | Nothing to install or audit; tests run in a clean CI image in seconds | Hand-written HTTP server and validator; no async |
| 2 | Errors are typed: `TransientError`, `PermanentError`, `InvalidOutputError` | The retry policy depends on the kind of failure, so providers must say which one it is | Every provider has to map its errors correctly (tested for HTTP codes) |
| 3 | Invalid model output is retryable *by policy*, not by type | A malformed answer is permanent for one prompt, but sampling and validator feedback make a second answer different | A broken model costs up to 3 calls per step before failing; bounded by the token budget |
| 4 | Validator feedback goes into the retry prompt | A blind retry repeats the same mistake; telling the model which field failed is cheap | Slightly longer prompts on retries |
| 5 | Tools are called through one `ToolRunner` owned by the orchestrator | Providers (and therefore models) can ask for tools but never execute them directly; policy lives in one place | Tool calls use a small JSON protocol instead of each vendor's native tool-calling format |
| 6 | JSON tool protocol (`{"tool_call": ...}`) instead of native function calling | Works with any chat model, including small local ones and OpenAI-compatible servers | Native tool calling would be more reliable on large hosted models |
| 7 | Client reference text reaches the model only through `read_reference`, marked untrusted | Keeps untrusted data out of the first prompt and makes every read visible in the event log | The model can still be influenced by what it reads; only actions are guarded |
| 8 | Write tools need human approval of the exact call | Approval of "an export" is not approval of any file the model chooses | One more click; the task waits in `awaiting_approval` |
| 9 | Per-task token budget | A loop or an injected "be verbose" instruction cannot spend without limit | The budget is per task, not per user or per day |
| 10 | Evaluation suite as versioned data with a scripted mode | Free, deterministic, runs in CI; the same cases can hit a real model | Scripted cases test the harness, not model quality |
| 11 | OpenAI-compatible provider as the main live path | One client for local models, vLLM, llama.cpp and hosted APIs | Provider-specific features (caching, native tools) are not used |
| 12 | Cooperative cancellation | Simple and safe with threads | An in-flight model call finishes before the task stops |
