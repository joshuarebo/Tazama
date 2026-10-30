# Runbook — the model endpoint is unavailable

**Severity: low.** This is not an outage. The product is designed to work without a model,
and the only correct action is usually to confirm that it is doing so.

---

## Symptoms

- "AI degraded" appears in the console header.
- `/readyz` reports the LLM probe as unreachable.
- Logs show `llm_degraded` with a `note`, or `circuit breaker open`.
- Incident cards show a cause and evidence but `method: "rules"` on the decision row.

## What is still working

Everything that matters operationally:

| Capability | Without a model |
| --- | --- |
| Complaint classification | The deterministic rules classifier (69.1% exact on the seed's own labels, 30% flagged for review) |
| Diagnosis | The rules path, which diagnoses 7/7 labelled scenarios top-1 |
| Knowledge answers | The retrieved excerpt verbatim, with its citation |
| Digests | The figures stated plainly |
| Detection, correlation, impact, prioritisation, routing, notification, verification | Entirely unaffected — none of them call a model |

Nothing is queued waiting for the model and nothing will be replayed when it returns. A
degraded answer was produced and recorded at the time, with `degraded: true` on the audit
row.

## Diagnosis

```bash
make doctor                      # probes the endpoint and prints the reason
curl -sS "$LLM_BASE_URL/v1/models"   # the same probe the gateway performs
```

Read the `note` on a degraded result. It distinguishes the cases that need different
responses:

| Note | Meaning | Action |
| --- | --- | --- |
| `no model endpoint configured` | `LLM_BASE_URL` is empty | None, if that is intended. This is a supported configuration |
| `circuit breaker open` | Four consecutive failures; failing fast on purpose | Fix the endpoint; the breaker half-opens after 30s |
| `model endpoint returned 5xx` / `429` | The endpoint is up but unhealthy or rate-limiting | Endpoint-side |
| `two invalid responses; falling back` | The model answers but not in the required shape | See below — this one is worth investigating |
| `call budget exhausted` / `token budget exhausted` | A process ceiling was reached | See below |
| `model output contains MSISDN` | **Not a model problem.** Escalate |

## Actions

**If the endpoint is down:** nothing to do in TAZAMA. Restore the endpoint. The breaker
recovers on its own — it half-opens after 30 seconds and closes on the first success.

**If responses are schema-invalid:** the model is reachable and answering wrongly, which is
more concerning than being down, because it means a prompt or a model version changed.
Check whether `LLM_MODEL_REASONING` was repointed, then compare against the fake LLM:

```bash
make fake-llm          # a deterministic OpenAI-compatible server
LLM_BASE_URL=http://127.0.0.1:8099 make eval
```

If the evals pass against the fake and fail against the real endpoint, the model is the
variable, not the code.

**If budgets are exhausted:** this is a ceiling, not a fault, and it resets when the process
restarts. A long-running worker hitting it repeatedly means either traffic has grown or
something is looping. Check `accounting` on the gateway for the per-task breakdown before
raising the limit — a single task dominating the count is a bug, not capacity.

## What not to do

- **Do not disable the schema validation or the repair step** to "get the AI working". An
  unvalidated model output is worse than no model output: it reaches a card or a proposal
  looking authoritative.
- **Do not raise the budgets to clear a backlog.** The backlog is not LLM-dependent.
- **Do not point `LLM_BASE_URL` at a hosted endpoint** to restore service. That is a
  cross-border transfer of complaint text and needs its own privacy assessment
  (`docs/dpia.md` §2).

## Escalate immediately if

- A degraded note says `model output contains MSISDN` or `the inbound redaction has a
  hole` — an identifier reached a model endpoint. Treat as a privacy incident.
- A `CanaryLeak` is raised — the system prompt leaked into output.

Both are blocked at the boundary and the response is discarded, so there is no customer
impact. They mean a control was exercised, and the control working is exactly why it needs
investigating.
