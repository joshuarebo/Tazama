# TAZAMA security baseline

Classification: Internal · Owner: Platform engineering · Last reviewed: 2026-10-21

Alignment: NIST AI RMF (Govern/Map/Measure/Manage), ISO/IEC 42001, ISO/IEC 27001 controls,
OWASP ASVS L2, OWASP Top 10 for LLM Applications, Kenya Data Protection Act 2019
(privacy by design; see `docs/dpia.md`; this is not legal advice).

Every control below is testable. Tests live in `backend/tests/security/` and run in
`make ci`; a control without a test is a wish, not a control.

## 1. Defence in depth — the nine layers

```
REQUEST / COMPLAINT TEXT / MODEL OUTPUT
  │
  ├─ 1 Input validation        Pydantic at every boundary: types, length, range, enum allowlist
  ├─ 2 Prompt-injection guard  untrusted text is data: delimiters, instruction hierarchy, canary
  ├─ 3 Rate limiting           per principal: API, LLM calls, tool calls; load shedding
  ├─ 4 Tool-arg validation     typed registry, per-tool schema, permission tier, timeout, budget
  ├─ 5 SQL guard               AST parse + deny patterns + table allowlist + row cap + timeout
  ├─ 6 PII masking             HMAC pseudonyms before analytics / AI / logs / Teams / export
  ├─ 7 Output sanitisation     strip active content, detect PII and canary leakage, cap length
  ├─ 8 Audit logging           append-only: actor, inputs, versions, confidence, evidence
  └─ 9 Human oversight         AI recommends, rules decide, humans approve; no AI side effects
  │
SANITISED RESPONSE
```

| Layer | Implementation | Test |
| --- | --- | --- |
| 1 | request/response models in `api/v1/schemas`, `api/v1/routers/*` | `tests/security/test_auth_and_headers.py`, per-router tests |
| 2 | `ai/llm/redaction.py`, prompt templates in `ai/llm/prompts/` | `tests/evals/` injection suite (Phase 4) |
| 3 | `core/security.RateLimiter`, `api/middleware.RateLimitMiddleware` | `tests/security/test_core_security.py` |
| 4 | `ai/agents/harness/registry.py` + `budget.py` | Phase 4 harness tests |
| 5 | `modules/discovery/sql_guard.py` | Phase 1 SQL-guard suite (~40 attacks) |
| 6 | `core/security.Pseudonymiser`, `core/logging.scrub` | `tests/security/test_core_security.py`, `tests/unit/test_logging_redaction.py` |
| 7 | `core/security.sanitise_output` | `tests/security/test_core_security.py` |
| 8 | `modules/audit` (append-only) | Phase 2 audit tests |
| 9 | `config/policies.yaml` + approval endpoints | Phase 2/6 approval tests |

## 2. The vibe-coder checklist, answered

Web and API classics:

