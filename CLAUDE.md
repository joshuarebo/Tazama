# CLAUDE.md — TAZAMA

## 1. Project summary
TAZAMA is a Smart Optimization Intelligence Platform for a mobile network operator (Safaricom-shaped,
synthetic data only). It correlates customer complaints and care calls with network KPIs and alarms,
detects and explains incidents, recommends evidence-backed actions, proposes human-approved proactive
customer notifications, and reports to Microsoft Teams. Everything is auditable and explainable.

`PRD.md` at the repo root is the **source of truth**. This file is the operating manual for working in
the repo. If the two disagree, PRD.md wins and the discrepancy goes into `docs/tech-debt.md`.

Shape: modular monolith, API-first, event-driven internally (transactional outbox), ports-and-adapters.
Stack: Python 3.12 / FastAPI / Pydantic v2 / SQLAlchemy 2 / python-oracledb (thin) / Alembic / uv;
Next.js (App Router) + TypeScript + Tailwind; Oracle Database Free in Docker; OpenTelemetry.

## 2. Commands (make targets)
Windows note: GNU `make` may be absent. `./make.ps1 <target>` and `scripts/mk.py <target>` are
equivalent entry points and run the same recipes. Never add a target to one without the others.

| Target | What it does |
| --- | --- |
| `make up` / `make down` | Start / stop the docker-compose stack (oracle, api, worker, web) |
| `make demo` | Clean-machine path: build, migrate, seed, start everything, print http://localhost:3000 |
| `make migrate` | Alembic upgrade head |
| `make seed` | Deterministic synthetic seed (fixed random seed, all labelled scenarios) |
| `make test` | Full pytest suite (unit + integration + contract + scenarios) |
| `make lint` | ruff, mypy, eslint, tsc, format checks |
| `make eval` | LLM/agent eval suite against the fake LLM (real Qwen evals are separate, non-blocking) |
| `make catalog` | Regenerate `docs/data-catalog.md` + join-coverage report |
| `make doctor` | `scripts/doctor.py`: environment, config, DB, LLM, vector-backend checks with fixes |
| `make ci` | Everything `make ci` must cover: lint + types + tests + coverage + secret scan + evals(fake) |
| `make sbom` / `make audit` | SBOM generation; pip-audit + npm audit |

Rule: `make ci` is the gate. A phase is not done until it is green.

## 3. Architecture rules
- **Module boundaries are hard.** `backend/src/tazama/modules/<m>/` may import from `core/`, `domain/`,
  and its own package only. **No cross-module imports.** Modules interact in exactly two ways:
  (a) through a port (an interface declared in the *consumer's* `ports.py`, implemented by an adapter
  wired in the composition root), or (b) by publishing/consuming a domain event.
- `domain/` is pure: entities, value objects, events. No I/O, no SQLAlchemy, no HTTP, no datetime.now().
- Every external system (Oracle, LLM, Teams, SMS, CRM, ServiceNow, NMS, HLR, CBS, Atoll, social) is an
  interface in `ports.py` with at least a `mock` and a `live_stub` adapter. Contract tests run against
  both implementations from the same test file.
- Composition root: `api/deps.py` and `worker/` wire concrete adapters. Nothing else constructs adapters.
- Persisted, explicit state machine for the complaint/incident lifecycle. Transitions are declared in
  one place and tested; no ad-hoc status writes.
- Event flow is the backbone: `ComplaintReceived → Validated → Enriched → SiteIdentified → Correlated →
  IncidentDetected/Updated → ImpactCalculated → DiagnosisCompleted → CasePrioritized → CaseRouted →
  NotificationProposed → NotificationApproved → RecoveryRecommended → VerificationCompleted → CaseClosed`,
  plus `DegradationDetected` as the network-first entry point. Events are versioned and additive-only.
- Outbox pattern: domain events are written in the same transaction as the state change; the worker
  polls, dispatches idempotently, and is safe to replay (`scripts/replay.py`).
- New dependency ⇒ one-line justification in `docs/adr/`. Prefer boring and proven.

## 4. Config rules
- **Env only.** One `pydantic-settings` module (`core/settings.py`) is the only place that reads the
  environment. No hardcoded URL, host, port, DSN, credential, model name, salt or webhook anywhere else
  — not in code, tests, compose files, or the frontend.
- Fail fast: on startup, invalid/missing config raises one error listing every offending variable.
- `.env.example` holds every variable with safe placeholders and no real values. CI asserts that the
  settings model and `.env.example` agree. `.env` is gitignored and never committed.
- `APP_MODE=demo|live`. In `live`, source reads are read-only, writes are restricted to the app schema,
  and `ALLOW_LIVE_WRITES=false` is the default and must be explicitly flipped.
- Feature flags / kill switches for every AI capability live in config, default off where risky.

## 5. Data rules
- **Store UTC always.** Convert to `DISPLAY_TZ` (Africa/Nairobi) only at the presentation edge. Never
  call `datetime.now()` in domain or module code — inject the clock (`core/clock.py`).
- **Parameterised SQL only.** No string interpolation into SQL, ever. The only component allowed to
  accept user SQL is the Data Explorer, and it goes through the SQL guard (SELECT-only, parsed,
  allowlisted tables, row cap, timeout, audit).
- **Pseudonymisation.** MSISDNs are HMAC-pseudonymised with `PSEUDONYM_SALT` before reaching analytics,
  AI, logs, exports, Teams cards or the UI. Raw identifiers never leave the ingestion boundary.
