# Phase 6 — Reporting, hardening, handover

Status: **gate green**. The Teams digest is built, validated and never sent; the threat
model and DPIA are written; the dependency audit is clean; and the whole stack runs.

The judgement running through this phase: where a control could not be verified, it is
labelled unverified rather than described as working.

---

## 1. What was built

| Component | Where | Notes |
| --- | --- | --- |
| Digest model | `modules/reporting/models.py` | A plain structure, not a card — so one truth renders to Teams, to the console and later to e-mail |
| Card builder | `modules/reporting/digest.py` | Adaptive Card 1.5, PII scan, size guard, Markdown neutralised |
| Schedule | `modules/reporting/schedule.py` | Local times in `DISPLAY_TZ`, converted at comparison |
| Reporting service | same | Idempotent send via a ledger; dead letters |
| Dry-run sender | `infrastructure/adapters/teams/mock.py` | **The default.** Validates as strictly as the live one |
| Workflows sender | `infrastructure/adapters/teams/live_stub.py` | Unverified; contract-tested against the documented payload |
| Reports API | `api/v1/routers/reports.py` | Preview only — there is no send route |
| Threat model | `docs/threat-model.md` | STRIDE over the eight real trust boundaries |
| DPIA | `docs/dpia.md` | Kenya DPA 2019, with a blocking prerequisites list |

---

## 2. Gate results

```
make ci               green  (lint, format, import-linter 5/5, mypy, tests, coverage, eval, secrets)
tests/unit/test_reporting_schedule.py   20 passed
tests/unit/test_reporting_digest.py     18 passed
npm audit                               found 0 vulnerabilities
playwright test                         25 tests
```

---

## 3. The decisions worth defending

### Local time is stored, not UTC

The requirement is "07:30 Africa/Nairobi". Storing 04:30 UTC would bake today's offset into
the schedule, and the day a timezone rule changes the digest silently arrives an hour out —
with nothing failing. So `ScheduledReport.at_local` holds `"07:30"` and the conversion
happens at comparison.

Nairobi has no DST, which is exactly why this needed a test rather than a comment.
`test_the_daily_digest_fires_at_0730_nairobi_which_is_0430_utc` asserts both directions:
04:30 UTC fires the daily digest, and 07:30 UTC — 10:30 in Nairobi — fires nothing.

### There is no send endpoint

`TEAMS_DRY_RUN=true` is the default, and the API has no route that posts. A digest that can
be sent from a GET is a digest that gets sent by accident. The test that asserts this is
`test_there_is_no_way_to_send_a_report_over_http`, which checks the *absence* of three
plausible routes — worth more than any assertion about a 200.

### A PII hit raises rather than redacts

`assert_no_pii` raises `PiiInCardError` and blocks the report. Redacting would hide the
upstream leak, and the point of finding an identifier in a card is to fix wherever it came
from. The exception deliberately does not repeat the offending value, because the exception
goes to logs and logs must not become the leak.

### The webhook URL never appears anywhere

Not in logs, not in errors, not in a response — only `webhook_configured: bool`. A
Workflows URL is a bearer credential: anyone who reads it can post to the channel. Transport
failures report the exception *type* only, because `httpx` puts the URL in the message.
`test_the_webhook_url_never_appears_in_an_outcome` plants a recognisable secret in the URL
and asserts it is absent from the outcome.

### Workflows, not Office 365 connectors

Connectors retire on 31 March 2026. Building against them would have been building something
with a known end date.

---

## 4. Defects found

**Verification had never run against a real adapter.** mypy flagged three
`incompatible type` errors in the composition root. They were not typing noise: the port
declares `baseline(*, element_id: str, kpi: str, window)` and the repository has
`baseline(cell_id, kpi: KpiName, window, granularity)` — positional, differently named —
and `current`, `open_alarms` and `count_since` did not exist on the adapters at all. The
first real call would have raised `TypeError`. The module had unit tests with fakes and an
HTTP surface, and still had never touched the database.

Three shims now live in the composition root (which is what a composition root is for), and
`tests/api_e2e/test_verification_wiring.py` goes through `build_services` with real
repositories so the ports and the adapters are proven to fit. Writing that test immediately
found a second thing: a fixture whose recovery happened *after* the frozen clock, which the
service correctly reported as "not recovered" — the service was right and the test was wrong.

**Two import-linter contracts were broken.** `ai.agents` transitively imported `httpx`
through the gateway, and `ai.ml.training` imported `infrastructure`. Both contracts were
right and both fixes improved the design: agents now depend on `LlmPort` declared in their
own package, and the ML composition root moved to `tazama/training.py` beside
`composition.py` — the layer that is allowed to know about both.

---

## 5. Deviations from the plan

| Planned | Built | Why |
| --- | --- | --- |
| Worker scheduler running the digest | `due_reports` is pure and tested; the worker does not yet call it | Sending on a timer with no webhook and no approval step would be a loaded gun. The scheduler hook is the last wire, and it belongs with a provisioned webhook |
| Admin "send now" | Not built | Same reason; `force=True` exists on the service and is tested |
| Load test (Locust) thresholds | Target exists (`make load`); no thresholds recorded | Not run. Recorded as a gap, not as a pass |
| SBOM / pip-audit in the gate | Targets exist (`make sbom`, `make audit`); `npm audit` is clean and verified | `pip-audit` was not run in this pass |
| Runbooks: DB down, LLM down, bad mapping, notification storm | Only `outbox-backlog.md` exists | Recorded as a gap |

---

## 6. Known gaps

1. **The live Teams send is unproven** (tech-debt #15). No webhook was available. It is
   contract-tested against the documented payload shape and left in dry-run.
2. **Nothing sends on a schedule.** `due_reports` is tested; no worker job calls it.
3. **No load test has been run.** No p99 figure exists for the incident list.
4. **`pip-audit` was not run** in this pass. `npm audit` is clean.
5. **Four of five runbooks are missing.**
6. **The threat model and DPIA are unreviewed.** They are the build team's own analysis of
   a system that has only ever processed synthetic data. `docs/dpia.md` §7 lists seven
   items that are blocking before real data — the most important being that **no erasure
   procedure exists**.
7. **`make demo` has not been rehearsed from a fresh clone with empty volumes.** The stack
   runs here, from a working tree with a seeded database; that is not the same test.
