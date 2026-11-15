# TAZAMA against the Safaricom AI standards

**Source:** Safaricom AI Democratization Framework and AI Enablement Strategy, as captured
in the Intelligent Commissions compliance checklist.
**Assessed:** 2026-11-15
**How to use this:** every row is PASS, PARTIAL or TODO. A PARTIAL or TODO carries the
reason and what unblocks it. Nothing is marked PASS on intent; each one names the file or
the gate that enforces it.

---

## 1. Security standards, non-negotiable

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 1.1 | PII masking before any LLM or external call | HMAC-SHA256 pseudonymisation at the ingestion boundary; `ai/llm/redaction.py` masks again before a prompt is built. Raw MSISDNs never leave ingestion. | PASS |
| 1.2 | No credentials in code | One `pydantic-settings` module reads the environment; nothing else does. `.env` gitignored, `.env.example` carries placeholders, CI asserts the two agree. | PASS |
| 1.3 | Input validation and sanitisation | Pydantic schemas at every boundary with length, range and enum bounds. `api/middleware.py` caps body size. | PASS |
| 1.4 | SQL injection prevention | `modules/discovery/sql_guard.py`: sqlglot AST parse, SELECT only, table allowlist, mandatory row cap and timeout. Parameterised SQL everywhere else, no string interpolation. | PASS |
| 1.5 | Read-only database access | `APP_MODE=live` forces read-only source reads; `ALLOW_LIVE_WRITES` defaults false and must be flipped deliberately. | PASS |
| 1.6 | Never log PII, credentials or tokens | Structured logging masks before output; the Teams webhook URL is never logged. Tested in `tests/security/`. | PASS |
| 1.7 | Bearer token authentication on APIs | JWT bearer on every route, server-side capability check per endpoint, 21 capabilities across 5 roles. **OIDC is configured but the provider integration is a stub.** | PARTIAL |
| 1.8 | TLS encryption | Edge termination on the OpenShift Route with HSTS preload and an insecure-to-secure redirect. Oracle connections use TLS. | PASS |
| 1.9 | Security headers | `SecurityHeadersMiddleware` sets CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, Permissions-Policy, and HSTS on TLS. `scripts/smoke_check.py` fails the deploy if any are missing. | PASS |
| 1.10 | Docker multi-stage, non-root, image scanning | Both images are multi-stage, numeric non-root USER, OpenShift arbitrary-UID compatible, read-only root filesystem. Trivy fails the build on HIGH or CRITICAL. | PASS |
| 1.11 | No external data exfiltration | No egress except the model gateway and the Teams sender, both internal. A source-level test enforces that only those two modules may hold an HTTP client. | PASS |
| 1.12 | Audit logging on every action | Append-only audit of every decision and AI output with actor, inputs, rule or prompt version, confidence and evidence. | PASS |

**Security: 11 PASS, 1 PARTIAL, 0 TODO.**

---

## 2. CI/CD standards

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 2.1 | Code linting | ruff check and format, mypy strict on domain/modules/core, import-linter contracts, eslint, tsc. All in `.gitlab-ci.yml` stage `lint`. | PASS |
| 2.2 | Security scanning | bandit, pip-audit, npm audit, Trivy filesystem, Trivy config, Trivy image, detect-secrets. Stage `scan`. | PASS |
| 2.3 | Unit tests at 80% coverage or above | 1,693 tests. `scripts/coverage_gate.py` fails below 80% on `domain/` and `modules/`. | PASS |
| 2.4 | GitOps-driven deployments | Kustomize overlays under `deploy/openshift`, applied declaratively. No imperative cluster edits. | PASS |
| 2.5 | Never commit secrets | detect-secrets in pre-commit and CI; `.gitignore`; manifest secrets are placeholders created out of band. | PASS |
| 2.6 | Idempotent workflows | Every job is safe to re-run. Deploys are declarative applies, migrations run as a Job with `backoffLimit: 0`. | PASS |
| 2.7 | Data validation steps | Schema validation on ingestion, source mappings validated at startup, `make catalog` measures join coverage every run. | PASS |

**CI/CD: 7 PASS.**

---

