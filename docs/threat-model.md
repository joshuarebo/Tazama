# Threat model — TAZAMA

STRIDE over the trust boundaries that actually exist in this system. Written against the
code as built, not against an intended design: every control named here has a test, and the
test is named.

Scope: the modular monolith (`backend/`), the Next.js console (`web/`), the worker, and the
six external systems the product talks to. Out of scope: the operator's own network
elements (TAZAMA never commands them), and the physical security of the hosts.

**Status: unreviewed.** This is the build team's own analysis. It has not been through a
security review with the operator, and `docs/tech-debt.md` #9 records that.

---

## 1. Trust boundaries

| # | Boundary | What crosses it | Direction |
| --- | --- | --- | --- |
| B1 | Browser → API | Session token, query parameters, SQL (Data Explorer), approvals | In |
| B2 | Channel → ingestion | Complaint text, MSISDN, stated location | In |
| B3 | Source systems → readers | KPIs, alarms, topology, subscriber facts | In |
| B4 | API/worker → Oracle | Parameterised SQL | Both |
| B5 | Gateway → model endpoint | Redacted prompts | Out |
| B6 | Reporting → Teams | Adaptive Card | Out |
| B7 | Notification → SMS | Pseudonym token + template id | Out |
| B8 | Operator → runbook execution | **Nothing. There is no path.** | — |

B8 is the most important row in the table. TAZAMA proposes; a human executes elsewhere.
There is no adapter, no credential and no code path that reaches a network element, which
removes the entire class of "the AI reconfigured the network" threats rather than
mitigating it.

---

## 2. Spoofing

| Threat | Vector | Control | Test |
| --- | --- | --- | --- |
| Caller impersonates a role | Forged or altered JWT | Pinned algorithm (`HS256` only, `alg` never read from the token), `exp` and `iat` required, server-side role lookup on every request | `tests/unit/test_auth.py`, `tests/api_e2e/*::*authorisation*` |
| Client claims a role the server does not grant | `role` in a request body or header | Role comes from the verified token only; `requires(capability)` consults `PERMISSIONS`, never the request | `tests/api_e2e/test_insights_reports_api.py::test_*_authorisation` |
| SSE stream opened with a stolen URL | Token in a query string, which leaks via logs, proxies and `Referer` | Single-use tickets, 30-second TTL, principal-bound, consumed on first use; `Referrer-Policy: no-referrer` | `tests/unit/test_scheduler_and_stream.py` |
| Forged webhook delivery | Unauthenticated inbound callback | **No inbound webhooks exist.** Teams is outbound only | — |
| Complaint attributed to the wrong subscriber | Channel supplies a bad MSISDN | Pseudonymised at ingestion; the token is the identity, and a wrong number yields a wrong token rather than another subscriber's record | `tests/api_e2e/test_care_api.py` |

---

## 3. Tampering

| Threat | Vector | Control | Test |
| --- | --- | --- | --- |
| SQL injection via the Data Explorer | User-supplied SQL — the one place it is accepted | `sqlglot` AST parse, SELECT-only, single statement, table allowlist (not a denylist), mandatory row cap and timeout, audited | `tests/security/test_sql_guard.py` (~40 attack cases) |
| SQL injection anywhere else | String-built queries | Parameterised SQL only; no interpolation into SQL exists in the codebase | `make lint` + review |
| Mass assignment | Extra fields in a write body | Pydantic schemas with explicit field allowlists at every boundary | `tests/api_e2e/*` |
| Path traversal | File paths built from input (exports, artifacts) | `safe_path` containment check; the model-artifact loader refuses any path outside the artifacts directory | `tests/security/test_core_security.py`, `tests/unit/test_ml.py` |
| Prompt injection changing behaviour | Complaint text reaching a model | Text is delimited data, never instruction; the delimiter cannot be closed by the text; tool access is never derived from text; patterns detected and recorded | `tests/security/test_llm_gateway_guards.py` |
| Agent reaching an executing tool | Model asks for `execute_runbook` | Permission tiers; unregistered tools default to `never`; the tier check precedes the handler | `tests/evals/test_agent_evals.py::test_the_agent_cannot_reach_an_executing_tool` |
| Fabricated evidence surviving review | Model cites an alarm that does not exist | Grounding check against the evidence actually provided; an ungrounded diagnosis is discarded | `tests/evals/test_agent_evals.py::test_an_ungrounded_model_diagnosis_is_discarded` |
| Invented figures in a management report | Model writes a number it was not given | Numeric grounding; one unaccounted figure discards the whole digest | `tests/evals/test_agent_evals.py::test_an_invented_figure_discards_the_whole_digest` |
| Registry history rewritten | Re-registering a model version | Versions are immutable; re-registration is a conflict | `tests/unit/test_ml.py::test_re_registering_a_version_is_refused` |
| Audit trail altered | Update or delete on the audit table | Append-only by construction; no update or delete path exists | `tests/unit/test_outbox_and_audit.py` |