- Oracle specifics stay inside `infrastructure/db/oracle/`: `'' IS NULL`, identifiers ≤ 30 chars,
  IDENTITY columns, explicit NLS, CLOB/JSON (`IS JSON`), bound params, time-bounded partition-friendly
  queries, pooling with health checks, never `SELECT *` on large tables.
- Source mappings (`config/source_mappings.yaml`) are the only place that knows real table/column names.
  Switching demo → live is a config edit, never a code change. Mappings are validated at startup.
- Migrations: Alembic, forward **and** down migration for every revision; no destructive migration
  without an explicit, reviewed note in the revision docstring.

## 6. Security rules (non-negotiable)
The baseline is the 9-layer model in `docs/security/security-baseline.md`; the controls below are the
short version. Security tests live in `backend/tests/security/` and run in `make ci`.
1. **Input validation** — Pydantic schemas at every boundary, length/range bounds, enum allowlists.
2. **Prompt-injection guard** — complaint/user text is untrusted data: delimited, never instruction;
   pattern detectors + canary token in the system prompt; tool access is never derived from text.
3. **Rate limiting** — per-user/IP on API, per-agent on tool calls and LLM calls; load shedding.
4. **Tool parameter validation** — typed tool registry, per-tool schema, permission tiers
   (`read` / `propose` / `never`), timeouts and budgets.
5. **SQL guard** — AST parse + deny patterns + table allowlist + mandatory row cap and timeout.
6. **PII masking** — deterministic masking before LLM, before logs, before Teams, before export.
7. **Output sanitisation** — strip scripts/HTML, detect PII and canary leakage, cap length.
8. **Audit logging** — append-only; every decision, action and AI output with actor, inputs,
   rule/model/prompt versions, confidence and evidence.
9. **Human oversight** — AI recommends, rules decide, humans approve. No AI side effects.
Also mandatory: RBAC + server-side authorisation on every endpoint (never trust the client), object-level
authorisation checks (no IDOR), CSRF-safe auth, CORS allowlist from env, CSP + security headers, HSTS,
no path traversal in any file path built from input, explicit field allowlists on writes (no mass
assignment), secrets in env only + gitleaks in pre-commit and CI, non-root containers, pinned lockfiles,
pip-audit/npm audit, SBOM, STRIDE threat model in `docs/threat-model.md`.

## 7. AI rules
- **All LLM traffic goes through `ai/llm/gateway.py`.** No direct HTTP to a model from anywhere else.
  The gateway owns: env-driven base URL/key/model, capability probe, timeouts, retries with jitter,
  circuit breaker, cache, token/cost accounting, PII redaction, prompt versioning, tracing.
- **Schemas on all outputs.** Every LLM response is validated against a Pydantic model: invalid ⇒ one
  repair attempt ⇒ deterministic fallback. The product keeps working with the LLM down ("AI degraded").
- **No LLM-driven side effects.** LLMs never execute runbooks, write network commands, send customer
  messages, or see raw MSISDNs. They fill slots in approved templates; they do not write free text to
  customers.
- Agents: exactly three (DiagnosisAgent, KnowledgeAssistant, Reporter), all bounded by max steps,
  tokens, wall-clock and tool allowlist. Low confidence routes to a human.
- The five agent layers are engineered explicitly and documented in `docs/architecture/agent-layers.md`:
  **context** (stable system prompt first, cache-friendly, large payloads on disk/by reference, goal
  restated at the tail), **loop** (goal + external checker + stop rule + budget; deterministic filter
  first, loop briefly, fall back to a workflow), **cheap-decision gate** (a classifier/fast model picks,
  the reasoning model writes; unsure ⇒ escalate, fail closed), **harness** (containment, guides, typed
  tools, sensors, permission tiers), **evals** (golden sets from seeded ground truth, behavioural checks
  on the trace, run on every change in `make eval`).
- Classical first: baseline before model, LightGBM+SHAP before deep learning, time-based validation
  only, no leakage. GNN and bandit scaffolds stay disabled until they beat the baseline on the eval set.
- Every model gets a model card in `docs/model-cards/` and a registry entry.

## 8. Testing rules
- Write tests **alongside** the code in the same change. A module without tests is not done.
- **Never mark anything done while red.** No skipped tests to make a gate pass; no `xfail` without a
  linked tech-debt entry.
- Layers: unit (pytest, hypothesis for time windows/MSISDN normalisation), integration (real Oracle Free
  container), contract (shared across mock and live_stub), scenario (seeded ground truth with
  precision/recall thresholds), API e2e, Playwright journeys, a11y, load, security, LLM evals.
- Coverage ≥ 80% on `domain/` and `modules/`. Determinism: fixed seeds, injected clock, no network in
  unit tests (the fake LLM server covers LLM paths).
- Every entry in the PRD §15 edge-case register has at least one test that names it.

## 9. Git rules
- Small, focused commits. Conventional Commits: `feat|fix|docs|test|refactor|chore|perf|build|ci(scope): subject`.
- One phase ⇒ at least one commit per coherent unit of work, plus a final `docs(phase-N): report` commit.
- Never commit secrets, `.env`, data dumps, model binaries or build output. Run gitleaks before pushing.
- Branch `main` holds green work only: do not commit a red gate; fix it or record it in `docs/tech-debt.md`
  with the failing test named.
- Phase reports go to `docs/phase-reports/PHASE_N.md` and list: what was built, gate results (pasted
  output), assumptions, deviations from the PRD, and known gaps.

## 10. When stuck
Write down the assumption — in the current phase report and, if it changes behaviour, in an ADR — and
continue. Only a true blocker (missing credentials, an unavailable external system, a contradiction in
the PRD that changes the data model) justifies stopping to ask. Never guess silently.
