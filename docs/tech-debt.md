# Technical debt register

Each item: what, why it is acceptable now, what would make it urgent, and where it is
tracked. Updated at every phase gate (PRD §17).

| # | Debt | Why acceptable now | Escalate when | Owner / ref |
| --- | --- | --- | --- | --- |
| 1 | **Integration adapters are mocks.** CRM, ServiceNow, NMS, HLR, CBS, Atoll, SMS, social are mock + live_stub only. | v1 explicitly does not integrate; contract tests pin the interface. | Pilot with real care data is scheduled. | Phase 2/6 adapters |
| 2 | **SQLite is the default backend.** Oracle-specific defects can pass local gates. | Shared contract tests across both adapters; Oracle suite runs when Docker is up. | Before any live deployment: run `make test-it` in CI against Oracle Free. | ADR 0002 |
| 3 | **Eval thresholds are fake-LLM baselines.** Real Qwen numbers are unknown. | ADR 0007; `make eval-live` is wired and skips cleanly. | Credentials for the internal endpoint arrive. | ADR 0007 |
| 4 | **Closure-code label quality is unknown.** Historical care closure reasons are noisy in every operator. | Demo data is synthetic and labelled by construction. | Training on real history begins. | Phase 3 model cards |
| 5 | **Dual state with ServiceNow.** Ownership of incident truth is undecided. | TAZAMA is advisory in v1; no writes leave the app schema. | Two-way sync is requested — then decide ownership and use the outbox. | ADR 0003 |
| 6 | **Event schema evolution.** Versioned and additive-only, but no registry or consumer-compat tests yet. | Single deployable, one team. | A second consumer or an external subscriber appears. | Phase 2 |
| 7 | **LLM prompt sprawl.** Prompts multiply as tasks grow. | Versioned directories + evals per prompt. | Prompt count outgrows the eval set. | `ai/llm/prompts/` |
| 8 | **Knowledge staleness.** Docs carry owner and expiry; expired docs are excluded, but nothing chases owners. | Retrieval refuses stale evidence. | Knowledge base exceeds a few dozen docs. | Phase 4 RAG |
| 9 | **Privacy review pending.** DPIA is a template, not a signed-off assessment. | Synthetic data only; pseudonymisation at the boundary (ADR 0008). | Any real subscriber data enters. | `docs/dpia.md` |
| 10 | **KPI baselines unverified.** Seeded KPI names and thresholds are modelled from public/industry sources, not from the operator's own counters. | Clearly labelled UNVERIFIED in the data catalog. | First live mapping — reconcile with the real counter definitions. | `docs/data-catalog.md` |
| 11 | **ML feedback loops.** Models trained on data shaped by their own suppression decisions. | Explicit exploration slice retained; decisions logged with versions. | Automated action on model output. | Phase 3 |
| 12 | **Oracle-version-dependent features.** VECTOR, property graph, native hybrid search sit behind capability probes. | Probes degrade gracefully, and the probe result is visible in `/readyz`. | Target version confirmed — then simplify the code path. | ADR 0005 |
| 13 | **In-memory vector backend is not production sized.** Linear scan, process-local. | Demo volumes only. | Knowledge corpus or incident history grows past a few thousand chunks. | ADR 0005 |
| 14 | **No GNN / bandit.** Scaffolds exist and are disabled. | They must beat LightGBM on the eval set first (PRD §9). | A baseline comparison shows a real gain. | Phase 3 |
| 15 | **Real Teams send is unproven.** Only the documented Workflows payload shape is contract-tested; `TEAMS_DRY_RUN=true` by default. | No webhook available; dry-run renders and previews the card. | A webhook is provisioned — send one test card, then keep dry-run for non-prod. | Phase 6 |

## Added in Phase 2

| # | Debt | Why acceptable now | Escalate when | Owner / ref |
| --- | --- | --- | --- | --- |
| 16 | **Four correlation thresholds are module constants, not policy** (`kpi_breach_min_severity`, `topology_degraded_share`, `silence_drop_ratio`, `silence_min_baseline_per_hour`). | They are tuned against labelled ground truth and documented in the Phase 2 report. | Anyone outside the build team needs to change detection sensitivity — then they belong in `config/policies.yaml`. | `modules/correlation/service.py` |
| 17 | **`flapping_min_transitions: 4` contradicts the seeded SOP**, which defines flapping as six or more raise/clear cycles per hour (twelve transitions). | The seeded scenario trips either threshold, so no gate depends on the discrepancy. | Before live use: make the policy key and the SOP agree, and say whether the unit is cycles or transitions. | `config/policies.yaml` |
| 18 | **`min_complaints_per_1k_subs` is inert for an average cell** — the absolute count of 3 binds first below ~3,750 subscribers. | Exposure normalisation still works where it matters, on large cells. | Exposure is expected to gate typical cells — then raise it to ~1.2. | `config/policies.yaml` |
| 19 | **`incident` has no `window_end` column**, so a persisted incident's event window is reconstructed as `started_at + 6h` when unresolved. | Incident identity buckets on window *start*, so idempotency is unaffected. | Incidents routinely run longer than six hours, which would split one fault into two. | `infrastructure/db/incident_repo.py` |
| 20 | **`notification.proposed_by` and `rejected_reason` have no columns**; they live on the audit row. | The audit row is the right system of record, and approval happens in the same process. | Proposal and approval move to different processes — then add the columns. | `infrastructure/db/notification_repo.py` |
| 21 | **Verification persists nothing.** The service returns a result and a transition; no row is written to `verification`. | The caller owns the transaction that writes both the state change and its events. | The Command Center needs verification history (Phase 5/6). | `modules/verification` |
| 22 | **Adapter constructors use two opposite conventions** — `(session, *, engine)` in ingestion/topology/correlation, `(engine, *, session)` in notification/recovery. | The composition root passes `session=` by keyword everywhere, so the difference cannot bite at runtime. | Anyone constructs an adapter positionally outside the composition root. | `infrastructure/db/*_repo.py` |
| 23 | ~~**Pipeline consumers are not wired into the worker yet.**~~ **RESOLVED in Phase 3**: `build_consumers` now registers the pipeline handlers, and `tests/scenarios/test_pipeline_end_to_end.py` drives one complaint through the whole chain via the outbox. | — | — | `worker/consumers.py` |
| 24 | **SSE fan-out is per API process.** Each replica tails the outbox independently. | Correct for the browser: it only needs *a* live update. | Ordering guarantees across replicas are ever required. | `api/stream_feed.py` |