---

## 4. Repudiation

| Threat | Control | Test |
| --- | --- | --- |
| "I never approved that notification" | Append-only audit row per decision with actor, role, inputs, rule and prompt versions, confidence and evidence | `tests/unit/test_outbox_and_audit.py` |
| "The AI decided, not me" | AI output is a proposal with `requires_approval=True`; approval is a separate audited action by a named principal | `tests/evals/test_agent_evals.py::test_approval_is_requested_before_any_propose_tier_action` |
| "That number was never in the report" | Deterministic report ids; the ledger records every send attempt and every dead letter | `tests/unit/test_reporting_*` |
| "The model said something else" | Prompt version, model name and token counts on every call; the trace records tool calls and refusals | `LlmResult.audit_inputs`, `AgentTrace.to_dict` |

---

## 5. Information disclosure

The highest-consequence category, because the data is subscriber data.

| Threat | Vector | Control | Test |
| --- | --- | --- | --- |
| MSISDN reaches a model endpoint | Complaint text in a prompt | HMAC pseudonymisation at ingestion plus redaction at the gateway boundary, with stable in-request placeholders | `tests/security/test_llm_gateway_guards.py::test_an_msisdn_never_reaches_the_wire` |
| MSISDN comes back from a model | Inference or hallucination | Outbound identifier scan on the message body, on tool-call arguments, and on the repair attempt; a hit degrades rather than returning | same file, three tests |
| System prompt leaks | Model echoes its instructions | Canary token, compared after flattening case and punctuation; a leak destroys the response | same file, six spellings |
| Identifier in a Teams card | Digest built from subscriber-linked rows | Card scanned for MSISDN, token and e-mail shapes before return; a hit **raises** rather than redacting, so the source gets fixed | `tests/unit/test_reporting_digest.py` |
| Identifier in a log line | Structured logging of request bodies | `scrub_text` and `scrub` at the logging boundary; MSISDN shapes and long digit runs masked | `tests/unit/test_logging_redaction.py` |
| Identifier in an API response | Complaint previews on the incident card, complaints list, care lookup | `scrub_text` on every free-text preview | `tests/api_e2e/test_care_api.py::test_a_complaint_preview_is_scrubbed_of_numbers` |
| Subscriber enumeration | `GET /care/samples` listing the most-complaining subscribers | Demo-mode only; 404 in `live`. Token-based lookup also refused outside demo | `tests/api_e2e/test_care_api.py::test_the_demo_affordances_are_closed_outside_demo_mode` |
| IDOR on an incident or complaint | Guessed id in a path | Object-level authorisation on every detail endpoint | `tests/api_e2e/*` |
| PII in a CSV export | Explorer export | Column-level masking from `PII_COLUMNS`, applied before serialisation | `tests/security/test_sql_guard.py` |
| Webhook URL disclosure | Logs, error messages, API responses | Never logged, never returned; only `webhook_configured: bool` is exposed; transport errors report the exception *type* only | `tests/api_e2e/test_insights_reports_api.py::test_the_webhook_value_is_never_returned` |
| Secret in the repository | Committed `.env`, pasted key | `.env` gitignored, `scripts/scan_secrets.py` in pre-commit and CI, value-level allowlist | `make ci` |
| Timing oracle on subscriber existence | Lookup latency differences | Constant-time token comparison (`hmac.compare_digest`) | `tests/security/test_core_security.py` |

