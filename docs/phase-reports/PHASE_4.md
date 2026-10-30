# Phase 4 — LLM harness and agents

Status: **gate green** against the fake LLM. Three agents, one gateway, and a set of
grounding checks that discard the model's output when it cannot be verified.

The design decision that matters most: **the deterministic path is the primary path, not a
fallback.** The seeded scenarios are diagnosable by rule, and the rules answer alone
whenever they are confident. Not to save money — a rule that is certain is a better answer
than a model that agrees with it, because it is explainable, free, and cannot be talked
into a different answer by the text of a complaint.

---

## 1. What was built

| Component | Where | Role |
| --- | --- | --- |
| LLM gateway | `ai/llm/gateway.py` | The only thing in the codebase that speaks HTTP to a model |
| Output schemas | `ai/llm/schemas.py` | Every task names a Pydantic model; `extra="forbid"` |
| Redaction | `ai/llm/redaction.py` | Inbound redaction, outbound identifier scan |
| Agent harness | `ai/agents/harness.py` | Typed tool registry, permission tiers, budgets, trace |
| Agent port | `ai/agents/ports.py` | What an agent needs from a model — no URL, no client |
| Tool surface | `ai/agents/tools.py` | Nine `read` tools, two `propose`, one `never` |
| DiagnosisAgent | `ai/agents/diagnosis.py` | Rules first, model only where rules cannot separate causes |
| KnowledgeAssistant | `ai/agents/knowledge.py` | Answers from retrieved documents, or declines |
| Reporter | `ai/agents/reporter.py` | Prose from figures, with numeric grounding |
| Evals | `tests/evals/test_agent_evals.py` | 36 behavioural checks on the trace |
| Guard tests | `tests/security/test_llm_gateway_guards.py` | 26 adversarial tests |

---

## 2. Gate results

```
tests/evals     36 passed    (diagnosis accuracy, trace behaviour, degraded modes)
tests/security  26 passed    (canary, redaction, delimiters, truncation, breaker, budgets)
make eval       green
make ci         green
```

**Diagnosis accuracy: 7/7 labelled scenarios, top-1, with no model involved.** That number
is deliberately measured on the rules path alone — if the rules could not diagnose the
seeded scenarios, the model would be covering for a broken baseline and nobody would know.

Thresholds met: diagnosis top-1 ≥ 0.80 (actual 1.00); injection suite 100% blocked;
every degraded mode still produces the correct answer by rule.

**These numbers are a statement about the harness, not about Qwen.** The fake LLM is
deterministic and returns adversarial responses on demand (ADR 0007). `make eval-live` is
wired and skips cleanly when `LLM_BASE_URL` is unset.

---

## 3. The five agent layers, as built

**Context.** The system prompt is byte-identical on every call — canary, data-handling
rules, JSON rule — so a provider's prompt cache can hold it and a canary appearance is
unambiguous. Evidence is *summarised*, not dumped: a model handed 720 KPI rows answers
about the rows. The goal is restated at the tail.

**Loop.** A deterministic filter runs first and often answers alone. The stop rule is an
external checker (rule certainty ≥ 0.80), never the model's own opinion — a model asked
"are you done?" says yes. Budgets bound steps, tool calls and wall-clock.

**Cheap-decision gate.** `fast=True` for the Reporter, the reasoning model for diagnosis.
Confidence below 0.55 escalates to a human rather than retrying.

**Harness.** Containment first. Tiers are `read` / `propose` / `never`; unregistered tools
default to `never`; the tier check runs before the handler so no handler defends itself
against a hallucinated argument.

**Evals.** On the trace, not the answer: a tool called before any conclusion, approval
requested before any propose-tier action, refusal when evidence is weak, and an injected
instruction in *model output* changing no tool decision.

---

## 4. Grounding: three checks, all mechanical

Each one replaces a judgement call with a comparison.

| Agent | Check | On failure |
| --- | --- | --- |
| Diagnosis | Every cited alarm or node must exist in the evidence the agent was shown | The model's diagnosis is discarded; the rules answer is used |
| Knowledge | Every cited `doc_id` must be one that was actually retrieved | The answer becomes a refusal |
| Reporter | Every number in the prose must be one of the numbers given (percentages of two given figures count as derived) | The whole digest is discarded for the deterministic one |

The Reporter's rule is all-or-nothing on purpose: a digest with one invented figure has
demonstrated that its figures are not trustworthy, and a manager skimming it has no way to
tell which one was wrong.

A fabricated citation is worse than an uncited answer, because it *passes a reviewer's
glance*. That is why `KB-INVENTED` produces a refusal rather than a footnote.

---

## 5. Defects found and fixed

