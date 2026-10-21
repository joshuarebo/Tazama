# TAZAMA — Smart Optimization Intelligence Platform (PRD v1)

## 0. Instructions to the building agent
- This document is the source of truth. If something is ambiguous, state your assumption in the phase report; do not guess silently.
- Build in phases. Do not start a phase until the previous gate is green.
- Synthetic data only. Never invent real endpoints, credentials or customer data.
- Prefer boring, proven technology. Every new dependency needs a one-line justification in docs/adr/.

## 1. Problem (first principles)
Care and network operations are reactive: customers call after they have already suffered. The network sees degradation first; complaints are a lagging confirmation. Target journey: Network degradation + Customer complaint -> Correlation -> Incident -> Impact -> Diagnosis -> Priority/Routing -> Proactive customer communication -> Recovery (human-approved) -> KPI verification -> Learning.
Business value: fewer avoidable calls, lower cost per contact, faster MTTR, lower churn, SLA protection for enterprise customers.
Key risk to design around: the join between a complaint and a network element (cell/site) is the hardest problem. Measure it from day one.

## 2. Goals / Non-goals
Goals: (1) explore data safely, (2) correlate calls/complaints with network KPIs, (3) detect and explain incidents, (4) recommend actions with evidence, (5) proactive notifications (approved), (6) Teams reporting, (7) auditable and explainable.
Non-goals (v1): replacing CRM/ServiceNow/NMS; AI executing network commands; real SMS sending; Daraja/M-Pesa integration (separate project; later optional signal adapter); live social listening.
Minimum Lovable Product: one region, demo data, with these experiences:
- Care agent looks up a subscriber and sees "known issue in your area, ETA X".
- Customer notification drafted from approved templates, human-approved.
- NOC engineer sees one incident card (map, alarms, KPI charts, probable root cause with evidence, similar incidents, suggested runbook).
- Analyst sees call-vs-network correlation insights and a safe SQL data explorer.
- Manager sees KPI dashboard and a Teams digest.

## 3. Users and roles (RBAC)
care_agent, noc_engineer, analyst, manager, admin. Least privilege; every endpoint declares required roles. AUTH_MODE=dev (local users) or oidc (Entra ID/OIDC).

## 4. Operating modes and environment contract
APP_MODE=demo (seed data, writes allowed) | live (read-only against source mappings; writes to app schema only; ALLOW_LIVE_WRITES=false default).
ALL configuration comes from environment variables via a single pydantic-settings module. No hardcoded URLs, credentials, hostnames or model names anywhere. App fails fast at startup with a clear message listing missing/invalid variables. Provide .env.example (no real values), validate it in CI, ensure .env is gitignored.
Variables (group, name):
- App: APP_ENV, APP_MODE, LOG_LEVEL, DISPLAY_TZ=Africa/Nairobi (store UTC always), ALLOW_LIVE_WRITES
- Oracle (app schema): ORACLE_USER, ORACLE_PASSWORD, ORACLE_DSN, ORACLE_POOL_MIN, ORACLE_POOL_MAX, ORACLE_WALLET_DIR (optional)
- Oracle (live read-only source): SOURCE_ORACLE_USER, SOURCE_ORACLE_PASSWORD, SOURCE_ORACLE_DSN, SOURCE_QUERY_TIMEOUT_S, SOURCE_MAX_ROWS
- LLM (internal Qwen): LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_REASONING, LLM_MODEL_FAST, LLM_TIMEOUT_S, LLM_MAX_RETRIES, LLM_TOOL_CALLING=auto|on|off
- Embeddings: EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL, VECTOR_BACKEND=oracle|memory
- Teams: TEAMS_WORKFLOW_WEBHOOK_URL (treat as a secret), TEAMS_DRY_RUN=true
- Security: PSEUDONYM_SALT, AUTH_MODE, OIDC_ISSUER, OIDC_CLIENT_ID, OIDC_CLIENT_SECRET, CORS_ORIGINS
- Integrations: SMS_GATEWAY_MODE=mock, SOCIAL_SIGNALS_ENABLED=false
- Observability: OTEL_EXPORTER_OTLP_ENDPOINT
- Web: NEXT_PUBLIC_API_BASE_URL, NEXT_PUBLIC_MAP_TILE_URL

