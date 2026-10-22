# Phase 2 — Core pipeline

Date: 2026-10-22 · Status: **GREEN**

## What was built
| Area | Delivered |
| --- | --- |
| Events | `domain/events.py` — 23 versioned event types with deterministic ids derived from the fact, so publishing twice is a no-op and replay reproduces the stream byte for byte; additive-only payloads; correlation id travels with every event |
| Lifecycle | `domain/state_machine.py` — the whole complaint/incident life in one declared table: 41 transitions, each naming the event it emits, illegal ones raising with the allowed set in the message, human review reachable from every live stage, suppression terminal, closure reopenable |
| Policy | `core/policy.py` over `config/policies.yaml`, fail-closed: weights must sum to 1, thresholds must decrease, `templates_only` and `respect_opt_out` cannot be switched off, numeric grounding cannot be disabled, an undeclared action requires approval, an ungranted tool is `never` |
| Outbox | `infrastructure/eventbus/` — events commit in the caller's transaction, at-least-once delivery, deterministic jittered backoff, dead letters after five attempts, operator requeue, lag probe; one poisonous event cannot block its batch |
| Audit | `modules/audit` + append-only sink: actor, inputs, evidence references, confidence and rule/model/prompt versions, scrubbed; the port exposes no update or delete |
| Ingestion | MSISDN normalisation across `+2547xx`/`2547xx`/`07xx`/`01xx` (plus full-width digits and non-breaking spaces), immediate HMAC pseudonymisation, deterministic dedupe that collapses a storm, explicit idempotency keys with body-hash conflict detection, late and clock-skewed events recorded as facts rather than errors |
| Site identification | Three weighted signals — age-decayed serving cell, stated-location name matching, topology tie-break — each in the evidence; below the policy threshold the cell is withheld and `HumanReviewRequested` is published instead |
| Correlation | Spatio-temporal clustering normalised by `subscribers_est`, topology merge on a shared upstream element, maintenance suppression, alarm-storm collapsing, flapping classification, false-alarm and non-network rejection, silent sufferers, silence-as-signal against a per-cell baseline, duplicate-storm collapsing, late attachment by event time |
| Incidents | Deterministic incident identity (element + cause + window bucket) so replay converges, optimistic locking, state changes only through the state machine, idempotent linking, structured similar-incident search, merge that moves links and deletes nothing |
| Impact | Affected cells/sites/subscribers (declared an estimate, not a measurement), enterprise exposure, affected services from the breached KPI families, SLA risk — every number explained in its evidence |
| Prioritisation | Transparent weighted rule with saturation normalisation, per-factor contributions, and three recorded overrides (SLA breach, total outage, silent sufferers); an ML score may blend at most 30% and never replaces the rule |
| Routing | A declared rule table read top to bottom, SLA per priority, enterprise escalation, and a loud fallthrough that parks a case with a human rather than losing it |
| Notification | Approved templates only (en/sw), strict non-evaluating substitution, slot values refused rather than laundered, quiet hours in local time, opt-out, per-incident and per-day limits, a storm guard that trips before anything is written, approval re-checked at send time |
| Recovery | Eight runbooks, simulated outcomes only, modelled rollback, two-approver separation of duties on high risk — and three tests asserting no execution path exists anywhere |
| Verification | Polarity-aware KPI recovery against a pre-incident baseline, alarms cleared, complaint quiet period; all three required, missing data fails closed, failure reopens |
| Worker | Bounded loop with graceful shutdown, consumers, local-time scheduler with idempotent run keys, and deterministic replay by incident or time range |
| API | 34 endpoints: intake, complaint browsing, notifications (propose/approve/reject/send), recovery (recommend/simulate/approve/reject/rollback), audit, data explorer, SSE stream with single-use tickets |

## Gate results
```
$ ./make.ps1 ci
==> lint            ruff clean · import-linter: 5 contracts kept, 0 broken
==> typecheck       mypy: Success, no issues in 126 source files
==> test            1248 passed in 140.57s
==> coverage-gate   domain 99.53% · modules 95.88% · core 94.68%   -> ok
==> eval            fake-LLM + SQL-guard suites pass
==> secrets         clean
GREEN  ci passed

$ ./make.ps1 test -- tests/scenarios          # the Phase 2 gate
confusion matrix (detections = live, non-suppressed incidents)
  labelled positive scenarios : 5      detections       : 17
  detected scenarios          : 5      true positives   : 17
  missed scenarios (FN)       : 0      false positives  : 0
  precision                   : 1.0000  (floor 0.85)
  recall                      : 1.0000  (floor 0.80)

  SCN-01-single-cell-outage          expect incident   detected 1
  SCN-02-transmission-link-failure   expect incident   detected 1    (9 sites -> ONE incident)
  SCN-03-evening-congestion          expect incident   detected 6
  SCN-08-outage-silences-channels    expect incident   detected 2
  SCN-09-silent-sufferers            expect incident   detected 7
  SCN-04-flapping-alarm              expect none       detected 0
  SCN-05-false-alarm                 expect none       detected 0
  SCN-06-planned-maintenance         expect none       detected 0
  SCN-07-non-network-surge           expect none       detected 0
  suppressed records: 2 (flapping, planned maintenance — recorded, never silent)
20 passed
```
Maintenance suppression is 100%. The engine is idempotent: a second run produces identical
incident ids *and* versions. Replay reproduces an incident deterministically, including its
recorded decisions.