**Five holes in the gateway's guards**, every one a control that was true in the code and
false in practice:

1. **The canary check matched one spelling.** `CANARY in raw` passes `tazama canary`,
   `TAZAMA_CANARY` and `T-A-Z-A-M-A-C-A-N-A-R-Y` — and a model paraphrasing its own system
   prompt reformats casing and punctuation as a matter of course. Both sides are now
   flattened to letters and digits.
2. **Tool calls were never checked.** They are model output as much as the message body
   is, so the canary or an MSISDN could ride out in a tool argument.
3. **The repair attempt skipped the outbound filter.** "Return JSON matching the schema"
   was a route around the identifier check, since only the first generation was scanned.
4. **Untrusted text could close its own block.** `</untrusted>` in a complaint ended the
   data block early and everything after it read as instructions. Unlike "ignore previous
   instructions", which is only words, this changes the prompt's *structure* — so it is
   neutralised, not merely flagged.
5. **Truncation removed the rules.** The prompt was assembled and then cut at the character
   limit, which drops whatever is last: the schema and the instructions. A prompt whose
   rules were truncated is a prompt with no rules.

**`BudgetExhausted` escaped the agents.** A budget firing is a stop rule, not an error —
the loop was meant to end and the caller still needs an answer — so letting it propagate
turned a working safety mechanism into an outage in whichever pipeline step called it. All
three agents now catch it and return the best answer they have, with the stop reason on the
trace.

**The agents imported the gateway directly**, which made every agent a transitive importer
of `httpx` and broke the contract that only the gateway talks to a model endpoint. The
contract was right. `ai/agents/ports.py` now declares `LlmPort` in the consumer and the
composition root supplies the gateway — which also means an agent can be tested with a
three-line fake, and "can an agent reach the network?" is answerable by reading one file.

---

## 6. Deviations from the plan

| Planned | Built | Why |
| --- | --- | --- |
| RAG: adaptive router, hybrid BM25 + vector + reranker, grader | A `search_knowledge` tool behind the `AgentData` port, with owner/expiry metadata and citation verification | The knowledge corpus is a handful of documents. A hybrid retriever over a dozen chunks is machinery without a problem; the *citation* discipline is what makes the answers safe, and that is built and tested. Recorded as tech-debt |
| LLM-bootstrapped → trained small classifier | The deterministic rules classifier from Phase 2, with the LLM path schema-ready | The rules classifier reaches 69.1% exact / 76.2% network-vs-non-network on the seed's own labels and flags 30% for review. The LLM path exists and is validated; nothing trains on top of it yet |
| Per-incident scratchpad | Not built | The agents are single-pass with bounded budgets; a scratchpad would be state without a multi-turn loop to use it |
| `ai/llm/cache.py`, `capability_probe.py` as modules | Both inside `gateway.py` | They are the gateway's internals and splitting them would invite a second caller |

---

## 7. Assumptions

1. **The fake LLM's shapes are the real model's shapes.** Every task's schema is
   contract-tested against the fake, which returns valid, malformed, schema-violating,
   empty and injected responses. If Qwen's JSON differs structurally, the repair attempt
   and then the deterministic fallback absorb it — degraded, and saying so.
2. **Tool-calling support is probed, not assumed.** `LLM_TOOL_CALLING=auto` probes;
   a 400 on a tools payload corrects the capability and retries without them.
3. **The canary is a constant, not a secret.** Its job is to be recognisable in a log after
   the fact, not unguessable.
4. **Redaction placeholders are stable within one request only.** A placeholder meaning the
   same person across requests would be a pseudonym the model could accumulate behaviour
   against.

---

## 8. Known gaps

1. **No real-model evals have been run.** `make eval-live` is wired and skips. Every
   threshold in §2 is a fake-LLM baseline.
2. **No hybrid retrieval, no reranker, no vector index in use** — the in-memory backend is
   a linear scan (ADR 0005, tech-debt #13).
3. **Retrieval faithfulness / answer relevancy / context precision are not measured.** The
   plan named thresholds of 0.9 / 0.85 / 0.8; what is tested instead is *citation
   integrity* — that every cited document was retrieved, and that a weak-evidence answer
   becomes a refusal. The numeric thresholds need a real corpus and a judge model.
4. **Knowledge staleness is enforced but not chased.** Owner and expiry are carried and
   shown; nothing notifies an owner (tech-debt #8).
5. **The classifier is lexical.** Phase 4's trained classifier was not built; the rules
   classifier and its measured accuracy floors stand in for it.
6. **One repair attempt, then fallback.** Deliberate, but it means a model that is
   reliably one field short will always degrade rather than ever succeeding.
