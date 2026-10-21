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