## 5. Architecture
Principles: API-first; event-driven internally; modular monolith with strict module boundaries (modules talk only via interfaces and domain events); ports-and-adapters (every external system is an interface with mock + live-stub implementations); deterministic where possible, AI where necessary; AI recommends, rules decide, humans approve high-risk actions; secure and observable by design.
Processes (docker-compose): oracle (Oracle Database Free container), api (FastAPI), worker (event consumers + scheduler), web (Next.js). Profiles: observability (otel-collector + trace UI), fake-llm (deterministic mock LLM server for tests/offline).
Stack and why:
- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + python-oracledb (thin mode), Alembic, uv for dependency management. Reason: team skills, async, strong typing, data/ML ecosystem.
- Events: transactional outbox table in Oracle + worker polling, behind EventBus interface; Kafka adapter stubbed for later. Reason: no extra infra for MVP; idempotent, replayable.
- Workflow: explicit persisted state machine for the complaint/incident lifecycle (documented transitions, tested). Temporal deferred (ADR).
- Topology graph: relational node/edge tables + recursive queries behind a TopologyRepository interface. Optional Oracle property graph later (ADR; verify version).
- Vector/RAG: Oracle AI Vector Search when VECTOR is available; capability probe at startup; fallback to in-memory FAISS-style backend (VECTOR_BACKEND=memory). Hybrid retrieval = Oracle Text/BM25 + vectors + reranker; if native hybrid is unavailable, fuse in application code.
- ML: scikit-learn, LightGBM, statsmodels, SHAP, scipy, umap-learn (visualization only).
- Agents: LangGraph for bounded stateful graphs behind an AgentRuntime interface.
- Frontend: Next.js (App Router) + TypeScript + Tailwind + a small in-house design system; ECharts for charts; MapLibre for maps (tile URL from env; works offline with blank base); TanStack Query; SSE for live updates; i18n scaffold (English, Swahili).
- Observability: OpenTelemetry traces/metrics/logs, structured JSON logs, correlation IDs.

## 6. Repository structure
```
tazama/
  CLAUDE.md  PRD.md  Makefile  docker-compose.yml  .env.example  .gitignore  .pre-commit-config.yaml
  docs/ { adr/, architecture/, runbooks/, phase-reports/, model-cards/, threat-model.md, data-catalog.md, tech-debt.md }
  config/ { source_mappings.yaml, policies.yaml, runbooks.yaml, taxonomy.yaml, notification_templates/ }
  scripts/ { doctor.py, seed.py, replay.py, export_openapi.py }
  backend/
    pyproject.toml  alembic/
    src/tazama/
      core/            settings, logging, errors, security, tracing, ids, clock
      domain/          entities, events, value_objects (pure, no I/O)
      modules/         ingestion, complaints, enrichment, site_identification, correlation, incidents,
                       impact, prioritisation, routing, recovery, verification, notification, audit,
                       reporting, discovery, insights   (each: service.py, ports.py, models.py, tests next to code)
      ai/
        llm/           gateway, capability_probe, schemas, prompts/ (versioned), cache, redaction
        agents/        harness/, tools/, diagnosis/, knowledge/, reporter/
        rag/           router, hybrid, graph, grader, ingest
        ml/            features, models, training, registry, evaluation
        evals/         datasets/, metrics/, runners/
      infrastructure/  db/oracle/, db/memory/, eventbus/, vectorstore/,
                       adapters/{crm,servicenow,nms,hlr,cbs,atoll,sms,teams,social}/{mock,live_stub}
      api/             v1/routers, deps, middleware, schemas
      worker/          consumers, scheduler
    tests/             unit, integration, contract, scenarios, evals, fakes, api_e2e, load
  web/
    src/ { app/, components/design-system/, features/, lib/, i18n/ }  tests/ { unit, e2e }
```