---

## 6. Denial of service

| Threat | Control | Test |
| --- | --- | --- |
| Expensive Explorer query | Mandatory row cap, statement timeout, `EXPLAIN` warning before execution | `tests/security/test_sql_guard.py` |
| Request flood | Per-principal and per-IP token buckets; load shedding returns problem+json 429 | `tests/security/test_rate_limit.py` |
| Agent loop that never ends | Step, tool-call and wall-clock budgets in the harness; a budget firing returns an answer rather than raising | `tests/evals/test_agent_evals.py::test_a_budget_stops_a_runaway_loop` |
| Slow model stalling the pipeline | Timeouts, bounded retries with jitter, circuit breaker; the deterministic path continues | `tests/security/test_llm_gateway_guards.py::test_repeated_failures_open_the_circuit` |
| Token spend without limit | Per-process call and token budgets on the gateway | same file |
| Outbox backlog | Bounded batches, per-event transactions, dead letters after 5 attempts, operator requeue | `docs/runbooks/outbox-backlog.md` |
| Notification storm | Per-incident dedupe, quiet hours, rate limits, opt-out, and approval before any send | `tests/unit/test_notification.py` |
| Alarm storm collapsing correlation | Storm detection and collapsing in the correlation engine | `tests/scenarios/` |
| Oversized request body | 413 at the middleware with problem+json | `tests/api_e2e/test_health_and_meta.py` |

---

## 7. Elevation of privilege

| Threat | Control | Test |
| --- | --- | --- |
| Care agent reaching NOC data | Capability matrix checked server-side per endpoint; the client's nav is cosmetic | role matrix verified across all six pages |
| Agent escalating its own tool access | Tiers and grants are code, not data; no text can alter them | `tests/evals/test_agent_evals.py` |
| LLM causing a side effect | No tool above `propose` is reachable; `propose` writes nothing | same file |
| Writes in live mode | `APP_MODE=live` makes source reads read-only; `ALLOW_LIVE_WRITES=false` by default and must be flipped explicitly | `tests/unit/test_settings.py` |
| Container escape widening blast radius | Non-root containers, pinned lockfiles, SBOM, `pip-audit`/`npm audit` | `make sbom`, `make audit` |
| CSRF riding an authenticated session | Bearer token in a header, no cookie, so there is no ambient credential to ride | `web/src/lib/api.ts` |
| Clickjacking | `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'` | `web/next.config.mjs`, `tests/api_e2e/test_health_and_meta.py` |

---

## 8. What is *not* mitigated

Stated plainly, because a threat model that claims full coverage is not a threat model.

1. **A malicious insider with the app's database credentials** can read everything the app
   can read. Pseudonymisation protects against disclosure *downstream* of ingestion; it
   does not protect the ingestion boundary from someone who holds its keys.
2. **A compromised model endpoint** sees redacted prompts, which still carry network
   topology, incident shapes and complaint text with identifiers removed. That is
   commercially sensitive even though it is not personal data.
3. **The live Teams sender is unverified** (`docs/tech-debt.md` #15). It is contract-tested
   against the documented payload shape and left in dry-run.
4. **The pseudonym salt is a single point of failure.** Rotating it breaks the link between
   historical analytics and current subscribers; there is no re-keying procedure yet.
5. **Dependency supply chain.** Lockfiles are pinned and audited, but no artifact signing
   or provenance verification is in place.
6. **No penetration test has been performed.** Every control here was verified by its own
   tests, which is not the same as having been attacked by someone trying to win.

---

## 9. Review triggers

This document is re-examined when any of these happens:

- the first real subscriber record enters any environment;
- a model endpoint outside the operator's network is configured;
- a Teams webhook is provisioned;
- an adapter gains a write path to any source system;
- `ALLOW_LIVE_WRITES` is set true anywhere;
- a new agent or a new tool tier is added.
