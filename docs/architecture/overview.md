# TAZAMA architecture overview

Current as of Phase 7. Two halves: **§1–3 are for anyone**, §4 onward is for engineers.

---

## 1. What the system does, without jargon

A mobile network produces two kinds of signal about the same problem, and nobody joins them
up.

The **network** knows first: a cell's availability drops, an alarm fires, throughput falls.
The **customer** knows second and tells you third: they try to make a call, it fails, and
eventually they ring the contact centre.

TAZAMA is the part in between. It takes both signals, decides they are the same incident,
works out who is affected and why, proposes what to do about it, and drafts a message to the
customers involved — then stops and asks a human before anything reaches anybody.

```
 network KPIs ─┐
 alarms ───────┤
               ├─▶ is this one incident? ─▶ who is affected? ─▶ why? ─▶ how urgent?
 complaints ───┤          (correlation)         (impact)     (diagnosis) (priority)
 care calls ───┘                                                    │
                                                                    ├─▶ tell the customers  ← human approves
                                                                    ├─▶ recommend a fix     ← human approves
                                                                    └─▶ did it work?        (verification)
```

Every box writes down what it decided, what it decided from, and which version of which
rule or model decided it. That record is the product as much as the answer is.

---

## 2. The three design commitments

Everything else in this document follows from these.

**AI recommends, rules decide, humans approve.** There is no adapter, no credential and no
code path from TAZAMA to a network element. An agent can recommend an approved runbook; it
cannot run one. A notification is drafted into an approved template and waits for a person.

**The product works with the AI switched off.** Not as a degraded curiosity — as the
primary path. Diagnosis by rules alone scores 7 of 7 on the labelled scenarios; detection
scores precision 1.00 / recall 1.00. When a model is unavailable the console says *AI
degraded* and keeps working, and every decision records whether a rule or a model produced
it.

**Phone numbers stop at the door.** An MSISDN is converted to an irreversible token the
moment it arrives, before anything else sees it. The model never receives an identifier, and
every outbound text — to a model, a log, a Teams card, an export — is scanned on the way
out. A Teams card containing a phone number *blocks the report* rather than being quietly
cleaned, because the point is to fix the source.

---

## 3. The hardest problem, measured from day one

Matching a customer's complaint to the cell that serves them. A customer says "my internet
is bad in Westlands"; the network records behaviour per cell. Bridging those is where all
the uncertainty lives, and everything downstream inherits it.

So it is **measured on every run** (`make catalog`) and shown on the Command Center rather
than buried: currently **52.0%** of complaints resolve to a cell. That number is the honest
ceiling on how much of the customer signal the product can use, and it is the first thing to
check after pointing the system at real tables.

---

## 4. Processes

| Process | Responsibility |
| --- | --- |
| `api` (FastAPI) | REST `/api/v1` (61 routes), SSE stream, OpenAPI, RBAC, rate limits |
| `worker` | Outbox consumers — the pipeline runs here — plus scheduled jobs |
| `web` (Next.js 16) | Command Center, incident card, complaints, care, explorer, insights, reports, AI quality |
| `fake-llm` | Deterministic model server; started by `make demo` so the AI paths are live offline |
| `oracle` (profile) | Oracle Database Free for the integration suite and live-shaped runs |
| `otel-collector` + `jaeger` (profile) | Traces across API → events → worker → model |

---

## 5. Layering, enforced rather than described

```
   api / worker          ← composition root: the only place adapters are constructed
        │                  (tazama/composition.py, tazama/training.py)
   infrastructure         ← adapters: db(sqlite|oracle), eventbus, teams, sms, agent_repo
        │
       ai                 ← llm gateway, agents, ml (no DB, no HTTP except the gateway)
        │
     modules              ← business logic; declares ports; never imports a sibling
        │
      core                ← settings, logging, errors, security, tracing, ids, clock, paths
        │
     domain               ← pure entities, value objects, events, state machine (no I/O)
```

