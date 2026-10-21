# TAZAMA architecture overview

## The journey the system automates
```
Network degradation ──┐
                      ├─▶ Correlation ─▶ Incident ─▶ Impact ─▶ Diagnosis ─▶ Priority/Routing
Customer complaint ───┘                                   │
                                                          ├─▶ Proactive notification (approved)
                                                          ├─▶ Recovery recommendation (approved)
                                                          └─▶ KPI verification ─▶ Learning
```
The network sees degradation first; complaints confirm it late. The value is in closing
that gap: fewer avoidable calls, lower cost per contact, faster MTTR, SLA protection.

The hardest problem is the **complaint → cell/site join**, so it is measured from day one
(`make catalog` prints join coverage on every run) rather than discovered late.

## Processes
| Process | Responsibility |
| --- | --- |
| `api` (FastAPI) | REST `/api/v1`, SSE stream, OpenAPI, RBAC, rate limits |
| `worker` | Outbox consumers, scheduled reports, time-based state transitions |
| `web` (Next.js) | Command Center, incident detail, explorer, insights, reports, admin |
| `oracle` (profile) | Oracle Database Free for the integration suite and live-shaped runs |
| `fake-llm` (profile) | Deterministic model server for offline work and evals |
| `otel-collector` + `jaeger` (profile) | Traces across API → events → worker → LLM |

## Layering (enforced by import-linter)
```
   api / worker          ← composition root: the only place adapters are constructed
        │
       ai                ← gateway, agents, rag, ml (no direct DB, no HTTP except gateway)
        │
     modules             ← business logic; declare ports; never import each other
        │
  infrastructure         ← adapters: db(sqlite|oracle), eventbus, vectorstore, external systems
        │
      core               ← settings, logging, errors, security, tracing, ids, clock
        │
     domain              ← pure entities, value objects, events, state machine (no I/O)
```
Contracts in `backend/pyproject.toml` (`[tool.importlinter]`) fail `make lint` on a
violation, so the boundaries are mechanical rather than aspirational.

## Data flow
1. A complaint or a KPI/alarm sample arrives (ingestion adapters, or the seed in demo mode).
2. Ingestion normalises, deduplicates and pseudonymises, then writes the aggregate **and**
   its domain events in one transaction (outbox).
3. The worker dispatches events; each consumer is idempotent on the event id.
4. Correlation groups complaints and degradations into incidents using space, time and
   topology (shared upstream element), suppressing planned maintenance.
5. Impact, diagnosis, prioritisation, routing, notification proposal, recovery
   recommendation and verification each append a decision record with its inputs,
   rule/model/prompt version and evidence.
6. Every step is audited and traceable by correlation id; `scripts/replay.py` reproduces it.

## Modes
`APP_MODE=demo` seeds synthetic data and allows writes. `APP_MODE=live` reads real sources
through `config/source_mappings.yaml` with a read-only user, writes only to the app schema,
and keeps `ALLOW_LIVE_WRITES=false`. Switching modes is configuration, never code.

## Where AI sits
AI recommends, rules decide, humans approve. All model traffic goes through
`ai/llm/gateway.py`; every output is schema-validated with a deterministic fallback, so the
product keeps working when the model is down (the UI shows "AI degraded"). See
`agent-layers.md` for the five-layer engineering and `../security/security-baseline.md`
for the guardrails.
