# Agentic Creative Studio

An orchestrator for multi-step LLM agents that stays correct when the model, the network or a tool misbehaves, and a versioned evaluation suite that measures it.

Five agents (planner, copywriter, art director, critic, packager) turn a creative brief into a campaign package. The domain is deliberately small; the engineering is in what happens around the model: state, retries, output contracts, tool permissions, human approval, budgets and evaluation.

![A finished task waiting for a person to approve the export](screenshots/09-approval.png)

## The problem

Agent demos show the happy path. In real use a model answers in prose instead of JSON, an API times out, a tool gets bad arguments, untrusted text tells the model to call something it should not, or a loop burns tokens. I wanted a small system where every one of those paths is handled on purpose, visible in the UI, covered by tests and **measured** against a fixed set of cases, so each reliability change has a number instead of a claim.

## What it does today

- **State machine** per task and per step: `queued → running → [awaiting_approval →] succeeded | failed | cancelled`.
- **Typed failures**: transient errors (timeouts, 429, 5xx) are retried with exponential back-off and jitter; permanent errors fail fast; invalid model output is retried with the validator's message in the next prompt.
- **Output contracts** per agent (`acs/schemas.py`): nothing reaches the next agent or the user without passing validation.
- **Tool calling** through one `ToolRunner`: per-agent allow-lists, argument validation, unknown tools refused, refusals logged and returned to the model as errors, never executed.
- **Untrusted input**: the client's reference text reaches a model only through the `read_reference` tool, marked `untrusted`.
- **Human approval**: write tools (`save_package`) pause the task in `awaiting_approval`; `POST /approve` runs exactly the call that was shown, reject and cancel write nothing.
- **Budgets**: per-task token budget; tool loop bounded to 4 calls per step.
- **Observability**: run id on every event, per-step timing and tokens, per-task metrics and `/api/metrics`. Briefs and model text are never logged.
- **Providers**: deterministic demo provider (default, offline), any OpenAI-compatible `/chat/completions` server (local model, vLLM, llama.cpp, hosted), and the Anthropic Messages API.
- **Evaluation**: 100 versioned cases in 9 categories, three configurations, scripted mode (free, deterministic) and live mode against a real model.

## What it does not do

- It does not judge creative quality: the critic is rule based and the suite grades the harness.
- It has no authentication and stores tasks in a JSON file: it is a local tool, not a hosted multi-user service.
- Live results come from a small local model (Qwen2.5), not from hosted frontier models.
- Cancellation is cooperative: a model call already in flight finishes before the task stops.

## Results

Full tables, per-category results and every failed case: [docs/EVALUATION_RESULTS.md](docs/EVALUATION_RESULTS.md). Method and honest reading: [docs/EVALUATION.md](docs/EVALUATION.md).

Scripted model, 100 cases (deterministic, reproducible with no API key):

| | baseline | improved | final |
|---|---|---|---|
| Cases passed | 65 % | 92 % | 100 % |
| Recovery from injected faults | 27.6 % | 100 % | 100 % |
| Valid outputs among succeeded tasks | 86.6 % | 100 % | 100 % |
| Unauthorised file writes (15 injection cases) | 3 | 3 | 0 |

`baseline`: one attempt, no validation, no tool policy. `improved`: retries and validation. `final` (what the server runs): plus validator feedback, tool policy, approval, budget and jitter. In the adversarial cases the scripted model obeys the injection on purpose, so the number measures the harness against a worst-case model.

Live run, local Qwen2.5-1.5B-Instruct on a GTX 1060, 32 cases (8 per category, same cases for every configuration):

| | baseline | improved | final |
|---|---|---|---|
| Cases passed | 28.1 % | 46.9 % | 53.1 % |
| Valid outputs among succeeded tasks | 28.6 % | 100 % | 100 % |
| Unauthorised file writes | 1 | 2 | 0 |
| Tokens per task (mean) | 3,936 | 7,392 | 2,513 |
| Wall time per case p50 | 26 s | 65 s | 59 s |

