# Case study: Agentic Creative Studio

**Type:** personal project, built to demonstrate agent orchestration and front-end craft.
**Status:** working local demo; container verified; not hosted publicly.

## The problem

Most agent demos show the happy path. In real use the interesting engineering is everything else: a tool times out, a step fails for good, the user cancels halfway, the process restarts. I wanted a small, honest example where those paths are first-class, visible in the UI, and covered by tests.

## What I built

A five-agent pipeline (planner, copywriter, art director, critic, packager) that turns a creative brief into a campaign package, with:

- an explicit state machine per task and per step;
- retries with exponential back-off for transient errors and fail-fast for permanent ones;
- cancellation that also interrupts back-off waits;
- manual retry that keeps completed steps;
- live progress over Server-Sent Events;
- a deterministic demo provider (no API, no network) and an optional LLM provider configured server-side;
- built-in failure drills so recovery can be demonstrated on demand.

## Decisions worth noting

| Decision | Reason |
| --- | --- |
| Python standard library only | Nothing to install or audit; runs anywhere |
| Deterministic demo provider | Reproducible demos and tests; no key needed to evaluate the project |
| Fault injection as part of the task | Retry and recovery paths are testable and demonstrable, not just claimed |
| Errors classified `Transient` / `Permanent` | The retry policy depends on that distinction, so providers must make it explicit |
| Keys server-side only | The browser cannot read or choose credentials |
| No `innerHTML` in the UI | Model output is untrusted text |

## Evidence

- 33 automated tests, run with `python3 -m unittest discover -s tests -t .`.
- Screenshots in `screenshots/` generated from a real run by `scripts/capture_screenshots.sh`.
- Docker image built and run; a task completed through the container API.

## Honest limits

No authentication, no rate limiting, single process, JSON-file storage, template-based demo agents, a rule-based critic, and the LLM path tested with an injected transport rather than the live API. Details in `docs/SECURITY.md`. I have not benchmarked throughput or latency and make no performance claims.

## Role and tools

Designed, built and reviewed by me, using Claude Code as an implementation assistant.
