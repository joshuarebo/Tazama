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
| 23 | **Pipeline consumers are not wired into the worker yet**, so the chain runs when invoked rather than autonomously. | Every stage is tested in isolation and through the API; the scenario gate drives the engine directly. | Phase 3 integration — then wire `build_consumers` to the module services. | `worker/consumers.py` |
| 24 | **SSE fan-out is per API process.** Each replica tails the outbox independently. | Correct for the browser: it only needs *a* live update. | Ordering guarantees across replicas are ever required. | `api/stream_feed.py` |