## 7. Data layer
Oracle specifics (encode in infrastructure/db/oracle only): empty string equals NULL; keep identifiers <= 30 chars; IDENTITY columns; store UTC timestamps; explicit NLS handling; CLOB/JSON handling (IS JSON constraints); always bound parameters; partition-friendly, time-bounded queries; connection pooling with health checks; never SELECT * on large tables.
Canonical domain tables (app schema): SUBSCRIBER (pseudonymised), SITE, CELL, TOPOLOGY_EDGE, ALARM, KPI_CELL (15-min and hourly), COMPLAINT, CARE_CALL (with reason), INCIDENT, INCIDENT_LINK, DIAGNOSIS, CASE_RECORD, NOTIFICATION, MAINTENANCE_WINDOW, RUNBOOK, KNOWLEDGE_DOC (+vector), PREDICTION, MODEL_REGISTRY, EVAL_RUN, AUDIT_LOG, OUTBOX_EVENT, SCHEDULED_REPORT.
Source-mapping layer: config/source_mappings.yaml maps real Oracle tables/columns to the canonical model (names, types, join keys, timezones, filters). Demo mode maps to seed tables; live mode to real tables, with NO code change. Validate mappings on startup and report coverage.
Seed generator (deterministic, fixed random seed, Kenya-realistic): ~5 regions (Nairobi, Mombasa, Kisumu, Nakuru, Eldoret), ~200 sites, ~600 cells, topology (core -> aggregation -> transmission link -> site -> cell), 30 days of KPIs with daily/weekly seasonality, ~20k complaints (English, Swahili, Sheng, code-switched; channels: call centre, One App, SMS, USSD, email, social), care calls with reasons, subscribers with pseudonymised IDs and varied MSISDN formats.
Seeded scenarios with ground-truth labels: single-cell outage; transmission link failure cascading to many sites; evening congestion; flapping alarm; false alarm (no complaints); planned maintenance (suppress); non-network surge (billing/data bundle confusion); outage that silences complaint channels; "silent sufferers" (degraded KPIs, no complaints); duplicate complaint storms; late/out-of-order events.
Data Discovery module (first-class, built early):
- Schema catalog + profiler: tables, columns, row counts, null %, cardinality, min/max, freshness, join-key coverage; output docs/data-catalog.md and an API.
- Join coverage report: % of complaints mappable to a cell, to a site, to an incident; time alignment checks.
- Safe SQL explorer: SELECT-only, parsed and validated (reject DML/DDL/PL-SQL/multiple statements), allowlisted schemas/tables, mandatory row cap and timeout, read-only DB user in live mode, query history and audit, PII columns masked by default, EXPLAIN-first warning for large scans. Include injection and bypass tests.
- Saved queries and CSV export (masked).

## 8. Backend modules and API
REST /api/v1, OpenAPI published, problem+json errors, pagination, idempotency keys on writes, rate limiting, request correlation IDs, SSE stream for incident updates.
Core flow (events): ComplaintReceived -> Validated -> Enriched -> SiteIdentified -> Correlated -> IncidentDetected/Updated -> ImpactCalculated -> DiagnosisCompleted -> CasePrioritized -> CaseRouted -> NotificationProposed -> NotificationApproved -> RecoveryRecommended -> VerificationCompleted -> CaseClosed. Also DegradationDetected (network-first entry point feeding the same incident engine).
Module notes:
- Ingestion: canonical versioned event schemas; MSISDN normalisation (+2547xx, 07xx, 01xx, 2547xx); dedupe; idempotency.
- Site identification: stated location + recent serving cell + topology; returns confidence; low confidence routes to human.
- Correlation: spatio-temporal clustering plus topology-aware joins (shared upstream element); maintenance-window suppression; alarm-storm collapsing.
- Impact: affected subscribers, enterprise accounts, services, SLA risk.
- Prioritisation/routing: transparent rules + (later) ML score; every decision stores inputs, rule/model version, explanation.
- Notification: approved templates only; LLM may fill slots, never write free-form text; dedupe per subscriber, rate limits, quiet hours, opt-out, human approval; SMS gateway is a mock in v1.
- Recovery: runbook catalogue and policy engine; execution simulated only; approval required for high risk; rollback modeled.
- Verification: KPI/alarm recovery plus quiet-period check before closure; failure reopens.
- Audit: append-only, every decision/action/AI output with actor, inputs, versions, confidence.

