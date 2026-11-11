# Architecture facts, read from the code

Source of truth for `tazama-demo.html`. Every line below was read from the codebase on
2026-11-11, not from the demo script. File paths are given so each claim can be checked.

## Scale

| | |
| --- | --- |
| Python source files | 171 (`backend/src`) |
| Python test files | 53 (`backend/tests`), 1,686 tests collected |
| Business modules | 16 (`backend/src/tazama/modules/`) |
| API routers / routes | 15 routers, 58 routes (`api/v1/routers/`) |
| Domain event types | 24 (`domain/events.py`) |
| Lifecycle states / triggers | 16 states, 16 triggers (`domain/state_machine.py`) |
| Alembic migrations | 11 |
| Roles / capabilities | 5 roles, 21 capabilities (`core/security.py`) |
| Model cards | 2 (`docs/model-cards/`) |
| Frontend | 19 TS/TSX files, 9 routes |

## The 16 modules

`ingestion`, `complaints`, `enrichment`, `site_identification`, `correlation`, `incidents`,
`impact`, `prioritisation`, `routing`, `recovery`, `verification`, `notification`, `audit`,
`reporting`, `discovery`, `insights`.

They never import one another — enforced by an import-linter independence contract. Each
declares its own `ports.py`: 70 port interfaces in total, most in `correlation` (10).

## The lifecycle (`domain/state_machine.py`)

States: `received → validated → enriched → located → correlated → diagnosing → prioritised
→ routed → notifying → mitigating → verifying → resolved → closed`, plus `reopened`,
`human_review` and `suppressed`.

Transitions are declared in one tuple and tested; there are no ad-hoc status writes.

## The 24 domain events (`domain/events.py`)

`ComplaintReceived`, `ComplaintValidated`, `ComplaintEnriched`, `SiteIdentified`,
`Correlated`, `DegradationDetected`, `IncidentDetected`, `IncidentUpdated`,
`ImpactCalculated`, `DiagnosisCompleted`, `CasePrioritized`, `CaseRouted`, `CaseClosed`,
`CaseReopened`, `NotificationProposed`, `NotificationApproved`, `NotificationRejected`,
`NotificationSent`, `NotificationSuppressed`, `RecoveryRecommended`, `RecoveryApproved`,
`VerificationCompleted`, `HumanReviewRequested`.

## Correlation — the eleven rules (`modules/correlation/service.py`)

**One engine, two entry points.** This is the part the demo under-sold.

- `detect_degradations(window)` is **network-first**: it scans KPI health, collapses alarm
  storms, walks the topology and raises incidents **whether or not anybody complained**.
- `correlate_complaint(observation)` is **customer-first**: a located complaint joins the
  incident covering its event time, or starts one with enough corroboration.

The rules, verbatim in intent from the module docstring:

1. **Spatio-temporal clustering** — complaints close in space and time are one event,
   normalised by the cell's subscriber estimate, so a busy cell must shout louder than a
   rural one.
2. **Topology-aware joining** — degradation on cells sharing an upstream element is one
   incident rooted at that element. The difference between "nine sites are broken" and "one
   transmission link is down".
3. **Maintenance suppression** — an approved window on the element or any ancestor means
   this was expected. Suppression is recorded, never silent.
4. **Alarm-storm collapsing** — alarms on related elements inside the collapse window are
   links on one incident, not N incidents.
5. **Flapping** — enough raise/clear transitions in an hour with no KPI impact is a hardware
   nuisance, not a customer incident.
6. **False-alarm rejection** — an alarm with healthy KPIs and nobody complaining is
   rejected. Silence plus health means nothing is wrong.
7. **Non-network rejection** — a billing surge is never a network incident, however big.
8. **Silent sufferers** — KPI degradation past the policy threshold raises an incident with
   **zero complaints**.
9. **Silence as signal** — a complaint rate that *collapses* against the cell's own baseline
   while KPIs collapse is evidence of an outage, not of health.
10. **Duplicate storms** — complaints sharing a dedupe key count once.
11. **Late events** — event time decides the window; a complaint may attach to an already
    closed incident, returning a reopen signal rather than a second incident.

Every outcome writes exactly one `DecisionLog` row stamped with the rule version.

Constants that matter: `KPI_BREACH_MIN_SEVERITY = 0.30`, `FLAPPING_WINDOW = 1 hour`,
`SILENCE_MIN_BASELINE_PER_HOUR = 0.25`.

## Notification — seven gates (`modules/notification/service.py`)