What this shows: validation stops bad answers from being accepted (the baseline "succeeds" on outputs that break the contract), and the tool policy brought unauthorised writes to zero with a real model. What it does not show: that the system works well with a 1.5B model. Almost every remaining failure in `final` is the art director leaving out the `typography` field even after the validator's feedback. A larger model is the next run to make; I have not made it yet. In `final` the model also made no tool calls at all, so this run does not test the tool loop with a real model.

## Architecture

```mermaid
flowchart LR
  UI["Browser UI"] -- "REST + SSE" --> S["acs.server"]
  S --> O["Orchestrator<br/>state, retries, budget, approvals"]
  O --> P{"Provider"}
  P --> D["Demo (deterministic)"]
  P --> M["ModelProvider<br/>prompt, tool loop, JSON"]
  M --> C["OpenAI-compatible / Anthropic"]
  O --> V["schemas.py"]
  O --> T["ToolRunner<br/>allow-list, args, approval"]
  O --> F["Store (JSON, atomic)"]
  E["acs.evals (100 cases)"] --> O
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Why each piece is the way it is: [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md).

## Why these technologies

Python standard library only for the core: nothing to install or audit, the test suite runs in seconds in a clean CI image. The OpenAI-compatible protocol is the main live path because one client then works with a local model on my laptop GPU, with vLLM or llama.cpp, and with hosted APIs. Tool calls use a small JSON protocol rather than a vendor's native function calling so that small local models can take part; the trade-off is written in the design decisions.

## Install and run

Python 3.10 or later. No dependencies for the core.

```bash
git clone https://github.com/Rukit-P/agentic-creative-studio && cd agentic-creative-studio
python3 -m acs.server            # demo mode, http://127.0.0.1:8780
```

Try the failure drills in the form (flaky tool, permanent failure), paste a prompt injection into "Reference from the client", or tick "Export" to see the approval step.

### With a real model (optional)

Any OpenAI-compatible server works. To run a local one (needs `torch`, `transformers`, `accelerate`; a GPU helps):

```bash
python scripts/local_model_server.py --model Qwen/Qwen2.5-1.5B-Instruct --port 8791
export ACS_OPENAI_BASE_URL=http://127.0.0.1:8791/v1 ACS_OPENAI_MODEL=Qwen/Qwen2.5-1.5B-Instruct
python3 -m acs.server --mode openai
```

Anthropic API: `export ACS_LLM_API_KEY=...` and `--mode llm`. Keys are read from the server environment only and never reach the browser, the store or the logs. See [.env.example](.env.example).

## Tests and evaluations

```bash
python3 -m unittest discover -s tests -t .                  # 61 tests, no network, no key
for c in baseline improved final; do python3 -m acs.evals.runner --config $c; done
python3 -m acs.evals.report                                  # regenerates docs/EVALUATION_RESULTS.md
```

CI runs the tests on Python 3.10 and 3.12, a server smoke test and the scripted evaluation of the final configuration.

## Decisions I made

The full list with trade-offs is in [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md). The ones I would defend first in a review: errors are typed so the retry policy can depend on them; invalid output is retryable by policy and the retry prompt says what was wrong; models can request tools but only the orchestrator executes them; approval covers one exact call, not "exports in general"; every change is measured against the same versioned cases.

## How AI tools were used

Built with Claude Code as a pair: I set the scope, the architecture and the acceptance criteria, wrote the evaluation categories, and reviewed every change. Generated code was accepted only when the tests and the evaluation suite passed, and the security notes in [docs/SECURITY.md](docs/SECURITY.md) come from reviewing the code, not from the model's description of it. The git history shows the steps.

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Design decisions](docs/DESIGN_DECISIONS.md) · [Evaluation method](docs/EVALUATION.md) · [Results](docs/EVALUATION_RESULTS.md) · [Security and limitations](docs/SECURITY.md) · [Deployment](docs/DEPLOY.md) · [Changelog](CHANGELOG.md)

## Licence

MIT, see [LICENSE](LICENSE).