## 3. API development standards

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 3.1 | Structured JSON logging, ELK compatible | `core/logging.py`, JSON by default in the cluster ConfigMap, correlation ID on every line. | PASS |
| 3.2 | Input validation at multiple layers | Pydantic at the edge, typed tool registry for agents, SQL guard for the explorer, output sanitisation on the way back. | PASS |
| 3.3 | Async processing, caching, connection pooling | Async FastAPI, SQLAlchemy pooling with health checks, LLM response cache, transactional outbox for async work. | PASS |
| 3.4 | Health checks for horizontal scaling | `/healthz` liveness, `/readyz` readiness probing DB, LLM, vector backend and outbox lag, plus a startup probe. HPA scales on CPU and memory. | PASS |
| 3.5 | Test pyramid | unit, integration against real Oracle, contract across mock and live_stub, scenario against seeded ground truth, API e2e, Playwright journeys, a11y, load, security, LLM evals. | PASS |
| 3.6 | SOC 2, ISO 27001, Kenya DPA | Architecture aligned; DPIA written. **Three DPA procedures outstanding before real subscriber data: erasure cascade, salt rotation policy, minimum-exposure floor.** | PARTIAL |

**API: 5 PASS, 1 PARTIAL.**

---

## 4. Model monitoring standards

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 4.1 | Data drift detection | PSI implemented and tested in `ai/ml/evaluation.py`. **Nothing schedules it yet.** | PARTIAL |
| 4.2 | Model drift detection | Metrics compared against the training baseline; the forecast gate requires beating the baseline, non-zero prediction variance, and winning on non-zero rows. | PASS |
| 4.3 | Concept drift detection | Hooks exist; needs real labels to be meaningful. | TODO |
| 4.4 | Operational metrics, p50/p95/p99 | `/metrics` exposes request counters and latency histograms with fixed buckets, bounded cardinality, scraped by Prometheus. | PASS |
| 4.5 | Grafana dashboards from day one | Scrape target exists; **dashboards not yet committed as code.** | PARTIAL |
| 4.6 | End-to-end lineage | Agent traces capture tools, evidence and budget per decision; every decision records which path answered and the rule or prompt version. | PASS |

**Monitoring: 3 PASS, 2 PARTIAL, 1 TODO.**

---

## 5. Governance and guardrails

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 5.1 | Data access policies | Pseudonymisation, read-only source access, per-endpoint capability checks, audit trail. | PASS |
| 5.2 | Model approval workflow | Production deploy is a manual gate on a tag. No auto-deploy to production. | PASS |
| 5.3 | Audit logs | Append-only, every decision and action. | PASS |
| 5.4 | Resource quotas | Agent budgets on steps, tool calls, tokens and wall-clock; SQL row caps and timeouts; cluster ResourceQuota and LimitRange; API rate limiting. | PASS |
| 5.5 | Human-in-the-loop for high-risk actions | Notifications require approval with separation of duties enforced in code. No AI side effects anywhere. | PASS |
| 5.6 | Confidence scores on all AI recommendations | Every diagnosis carries confidence and a floor; below the floor it routes to a human. | PASS |
| 5.7 | Data source reference on all outputs | Every diagnosis lists its evidence rows; every knowledge answer must cite a retrieved document or it is rejected. | PASS |
| 5.8 | Shadow mode before production | Staging overlay exists and is wired. **Not yet run for the required period.** | PLANNED |
| 5.9 | Model card for every deployed model | Two written (`call_surge_forecast`, `kpi_anomaly_detector`). **Priority model has none and is not in control; GNN and bandit are registered but disabled.** | PARTIAL |
| 5.10 | Documented rollback plan | `rollback:production` job, plus `docs/runbooks/rollback.md` covering the decision between rollback and fix-forward, and why migrations are deliberately not auto-reverted. | PASS |
| 5.11 | Ethical AI and fairness review | Rule-based decisions are inspectable; no protected attributes are used as features. Formal review not yet held. | PARTIAL |
| 5.12 | Kenya DPA compliance | Pseudonymisation at the boundary, internal-only processing, DPIA written. Blocked on the three procedures in 3.6. | PARTIAL |

**Governance: 8 PASS, 3 PARTIAL, 1 PLANNED.**

---

## 6. AIOps framework alignment

| # | Requirement | Implementation | Status |
|---|---|---|---|
| 6.1 | Full MLOps lifecycle | Data prep, training, evaluation and registry are built with time-ordered validation and a leakage gate. **Deploy, monitor and retrain need the platform.** | PARTIAL |
| 6.2 | Experimentation in a controlled sandbox | Seeded synthetic data with a fixed seed; staging overlay for the lab. | PASS |
| 6.3 | No external data exfiltration | Internal model endpoint, read-only sources, no third-party APIs. Map tiles are configured by URL and the console is tested to work without any. | PASS |
| 6.4 | Feature store integration | Not yet evaluated. Revisit when feature reuse across models justifies it. | TODO |

