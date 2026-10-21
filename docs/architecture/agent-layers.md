# The five agent layers in TAZAMA

The model is one component; the engineering is the five layers around it. This document
says exactly where each layer lives in this repo, and how it is verified.

| Layer | Question it answers | Where it lives |
| --- | --- | --- |
| Context | What does the model see? | `ai/llm/prompts/`, `ai/agents/harness/context.py` |
| Loop | Who decides the next step? | `ai/agents/*/graph.py`, `ai/agents/harness/budget.py` |
| Cheap decisions | What can be decided without the expensive model? | `modules/*/rules.py`, `ai/ml/`, `ai/llm/gateway.py` fast model |
| Harness | What can it reach, and who checks it? | `ai/agents/harness/`, `ai/agents/tools/`, `config/policies.yaml` |
| Evals | How do we know it got better? | `ai/evals/`, `backend/tests/evals/`, `make eval` |

## 1. Context — what is on the desk
Rules we hold ourselves to:

* **Stable first, volatile last.** The system prompt is byte-identical across calls (role,
  constraints, output schema, canary). No timestamps, no re-ordered JSON keys, nothing that
  breaks prompt caching. Versioned under `ai/llm/prompts/v1/…`; a change is a new version.
* **Tools are fixed for the duration of a run.** To restrict a tool mid-run we refuse the
  call; we do not remove the definition (that would invalidate the cache and invite calls
  to tools that no longer exist).
* **Large payloads stay out of the window.** KPI series, alarm dumps and documents are
  referenced by id/path and fetched by a tool; the window carries the reference and a
  summary, not the body.
* **Restate the goal at the tail.** Every few steps the current objective and the
  remaining budget are appended near the end of the context, where attention is strongest.
* **Provenance travels with content.** Every injected fact records where it came from, so
  a diagnosis can cite it and an auditor can check it.
* **Under ~20 tools, load them; beyond that, search.** TAZAMA's read-only tool set is
  small by design (five tools for DiagnosisAgent), so they are all loaded.

Verified by: prompt-stability tests (the system prompt is a frozen fixture; a diff fails
the gate), context-budget tests, and provenance assertions in the diagnosis evals.

## 2. Loop — goal, checker, stop rule, budget
A loop is justified only where the steps cannot be written in advance: root-cause
diagnosis. Everything else — classification, correlation, prioritisation, notification
rendering, verification — is a deterministic workflow, because a workflow is cheaper,
parallelisable and restartable at the failing step.

DiagnosisAgent's loop has all four parts:

* **Goal with a definition of done:** a structured `Diagnosis` (probable cause, confidence,
  evidence list, recommended runbook id) that passes schema validation and whose evidence
  references real rows.
* **Checker outside the model:** schema validation, evidence existence checks against the
  database, and numeric grounding. The model does not grade itself.
* **Stop rule:** checker passes, or max steps, or two consecutive identical actions
  (stuck), or budget exhausted.
* **Budget:** steps, tokens and wall-clock, enforced in `harness/budget.py`.

Shape, following the Atlan pattern: **deterministic filter first, loop briefly, fall back.**
Rules decide whether an incident even warrants an agent; the loop gets a small number of
cycles; if confidence is still below the threshold, a deterministic template explanation
plus a human review flag is the answer. The fallback is never "no answer".

Verified by: budget-exhaustion tests, stuck-detection tests, and a fallback suite that
asserts a usable deterministic result when the loop fails.

## 3. Cheap decisions — the gate in front of the expensive model
Order of operations from the PRD: classical first, LLM where it adds value, agent only for
multi-step reasoning. Concretely:

* Rules and thresholds (`config/policies.yaml`) decide suppression, priority and routing.
* A trained classifier (LightGBM/linear, bootstrapped from LLM labels) handles
  high-volume complaint classification; the LLM is the fallback for low-confidence items
  and the labeller for new taxonomy entries.
* `LLM_MODEL_FAST` handles narrow picks (language detection, template slot choice);
  `LLM_MODEL_REASONING` is reserved for explanation and diagnosis.
* **Fail closed:** below the confidence threshold, escalate to the more expensive path, and
  ultimately to a human — never silently accept a cheap answer.

Verified by: baseline-vs-model comparison in Phase 3 (the dumb thing must be beaten, not
assumed inferior) and routing tests that assert escalation on low confidence.

## 4. Harness — containment, guides, sensors, permissions
* **Containment first.** The agent's tools are read-only by construction: `kpi_query`,
  `alarm_query`, `topology_lookup`, `similar_incidents`, `maintenance_lookup`. There is no
  write tool, no shell, no network. In live mode the DB user is read-only and
  `ALLOW_LIVE_WRITES=false`. What it cannot reach, it cannot break.
* **Guides.** `CLAUDE.md` for humans and coding agents; typed tool descriptions and output
  schemas for the model; few-shot examples versioned with the prompt.
* **Sensors.** Fast deterministic checks on every output (schema, citations, numeric
  grounding, evidence existence); slower checks (a second model reviewing a draft digest)
  only where the cost is justified.
* **Permissions.** Three tiers — `read` (auto), `propose` (writes a proposal a human
  approves), `never` (not exposed at all: runbook execution, customer messaging, network
  commands). Approval prompts are not the main control; the absence of the capability is.

Verified by: tool-registry tests (a `never`-tier action has no tool), argument-validation
tests, permission-tier tests, and the injection suite proving text cannot acquire a tool.

## 5. Evals — the same test, every change
* Golden sets are generated from the **seeded scenarios with ground truth**, so "correct"
  is defined by construction rather than by opinion.
* Cases come from real failures: every bug found in an agent becomes a case.
* **Behavioural checks run on the trace**, not only the final answer: was a tool called
  before answering, is every number present in the source rows, is there a citation, was
  approval requested before a propose-tier action, was the canary absent.
* Thresholds (PRD §9): retrieval faithfulness ≥ 0.9, answer relevancy ≥ 0.85, context
  precision ≥ 0.8, plus classification accuracy and diagnosis top-k against ground truth.
* The judge is checked too: where a model grades output, agreement with hand-labelled
  samples is measured; a judge that disagrees with itself across runs is reported rather
  than trusted.
* Watch the individual checks, not the aggregate pass rate — a single behaviour can break
  while the average improves.

Run with `make eval` (fake LLM, blocking) and `make eval-live` (real Qwen, advisory until
baselines exist — ADR 0007).

## Harness expiry
Every piece of scaffolding is a bet that the model cannot do something. Those bets expire.
Re-read this document when the model endpoint is upgraded, and delete what is no longer
needed instead of accumulating it.