## 9. AI / ML / Agentic design
Order of operations: classical first, deep learning only where it beats baselines, agents only where multi-step reasoning is needed.
Analytics and ML (insights module):
- Normalise: calls per 1,000 subscribers per cell/hour; de-seasonalise.
- Exploration: PCA (scree, loadings), correlation heatmaps, lagged cross-correlation, Granger-style tests, event studies around known outages, mutual information. UMAP/t-SNE for visualization only; never for feature selection. Variance is not relevance: use supervised importance (LightGBM + SHAP) to rank drivers.
- Models: call-surge forecast (seasonal-naive baseline vs LightGBM, time-based validation only), KPI anomaly detection, incident impact/priority scoring, subscriber call-propensity (design + scaffold; enabled only if data supports).
- Deep learning scaffold (disabled by default): spatio-temporal GNN over topology; enabled only if it beats LightGBM on the eval set. RL scaffold (disabled): contextual bandit for notification timing using offline policy evaluation; no RL on the live network, ever.
- Registry: simple file/DB model registry, model cards in docs/model-cards, reproducible training scripts, drift checks.
LLM usage (Qwen, OpenAI-compatible):
- Gateway: single module; reads base URL/key/model from env; startup capability probe (tool calling? JSON schema? reasoning parser?). If tool calling is unavailable, fall back to schema-constrained JSON and an explicit tool loop. Timeouts, retries with backoff, circuit breaker, response cache, token/cost accounting, PII redaction before prompts and logs, prompt versioning.
- Every LLM output is validated against a Pydantic schema; invalid output -> repair attempt once -> deterministic fallback. System keeps working when the LLM is down; UI shows an "AI degraded" badge.
- Tasks: complaint classification and call-reason taxonomy (multilingual: English/Swahili/Sheng), sentiment/urgency, incident summaries, knowledge Q&A, diagnosis explanation. Bootstrap labels with the LLM, then train a small classifier for volume.
- LLMs never: execute runbooks, write network commands, send customer messages, or see raw MSISDNs.
RAG routing (adaptive router):
- SOP/runbook Q&A -> advanced hybrid RAG (BM25 + vector + reranker + metadata filters), citations required.
- Similar incidents -> structured filters then semantic similarity.
- Root cause -> topology graph tools + agentic loop.
- Corrective step: a grader scores retrieved evidence; if weak or stale, call live tools or escalate to a human.
- Knowledge docs have owner and expiry; expired docs are excluded from retrieval.
Agents (only these three; all bounded):
- DiagnosisAgent: read-only tools (kpi_query, alarm_query, topology_lookup, similar_incidents, maintenance_lookup); max steps/tokens/time budget; must return structured output (probable cause, confidence, evidence list, recommended runbook ID); low confidence routes to a human.
- KnowledgeAssistant: hybrid RAG with citations; refuses when evidence is insufficient.
- Reporter: composes digests; numeric-grounding verifier ensures every number in the text exists in the source data; otherwise rejects and falls back to a deterministic template.
Harness (the real engineering):
1) Tool orchestration: typed tool registry (Pydantic schemas), permission tiers (read / propose / never), argument validation, timeouts.
2) Verification loops: citation check, numeric grounding, schema validation, confidence thresholds -> human review.
3) Context and memory: context assembled with provenance; per-incident scratchpad only; no cross-subscriber memory.
4) Guardrails: complaint text is untrusted input (prompt-injection defences: delimiting, instruction hierarchy, no tool access derived from text, output filters); policy engine; allowlists.
5) Observability: trace every prompt, tool call, retrieval and decision; version prompts and models.
Evals (CI gate, `make eval`): golden datasets generated from seeded scenarios: classification accuracy, diagnosis top-k accuracy vs ground truth, retrieval faithfulness >= 0.9, answer relevancy >= 0.85, context precision >= 0.8, injection-resistance suite, fallback suite. Deterministic fake LLM server for unit/integration tests; real Qwen evals run separately and are non-blocking until baselines are set.