| Risk | Control in TAZAMA |
| --- | --- |
| SQL injection | Bound parameters only; the single user-SQL surface goes through the AST-based guard with a table allowlist; `S608` is only waived inside `infrastructure/db/oracle` with a reviewed docstring. |
| XSS | React escapes by default; `sanitise_output` strips scripts/handlers/dangerous URIs from any model or complaint text that reaches a card, CSV or log; CSP on both API and web. |
| CSRF | No cookie-based auth: bearer tokens in the `Authorization` header, `allow_credentials=false` on CORS, so there is no ambient credential to ride. |
| CORS | Explicit allowlist from `CORS_ORIGINS`; `*` is refused by the settings validator. |
| SSRF | No endpoint fetches a user-supplied URL. Outbound calls go to configured hosts only (LLM, embeddings, Teams), each from env, never from request data. |
| IDOR | Every read of an object re-checks the caller's capability **and** the object's scope server-side; ids are non-sequential (time-sortable ULID-ish), and tests assert cross-tenant/cross-role denial. |
| Path traversal | `core.security.safe_path` resolves under a base directory and refuses `..`, absolutes, NULs and odd whitespace. Export filenames are generated, not taken from input. |
| Mass assignment | Writes use explicit request models; no model is constructed from raw request JSON, and internal fields (status, scores, versions) are never settable by a client. |
| Clickjacking | `X-Frame-Options: DENY` plus `frame-ancestors 'none'`. |
| CSP / headers | `api/middleware.SECURITY_HEADERS` + web headers: nosniff, no-referrer, COOP/CORP, Permissions-Policy, HSTS when TLS. |
| Input validation | Pydantic everywhere, with length and range bounds; unbounded strings are a review finding. |
| Server-side authorisation | `Principal.require(capability)`; a client-supplied role is never trusted. Unknown capability denies. |
| Row-level security | Live-mode reads use a read-only DB user; app-schema queries are scoped by the caller's region/role filters in the query, not in the UI. |
| Least privilege | Capability → role map in `core/security.PERMISSIONS`; admin is enumerated, not a wildcard. `report:send` and `admin:manage` are admin-only. |
| Secrets management | Env only, via one settings module; `.env` gitignored; gitleaks in pre-commit and CI; `redacted()` for any config dump; the Teams webhook is a `SecretStr` and never logged. |
| Key rotation | Salt/JWT/webhook rotation is documented in `docs/runbooks/`; rotating `PSEUDONYM_SALT` is a deliberate operation (ADR 0008). |
| Hashing vs encryption | HMAC-SHA256 pseudonyms for identifiers (one-way, keyed); no home-grown crypto; bcrypt available for any future local password store. |
| JWT expiry / sessions | `exp` and `iat` required, algorithm pinned (`alg: none` rejected), issuer checked, TTL from env; no refresh tokens in dev mode. |
| Account enumeration | One message for unknown user and wrong password; constant-time comparisons for tokens and pseudonyms. |
| Brute force | Rate limiting on the login path like any other; lockout is an OIDC-provider concern in `oidc` mode. |
| Timing attacks | `hmac.compare_digest` for every secret or token comparison. |
| File upload | v1 accepts no uploads. When knowledge ingestion lands, type and size are validated and content is never executed. |
| Webhook verification | Outbound only. The Teams Workflows URL is treated as a secret; payloads are schema- and size-validated before sending; dry-run is the default. |
| TLS / HSTS | TLS terminates at the platform edge; HSTS is set when the request arrived over HTTPS. |
| Encryption at rest | Oracle TDE / platform disk encryption (operational, outside the app); the app stores pseudonyms, not raw identifiers. |
| Dependency vulnerabilities | Lockfiles pinned, `pip-audit` + `npm audit` in CI, SBOM artefact per build. |
| Supply chain | Pinned action versions and base images; no `curl | sh` in any image; `uv.lock` committed. |
| Threat modelling | STRIDE in `docs/threat-model.md`, reviewed per phase. |

LLM-specific (OWASP Top 10 for LLM Apps):

| Risk | Control |
| --- | --- |
| Prompt injection (direct/indirect) | Complaint text is delimited untrusted data; tools are never selected from text; canary token detects system-prompt leakage; output filters block leaked canaries. |
| Insecure output handling | Schema validation on every output, then sanitisation before display or delivery. |
| Excessive agency | Permission tiers `read` / `propose` / `never`; no tool writes to the network; high-risk actions require human approval. |
| Sensitive information disclosure | Redaction before prompts and logs; the model never sees raw MSISDNs. |
| Model DoS / cost | Budgets per agent run (steps, tokens, wall clock), caching, circuit breaker, rate limits, token accounting. |
| Overreliance | Confidence thresholds route to humans; numeric grounding rejects invented numbers; every AI claim carries evidence. |

## 3. Residual risks (tracked, not hidden)
1. SQLite is the default demo backend, so Oracle-specific defects can escape local gates — ADR 0002, mitigated by shared contract tests.
2. Eval thresholds are fake-LLM baselines until real-Qwen numbers exist — ADR 0007.
3. Integration adapters are mocks in v1; live stubs are contract-tested but unproven against real systems.
4. A novel injection pattern may evade the detectors; the canary and the output filter are the backstop.
5. Dev-mode auth exists for local use only and is refused outside `local`/`dev` by the settings validator.

## 4. Incident response (security events)
| Severity | Trigger | Response |
| --- | --- | --- |
| Critical | Canary leak, confirmed data exfiltration attempt | Disable the AI capability flag, block the principal, notify security, preserve audit rows |
| High | SQL-guard bypass attempt, authz failure spike | Log, refuse, alert the on-call engineer |
| Medium | Suspicious but allowed input, repeated low-confidence routing | Log and monitor for escalation |
| Low | Rate limit hit | Throttle, log, auto-recover |