## Added in Phases 3–6

| # | Debt | Why acceptable now | Escalate when | Owner / ref |
| --- | --- | --- | --- | --- |
| 25 | **No erasure procedure.** Deleting a pseudonym token must cascade to complaints, care calls, incident links and analytics. It does not. | Synthetic data only; nothing to erase. | **Blocking** before any real subscriber record enters any environment. | `docs/dpia.md` §7 |
| 26 | **No salt-rotation procedure.** Rotating `PSEUDONYM_SALT` silently breaks the link between historical analytics and current subscribers. | The salt has never changed. | Before the first rotation, or on any suspicion of salt compromise. | `core/security.py`, `docs/dpia.md` R8 |
| 27 | **Re-identification risk in the insights surface.** A per-cell complaint rate at 03:00 on a twelve-subscriber cell is close to identifying. | Synthetic data; aggregates are normalised. | **Blocking** before real data — needs a minimum-exposure floor below which a cell is not reported individually. | `modules/insights`, `docs/dpia.md` R3 |
| 28 | **Nothing sends reports on a schedule.** `due_reports` is pure and tested; no worker job calls it. | Deliberate: sending on a timer with no webhook and no approval step would be a loaded gun. | A webhook is provisioned and a test card has been sent. | `modules/reporting/schedule.py` |
| 29 | **The forecast is region-grain and one hour ahead.** Cell-level forecasting is not possible on this target (97.6% zeros) and care staffing is often planned a shift ahead. | Region grain is the right unit for staffing, and the one-hour horizon is honest about what the data supports. | A longer horizon is needed — then the target needs rebuilding, not the model retuning. | `docs/phase-reports/PHASE_3.md` §3 |
| 30 | **The priority model can only imitate the existing rule**, because its labels are distilled from that rule. | It is registered but the rule remains in control. | Real outcome labels exist (resolution time, repeat-contact rate). | `ai/ml/models/priority.py` |
| 31 | **No drift monitoring runs.** PSI is implemented and tested; nothing schedules it. | Models are retrained on demand, not continuously. | A model is left active across a month of live data. | `ai/ml/evaluation.py` |
| 32 | **No hybrid retrieval.** `search_knowledge` is a simple search with citation verification; the vector backend is a process-local linear scan. | The corpus is a handful of documents, and citation integrity is what makes answers safe. | The knowledge corpus passes a few hundred chunks. | ADR 0005, tech-debt #13 |
| 33 | **Retrieval faithfulness, answer relevancy and context precision are not measured.** What is tested instead is citation integrity. | The numeric thresholds need a real corpus and a judge model. | Real-model evals are run. | `docs/phase-reports/PHASE_4.md` §8 |
| 34 | **No component unit tests in the frontend.** vitest is installed and configured with zero tests. | Playwright journeys cover behaviour; the a11y suite covers markup contracts. | The component surface grows past the design system. | `web/package.json` |
| 35 | **No console i18n.** Customer-facing templates are bilingual (en/sw); the operator console is English only. | The console is an internal tool. | Care agents who prefer Swahili use the console directly. | `web/src` |
| 36 | **React 18 is deprecated in Next 16** and unsupported in Next 17. | Next 16 still supports it and the console works. | Before the Next 17 upgrade. | `web/package.json` |
| 37 | **No load test has been run.** `make load` exists; no p99 figure for the incident list exists. | Demo-sized data on one machine. | Before a pilot with real traffic. | `backend/tests/load/` |
| 38 | **`pip-audit` was not run** in the Phase 6 pass. `npm audit` is clean (0 vulnerabilities). | Lockfiles are pinned. | Every release. | `make audit` |
| 39 | **`make demo` has not been rehearsed from a fresh clone with empty volumes.** It runs here from a working tree with a seeded database. | The individual steps are each exercised. | Before handing the repo to someone else to run. | `scripts/mk.py` |
| 40 | **The threat model and DPIA are unreviewed**, and the audit log has no review routine. An append-only log nobody reads is evidence after the fact, not a control. | Synthetic data; build-team analysis. | **Blocking** before real data. | `docs/threat-model.md`, `docs/dpia.md` |