## 10. Frontend and design system
Goal: look and feel aligned with Safaricom's web identity (clean, green-forward, friendly, high-contrast) without copying protected assets.
- Design tokens in web/src/styles/tokens.css only (color, type, radius, spacing, shadows) so the official brand values can be dropped in. PLACEHOLDER tokens: primary green (approx. #43B02A, UNVERIFIED), dark neutral, light neutral, danger/warn/info. Add a TODO banner in tokens.css and docs: replace with official Safaricom brand guideline values.
- Do not fabricate or embed the real Safaricom logo. Use a text wordmark placeholder in web/public/brand/ and document where official assets go.
- WCAG 2.2 AA contrast, keyboard navigation, focus states, reduced motion; light + dark themes; responsive; i18n (en/sw).
- Persistent "DEMO DATA" ribbon in demo mode and "Internal - Confidential" footer.
Pages: Command Center (map + live incidents + KPIs); Incident detail (map, alarms, KPI charts, root cause with evidence, similar incidents, suggested runbook, approve/reject); Complaints explorer; Care Agent lookup (known-issue banner by pseudonymised ID); Insights (call-vs-KPI lead-lag, PCA explorer, driver importance, forecast); Data Explorer (catalog, profiler, join coverage, safe SQL console); Reports (Teams digest preview + send test); AI Quality (evals, drift, LLM health); Admin (users/roles, mappings, audit log, settings); loading, empty and error states everywhere.

## 11. Reporting and Teams
- Teams delivery via a Power Automate Workflows webhook (NOT the retired Office 365 connector). POST an Adaptive Card payload: {"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","content":{... Adaptive Card ...}}]}.
- Reports: daily digest (e.g. 07:30 Africa/Nairobi), P1/P2 incident alerts, weekly management summary. Content: top incidents, calls avoided (pilot vs control), repeat-complaint rate, MTTR, data-quality warnings, link to the portal.
- TEAMS_DRY_RUN=true by default (renders and logs the card, shows preview in UI). Validate card against schema and keep payload well below Teams size limits. Retries with backoff, dead-letter table, idempotent report IDs. No PII in cards. Webhook URL never logged.
- Scheduler in worker; manual "send now" in UI (admin).

## 12. Security, privacy, governance
- Standards alignment: NIST AI RMF (Govern, Map, Measure, Manage) and ISO/IEC 42001 as the AI governance backbone; ISO/IEC 27001 controls; OWASP ASVS (L2 target) and OWASP Top 10 for LLM Applications; Kenya Data Protection Act 2019 (privacy by design, DPIA template in docs; this is not legal advice).
- Secrets: env only; secret scanning in pre-commit/CI (gitleaks); no secrets in logs; .env never committed.
- Data: MSISDN pseudonymised with HMAC + PSEUDONYM_SALT in the analytics and AI layers; raw identifiers never reach LLM prompts or logs; masking by default in UI; retention and deletion rules documented; purpose limitation.
- App security: RBAC, input validation, parameterised SQL only, CSP + security headers, CORS allowlist, rate limiting, non-root containers, pinned dependencies (lockfiles), pip-audit/npm audit, SBOM, threat model (STRIDE) in docs/threat-model.md.
- Governance artifacts: model cards, AI risk register, decision audit trail, human-in-the-loop policy in config/policies.yaml, kill switches (feature flags) for each AI capability.

## 13. Observability and debuggability
- Structured JSON logs with correlation ID, incident ID, module; OpenTelemetry traces across API -> events -> worker -> LLM/tool calls.
- Every decision stores a "why" record (inputs, rule/model/prompt versions, evidence).
- scripts/replay.py replays an event stream or incident deterministically for debugging.
- /healthz, /readyz (DB, LLM probe, vector backend, outbox lag), scripts/doctor.py checks environment and prints actionable fixes.
- Runbooks in docs/runbooks for: DB down, LLM down, outbox backlog, bad mapping, notification storm.

## 14. Testing strategy and quality gates
- Unit (pytest, hypothesis for time-window and normalisation logic), integration (real Oracle Free container), contract tests for every adapter (mock and live-stub share the same tests), scenario tests against seeded ground truth (incident precision/recall thresholds), API e2e, Playwright UI e2e for core journeys, accessibility checks, load tests (k6 or Locust), security tests (SQL guard bypass, prompt injection, authz), LLM evals (section 9).
- Tooling: ruff, mypy (strict on domain/modules), eslint, tsc, pre-commit. `make ci` runs everything except live-LLM evals.
- A phase is "green" only when: tests pass, lint/type checks clean, coverage >= 80% on domain and modules, no secrets detected, docs updated.