> "The product value is telling a customer about an outage before they call. The risk is
> telling two million customers something wrong at 03:00."

The service is "a sequence of refusals with a narrow path through the middle":

1. Authorise the caller — capability, decided server-side.
2. **Storm guard** — a system-wide ceiling on proposals per minute.
3. **Quiet hours** — evaluated in the policy's timezone against a UTC instant.
4. **Per-subscriber gates** — opt-out, per-incident cap, daily cap, dedupe window.
5. **Render from an approved template** — never free text.
6. **Human approval, with separation of duties** — enforced in code: if the policy names two
   approvers, the proposer cannot approve their own proposal.
7. Send through the SMS port, mock mode only in v1.

Every refusal publishes `NotificationSuppressed` and writes a decision row: **a message that
was not sent is as auditable as one that was.**

Templates (`modules/notification/templates.py`) require **both English and Swahili**. Slot
values are sanitised: template syntax, angle brackets and backticks are forbidden inside a
slot, and the renderer detects Kenyan MSISDN shapes and long digit runs so an identifier
cannot be injected through a slot.

## Roles and capabilities (`core/security.py`) — the demo had this WRONG

Five roles: `care_agent`, `noc_engineer`, `analyst`, `manager`, `admin`. Twenty-one
capabilities. The correct per-role sets:

| Capability | care_agent | noc_engineer | analyst | manager | admin |
| --- | :-: | :-: | :-: | :-: | :-: |
| `incident:read` | yes | yes | yes | yes | yes |
| `incident:approve` | | yes | | yes | yes |
| `incident:write` | | yes | | | yes |
| `complaint:read` | yes | yes | yes | yes | yes |
| `complaint:write` | yes | | | | yes |
| `subscriber:lookup` | yes | | | | yes |
| `notification:propose` | yes | yes | | | yes |
| `notification:approve` | | yes | | yes | yes |
| `recovery:recommend` | | yes | | | yes |
| `recovery:approve` | | | | yes | yes |
| `insights:read` | | yes | yes | yes | yes |
| `explorer:query` | | | yes | | yes |
| `explorer:export` | | | yes | | yes |
| `catalog:read` | | yes | yes | yes | yes |
| `report:read` | | yes | yes | yes | yes |
| `report:send` | | | | | yes |
| `ai:ask` | yes | yes | yes | yes | yes |
| `ai:diagnose` | | yes | yes | | yes |
| `ai:quality` | | | yes | yes | yes |
| `audit:read` | | | | yes | yes |
| `admin:manage` | | | | | yes |

**Correction needed in the demo:** a care agent *can* see Command Center (`incident:read`)
and Complaints. A manager *can* see Complaints. The demo hid both.

Console nav gating (`web/src/components/Shell.tsx`): Command Center `incident:read`,
Complaints `complaint:read`, Care lookup `subscriber:lookup`, Data Explorer
`explorer:query`, Insights `insights:read`, Reports `report:read`, AI quality `ai:ask`.

## AI layer (`ai/`)

- `llm/gateway.py` — the single egress to a model. Owns the capability probe, timeouts,
  retries, circuit breaker, cache, token and cost accounting, redaction, prompt versioning.
  A source-level test asserts no other module imports an HTTP client.
- `llm/schemas.py` — every model response is validated against a Pydantic model; invalid
  means one repair attempt, then a deterministic fallback.
- `llm/redaction.py` — identifier masking before the prompt is built.
- `agents/` — `harness.py` (containment, budgets, tiers), `tools.py` (the typed registry),
  and three agents: `diagnosis.py`, `knowledge.py`, `reporter.py`.
- `ml/models/` — `baseline.py`, `forecast.py`, `anomaly.py`, `priority.py`, `scaffolds.py`
  (the GNN and bandit, registered and refusing to run).
- `ml/registry.py`, `ml/features.py`, `ml/evaluation.py`.
- `rag/` — retrieval for the knowledge assistant.

## Adapters (`infrastructure/adapters/`)

Only `teams` and `sms` are separate adapter packages; the database, LLM and vector store
live under `infrastructure/db/`, `ai/llm/` and `infrastructure/vectorstore/`. The claim
"every external system has a mock and a live stub" holds for the ports, but the demo should
not imply a large adapter directory that does not exist.

## Worker (`worker/`)

`consumers.py`, `pipeline.py`, `scheduler.py`, `replay.py`. Domain events are written in the
same transaction as the state change (transactional outbox); the worker polls, dispatches
idempotently, and `replay.py` makes any incident reproducible.
