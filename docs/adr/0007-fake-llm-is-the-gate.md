# ADR 0007 — The fake LLM is the eval gate; real Qwen is advisory

Status: accepted · Date: 2026-10-21 · Phase: 0 (evals grow in Phase 4)

## Context
AI behaviour must be gated in CI, but the internal Qwen endpoint is unreachable from the
build environment and no credentials exist. A non-deterministic model is also a poor gate:
a flaky eval teaches the team to ignore it.

## Decision
`scripts/fake_llm.py` is an OpenAI-compatible server that is **deterministic** (same
request → same response, by prompt hash) and **adversarial on demand** (timeout, malformed
JSON, schema violation, 500, 429, tools-unsupported, empty response, canary leak, slow).

* `make eval` runs against the fake LLM and is **blocking**.
* `make eval-live` runs the same suite against real Qwen when `LLM_BASE_URL` is set, and
  is **non-blocking** until baselines exist (PRD §9 allows this explicitly).
* Behavioural checks run on the trace (was a tool called before answering? is there a
  citation? was approval requested before a propose-tier action?), not only on the final
  text, so passing the fake cannot be achieved by returning canned prose.

## Consequences
Eval thresholds in Phase 4 are **fake-LLM baselines**, not claims about Qwen's quality;
the phase report says so. When the real endpoint is available, run `make eval-live`,
record the numbers, and only then promote them to gates.