## 15. Edge-case register (must have tests)
Duplicate and out-of-order events; clock skew; UTC vs Africa/Nairobi; MSISDN format variants; complaint with no resolvable cell; subscriber moved between cells; roaming; planned maintenance suppression; flapping alarms and alarm storms; outage that suppresses complaints (silence as signal); non-network complaint surges; Swahili/Sheng/code-switched text; empty or very long complaint text; prompt injection inside complaints; LLM timeout, malformed JSON, hallucinated tool arguments, tool-calling unsupported; vector backend unavailable; Oracle version lacking VECTOR; Oracle empty-string/NULL and CLOB pitfalls; pool exhaustion and reconnects; replica lag; notification storms (rate limits, quiet hours, opt-out); webhook failure and retries; live mode accidental write attempts; huge result sets from the SQL explorer; schema drift in source tables (mapping validation catches it).

## 16. Phases and exit gates
Phase 0 Foundations and harness: repo, CLAUDE.md, settings + .env.example, docker-compose (Oracle Free, api, worker, web, profiles), Makefile (up, down, demo, test, lint, seed, eval, ci, doctor), pre-commit, CI, ADRs, fake-LLM server. Gate: `make up` healthy, `make doctor` and `make ci` green.
Phase 1 Data layer, seed, discovery: Alembic migrations, canonical schema, source-mapping layer, deterministic seed with all scenarios, profiler, join coverage report, safe SQL explorer API. Gate: seed reproducible; SQL guard tests pass; catalog generated; join coverage reported.
Phase 2 Core pipeline: ingestion -> incidents -> impact -> prioritisation/routing -> notification proposals -> verification, outbox/event bus, audit, SSE. Gate: scenario tests meet precision/recall thresholds on seeded incidents; replay works.
Phase 3 Insights and ML: normalisation, lead-lag/Granger/event studies, PCA explorer, driver importance, call-surge baseline + LightGBM, anomaly detection, registry, model cards. Gate: beats naive baseline on seed; reproducible training; leakage tests pass.
Phase 4 LLM harness and agents: gateway + probe, classification/taxonomy, RAG (router, hybrid, grader), DiagnosisAgent, KnowledgeAssistant, Reporter with numeric grounding, eval suite, injection suite. Gate: eval thresholds met with fake and (if configured) real Qwen; degraded-mode tests pass.
Phase 5 Frontend: design system, all pages, SSE, i18n, a11y, Playwright journeys. Gate: e2e journeys pass (care lookup, incident approval, data explorer, insights, report preview).
Phase 6 Reporting, hardening, handover: Teams digest (dry-run + real-send path), notification engine, security hardening, load test, threat model, docs, tech-debt register. Gate: `make ci` green; `make demo` brings everything up from scratch.
Later (not now): live adapters via source mappings, real SMS, social-signal adapter (aggregate public signals only), Kafka/Temporal, GNN/bandits if justified, Daraja/M-Pesa failure signals as an extra input.

## 17. Technical debt to document in docs/tech-debt.md
Integration adapters are mocks; label quality of historical closure codes; dual state with ServiceNow (needs outbox + ownership decision); schema evolution (versioned events); LLM prompt sprawl (mitigated by versioning + evals); knowledge staleness (owners/expiry); privacy review pending; KPI baselines unverified; ML feedback loops (keep an exploration slice); Oracle-version-dependent features behind capability probes.

## 18. Definition of Done
All phase gates green; `make demo` runs end-to-end on a clean machine (migrate, seed, start); docs complete; open http://localhost:3000 and show Command Center with seeded incidents, an incident card with evidence, the Data Explorer, Insights and a Teams digest preview; final report lists assumptions, known gaps and how to switch to LIVE mode (fill .env, edit config/source_mappings.yaml, run `make doctor`).

## 19. CLAUDE.md (create at repo root)
Include: project summary; commands (make targets); architecture rules (module boundaries, no cross-module imports except via ports); config rules (env only, never hardcode); data rules (UTC, pseudonymisation, parameterised SQL); AI rules (LLM via gateway only, schemas on all outputs, no LLM-driven side effects); testing rules (write tests alongside code, never mark done while red); git rules (small commits, conventional messages); "when stuck: write down the assumption and continue, unless it is a true blocker".