Precision is measured over detections and recall over labelled scenarios, which is why
SCN-03 (six independent cells) and SCN-09 (eight) count once for recall and several times
for precision. The reason precision is not luck: **nothing raises an incident without
network-side corroboration**, so a complaint cluster on healthy cells cannot create one.

Test shape: 1,248 — ingestion 114, site identification 89, notification 77, recovery 59,
SMS contract 47, verification 40, correlation 27, incidents 25, impact 21, scenarios 20,
prioritisation/routing 39, events/state machine 37, policy 30, outbox/audit 28,
scheduler/stream 24, stream feed + tickets 19, worker/replay 18, pipeline API 60, plus the
Phase 0–1 suites.

## Defects found and fixed during integration
The parallel build surfaced six real problems, each fixed at the cause rather than worked
around:
1. **The import-linter layer order was inverted** for `infrastructure` and `modules`
   (ADR 0009). An adapter must be allowed to name the port it satisfies.
2. **`domain` imported `core`** for `deterministic_id`. The function is pure, so it moved
   to `domain/identifiers.py` and `core.ids` re-exports it.
3. **The worker pushed into the API's in-process SSE stream** — the wrong direction, and
   impossible across processes anyway. The API now tails the outbox itself
   (`api/stream_feed.py`), so each replica serves its own browsers.
4. **A general-purpose JWT in the SSE query string** (flagged by the security review).
   Replaced with single-use, 30-second, principal-bound tickets issued over an
   authenticated POST and consumed on first use.
5. **"Simulate, then approve" was impossible**: `simulate` set the status to `simulated`
   and `approve` refused anything but `proposed`. Looking at the modelled outcome before
   approving is the intended workflow, so `SIMULATED` is now approvable.
6. **Login minted tokens against the injected domain clock.** A JWT's `exp` is checked
   against wall-clock time by every verifier, so credentials must be minted against it.

## Assumptions (stated, per PRD §0)
1. **Correlation consumes already-located complaints** (`cell_id IS NOT NULL`, ~52% per the
   Phase 1 baseline). The gate therefore scores detection on what the source system
   genuinely knows; closing the other 48% is site identification's job and is measured
   separately.
2. **Severity is the share of breached KPI samples over degraded buckets**, not over the
   whole scan window — averaging would dilute a three-hour outage inside a day-long scan
   until no threshold could fire.
3. **Four correlation thresholds are still module constants** rather than policy:
   `kpi_breach_min_severity` (0.30), `topology_degraded_share` (0.60),
   `silence_drop_ratio` (0.40), `silence_min_baseline_per_hour` (0.25). They are exactly
   the numbers a reviewer will want to argue about, so they belong in
   `config/policies.yaml`; moving them is in `docs/tech-debt.md`.
4. **`flapping_min_transitions: 4` contradicts the seeded SOP**, which says six or more
   raise/clear *cycles* in an hour — twelve transitions. The code counts transitions, which
   is the honest reading of the key, so the policy is three times more trigger-happy than
   the operator's own written rule. Flagged rather than silently changed.
5. **`min_complaints_per_1k_subs: 0.8` is inert for an average cell** (the raw count of 3
   binds first below ~3,750 subscribers). Exposure normalisation only bites on large cells.
6. **No `window_end` column on `incident`**, so a persisted incident's event window is read
   back as `[started_at, resolved_at or started_at + 6h)`. A fault on the same element after
   six hours is a new incident.
7. **`affected_subscribers` is a dimensioning estimate** — the sum of `cell.subscribers_est`
   — stated in the docstring, in the evidence and in the payload, and asserted by a test.
8. **Notification `proposed_by` and `rejected_reason` have no columns**, so they survive on
   the audit row rather than a database round-trip. Approval therefore reads the in-memory
   proposal; proposing and approving in different processes would need those columns.
9. **Verification persists nothing**: it returns a result and the transition, and the caller
   writes both. Nothing writes to the `verification` table yet.
10. **The two sets of adapters took opposite positional conventions**
    (`(session, *, engine)` vs `(engine, *, session)`). The composition root passes the
    session by keyword everywhere; standardising the signatures is in `docs/tech-debt.md`.
11. **SSE fan-out is per API process.** With several replicas each tails the outbox
    independently, which is correct; a shared relay is only needed if ordering across
    replicas ever matters.

## Known gaps (later phases)
* No diagnosis yet — `DiagnosisCompleted` is in the vocabulary and the agent lands in Phase 4.
* Pipeline consumers are not yet wired into the worker, so the chain runs when called
  through the API or a test harness rather than autonomously end to end. Wiring them is the
  first task of Phase 3's integration pass.
* No incidents or care-lookup API yet (Command Center reads them in Phase 5).
* `make test-it` still reports *skipped*: the Docker daemon is down, so the Oracle
  integration suite is unexercised (tech-debt #2).

## Next
Phase 3 — insights and ML: normalisation per 1,000 subscribers, lead-lag and Granger-style
tests, event studies around the labelled outages, PCA explorer, LightGBM driver importance
with SHAP, call-surge forecast against a seasonal-naive baseline, anomaly detection, model
registry and model cards, with leakage tests as the gate.