Five import-linter contracts fail `make lint` on violation, so these are mechanical. Two
were caught breaking during Phase 6 and both fixes improved the design: the agents now
depend on an `LlmPort` declared in their own package rather than importing the gateway (and
transitively `httpx`), and the ML composition root moved out of `ai/` to sit beside the
request-path one.

**Why `infrastructure` is above `ai`:** adapters implement the ports that modules and agents
declare, so they must be able to see those declarations. `ai/ml` therefore takes its data
through a port and the training entrypoint wires it — which is also what keeps `ai/ml`
testable with a three-line fake.

---

## 6. Data flow

1. A complaint or a KPI/alarm sample arrives through an ingestion adapter (or the seed, in
   demo mode).
2. Ingestion normalises, deduplicates and **pseudonymises**, then writes the aggregate *and*
   its domain events in one transaction — the outbox pattern.
3. The worker polls the outbox and dispatches. Every consumer is idempotent on the event id,
   so at-least-once delivery is safe and `scripts/replay.py` reproduces any incident.
4. Enrichment classifies the text; site identification resolves a serving cell with a
   confidence band (low confidence routes to a human rather than guessing).
5. Correlation groups complaints and degradations by space, time and topology, suppressing
   planned maintenance.
6. Impact, diagnosis, prioritisation, routing, notification proposal, recovery
   recommendation and verification each append a decision record with inputs, rule/model/
   prompt version, confidence and evidence.
7. Reporting renders a digest; the dry-run sender validates it and posts nothing.

---

## 7. Where the AI sits

All model traffic goes through **one gateway** (`ai/llm/gateway.py`). Nothing else in the
codebase speaks HTTP to a model, which is what makes its controls guarantees rather than
conventions: env-driven config, inbound redaction, a schema on every response with one
repair attempt then a deterministic fallback, a canary token that destroys any response
echoing the system prompt, a circuit breaker, per-process budgets, a cache, and token
accounting.

**Three agents**, each bounded by steps, tool calls and wall-clock:

| Agent | Tier ceiling | Without a model |
| --- | --- | --- |
| DiagnosisAgent | `propose` | Deterministic rules — 7/7 on the labelled scenarios |
| KnowledgeAssistant | `read` | The retrieved excerpt verbatim, with its citation |
| Reporter | `read` | The figures stated plainly |

Tools are typed and tiered `read` / `propose` / `never`; an unregistered tool defaults to
`never`, so unknown means forbidden. `execute_runbook` is registered *only* so that an agent
reaching for it is recorded — it is refused before any handler runs.

Three grounding checks, all mechanical: a diagnosis citing evidence the agent was not shown
is discarded; an answer citing an unretrieved document becomes a refusal; a digest
containing a number that was not in its inputs is thrown away whole.

The natural-language query path follows the same shape — the model writes SQL, the guard
gives a verdict without executing, a human reads it and presses Run. See
`algorithm-choices.md` §7.

---

## 8. Modes

`APP_MODE=demo` seeds synthetic data, allows writes, shows the DEMO DATA ribbon, and exposes
two demo affordances (a list of sample subscriber tokens, and lookup by token).

`APP_MODE=live` reads real sources through `config/source_mappings.yaml` with a read-only
user, writes only to the app schema, keeps `ALLOW_LIVE_WRITES=false`, and closes both demo
affordances — `/care/samples` 404s, and care lookup requires an MSISDN rather than a token,
so nobody can walk the pseudonym space.

**Switching modes is configuration, never code.**

---

## 9. Further reading

| Topic | Document |
| --- | --- |
| Why each algorithm, and what it was chosen over | `algorithm-choices.md` |
| The five agent layers as built | `agent-layers.md` |
| What each module may import | `module-boundaries.md` |
| The nine security layers and the test for each | `../security/security-baseline.md` |
| STRIDE over the eight real trust boundaries | `../threat-model.md` |
| Privacy assessment and what blocks real data | `../dpia.md` |
| Presenting this to leadership | `../DEMO_SCRIPT.md` |
| What is deliberately not built | `../ROADMAP.md` |