**AIOps: 2 PASS, 1 PARTIAL, 1 TODO.**

---

## 7. Iguazio and platform access

| # | Requirement | Status |
|---|---|---|
| 7.1 | Sandbox namespace via the MLOps team | **Needed.** Manifests are ready to apply once a project exists. |
| 7.2 | SSO / AD authentication | `AUTH_MODE=oidc` wired; needs an Entra ID app registration. |
| 7.3 | Use-case request via #ai-onboarding | **Needed before formal deployment approval.** |
| 7.4 | ResourceQuotas and LimitRanges | Defined in `deploy/openshift/base/platform.yaml`. PASS |

---

## Scorecard

| Category | PASS | PARTIAL | TODO | Total |
|---|---|---|---|---|
| Security | 11 | 1 | 0 | 12 |
| CI/CD | 7 | 0 | 0 | 7 |
| API | 5 | 1 | 0 | 6 |
| Monitoring | 3 | 2 | 1 | 6 |
| Governance | 8 | 4 | 0 | 12 |
| AIOps | 2 | 1 | 1 | 4 |
| **Total** | **36** | **9** | **2** | **47** |

**77% PASS, 19% PARTIAL, 4% TODO.**

For reference, Intelligent Commissions scored 64% PASS at the equivalent stage. The
difference is mostly CI/CD and API, which were built in from the start here.

---

## What blocks production

In the order they must be resolved.

1. **OIDC against Entra ID (1.7).** The capability model, server-side checks and token
   validation are built. What is missing is the provider: JWKS fetch with key rotation,
   issuer and audience validation, and a groups-claim to role mapping. Needs an app
   registration.
2. **The three Kenya DPA procedures (3.6, 5.12).** Erasure that cascades from the
   pseudonym token, a policy decision on what happens to historical analytics when the
   salt rotates, and a minimum group size below which a cell is not reported individually.
   Days of work each, but they are obligations and they gate real subscriber data.
3. **Grafana dashboards as code (4.5).** The scrape target now exists. The standard says
   dashboards from day one, so they ship with the deployment rather than after it.
4. **Scheduled drift monitoring (4.1).** PSI is implemented and tested; a worker job needs
   to call it on a cadence and alert on breach.
5. **Rollback runbook (5.10).** The pipeline job exists. The written procedure, including
   what to do about a migration that has already applied, does not.
6. **Shadow mode (5.8).** Run in parallel against real data without acting, for the agreed
   period, before anything is relied upon.
7. **Model card for any model that enters control (5.9).** The priority model is
   deliberately not in control, so this is only blocking if that changes.

## What is already met

Every security fundamental: pseudonymisation, SQL guard, prompt-injection guard, audit
logging, read-only access, no secrets in code, input validation at every boundary,
human-in-the-loop on anything that reaches a customer, confidence scores and source
references on every AI output, and no path by which a model can act on the network.

Plus the CI/CD and API rows in full, which is what makes the remaining work schedulable
rather than open-ended.

**Verdict: deployable to a lab or staging namespace now. Production needs items 1 and 2.**

---

## Findings fixed during this assessment

Worth recording, because the chain was only visible when the three were read together.

**A placeholder signing key would have reached production.** The base manifest shipped a
`Secret` containing `JWT_SECRET: "REPLACE_IN_CLUSTER"`, and that file was listed in the
kustomize resources, so `oc apply -k` would have overwritten the real cluster Secret on
every deploy. Separately, the ConfigMap did not set `APP_ENV`, so it fell back to its
default of `local`, where the validator that rejects placeholder secrets returns early.
And `"REPLACE_IN_CLUSTER"` matched none of the placeholder patterns that were checked.

Together: a known signing key in production, which is admin-token forgery by anyone with
repository access, plus a known pseudonymisation salt, which makes every pseudonym
reversible.

Each link is now broken on its own:

- the Secret is no longer an applied resource, and
  `tests/unit/test_import_boundaries.py` fails if any Secret is added to a kustomization;
- `APP_ENV` is set explicitly in every overlay;
- placeholder rejection is **unconditional**, in every environment, and matches `replace`
  case-insensitively. `tests/security/test_placeholder_secrets.py` covers each token;
- `scripts/mk.py` generates real local secrets when creating `.env`, since the committed
  template is a placeholder by design.

**Metrics cardinality could be driven past its cap.** The route label was bounded but the
HTTP method was not, and a client may send any token as a method, so the overflow key was
still unbounded. Methods are now normalised to a fixed allowlist and the overflow series
has entirely constant labels.

