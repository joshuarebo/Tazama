# TAZAMA — final report

A Smart Optimization Intelligence Platform for a mobile network operator, built on
synthetic data. It correlates customer complaints with network KPIs and alarms, detects and
explains incidents, recommends evidence-backed actions, proposes human-approved customer
notifications, and reports to Microsoft Teams.

This report is the handover. It says what works, what does not, what was assumed, and what
has to happen before the system sees a real subscriber record.

---

## 1. What runs

```
http://localhost:3000     the operator console (six pages)
http://127.0.0.1:8000     the API (41 routes)
```

From the seeded database, detected by the pipeline rather than fixtured:

| Figure | Value |
| --- | --- |
| Live incidents | 17 (plus 2 suppressed as planned maintenance) |
| Subscribers affected | 140,201 |
| Complaints correlated | 69 |
| Causes found | congestion 13, cell outage 3, transmission link failure 1 |
| Cells on the map | 49 across 5 regions |

**Walkthrough:** open `http://localhost:3000`, sign in as **Admin** (that role holds all
six capabilities; the others are deliberately narrower and the server enforces it).

1. **Command Center** — headline figures, the affected-cell map, cause and region
   breakdowns, and the incident queue worst-first.
2. **An incident card** — click any incident title. Impact in subscribers and cells, the
   probable cause *with the evidence rows behind it*, the affected-cell map, alarms, hourly
   KPI curves marked at the incident start, the correlated complaints, and the decision
   trail with actor and rule version.
3. **Data Explorer** — run "Complaints per region" (it works), then "Try to delete data"
   (it is refused by the SQL guard, with the reason, and never reaches the database).
4. **Insights** — complaint rate raw against de-seasonalised, lead/lag with a
   plain-language reading, candidate drivers, correlation pairs, PCA.
5. **Reports** — the Teams digest as a rendered card *and* as the exact Adaptive Card JSON
   that would be posted, with `TEAMS_DRY_RUN=true` stated on the page. There is no send
   button, because there is no send endpoint.
6. **Care lookup** — sign in as **Care agent**, click a seeded token, and read the
   approved wording. There is no free-text box: an agent reads an approved line, they do
   not compose one.

---

## 2. Gate results

| Gate | Result |
| --- | --- |
| `make ci` | **green** — ruff, format, import-linter 5/5, mypy strict, pytest, coverage, evals, secret scan |
| Phase 1 seed | reproducible; content hash identical across runs; join coverage 51.99% recorded as the day-one baseline |
| Phase 2 scenarios | **precision 1.0000 / recall 1.0000** against floors of 0.85/0.80; maintenance suppression 100%; replay deterministic |
| Phase 3 ML | forecast beats seasonal-naive by **15.7%** overall and on the hours where demand exists; anomaly recall 0.600 at a 3.93% flag rate |
| Phase 4 evals | diagnosis **7/7** labelled scenarios top-1 with no model involved; injection suite 100% blocked; all five degraded modes answer correctly |
| Phase 5 frontend | lint and `tsc` clean; `npm audit` **0 vulnerabilities**; **25/25 Playwright tests pass** (journeys, axe on all six pages, token contrast in both themes) |
| Phase 6 reporting | digest built, PII-scanned, size-guarded; schedule timezone-correct; no send path exists |

---

## 3. What the product actually does well

**The complaint → cell join is measured, not assumed.** It is the hardest problem in the
product and the number is on the Command Center every time it loads. 51.99% at the start,
and the unlocated count is a filter on the Complaints page rather than a footnote.

**The deterministic path is the primary path.** Diagnosis, classification and reporting all
work with no model at all, and the LLM is consulted only where rules genuinely cannot
separate two answers. The product degrades to "less fluent" rather than to "broken", and it
says which.

**Every AI output is checked against something.** A diagnosis citing an alarm that was never
provided is discarded. An answer citing a document that was never retrieved becomes a
refusal. A digest containing a number that was not in its inputs is thrown away whole.

**Nothing the AI produces can act.** There is no adapter, credential or code path from
TAZAMA to a network element. `execute_runbook` is registered only so that an agent asking
for it is recorded.

---

## 4. Assumptions

1. **All data is synthetic**, generated from a fixed seed. Every accuracy figure is
   optimistic: the relationships are the ones the generator put there.
2. **KPI names and thresholds are modelled** from public RAN practice, not from the
   operator's counters. Flagged UNVERIFIED in `docs/data-catalog.md`; reconciling them is
   the first live-mapping task.
3. **Brand values are placeholders.** `tokens.css` says so, and a test fails when
   `--brand-green` changes to prompt a contrast recheck. No logo is embedded.
4. **SQLite is the default backend** (ADR 0002). Oracle Free sits behind the same ports and
   the same contract tests, selected by `DB_BACKEND=oracle`.
5. **The fake LLM is the eval gate** (ADR 0007). Every threshold in Phase 4 is a statement
   about the harness, not about Qwen.
6. **Forecasting is per region, not per cell** — measured, not chosen: the cell-hour target
   is 97.6% zeros and unlearnable. See `PHASE_3.md` §3.
7. **Dev auth mode** (password equals username) powers the role switcher. The settings
   validator refuses it outside local/dev.

---

## 5. Known gaps

Ordered by what would block a pilot.

### Blocking before any real subscriber data

1. **No erasure procedure.** Deleting a pseudonym token must cascade to complaints, care
   calls, incident links and analytics. It does not yet. (`dpia.md` §7)
2. **No salt-rotation procedure.** Rotating `PSEUDONYM_SALT` breaks the link between
   historical analytics and current subscribers, and nothing handles that.
3. **Re-identification risk in the insights surface.** A per-cell complaint rate at 03:00
   on a twelve-subscriber cell is close to identifying. Needs a minimum-exposure floor.
4. **DPIA and threat model are unreviewed.** Both are the build team's own analysis.
5. **No retention job.** The schedule is a document, not code.
6. **No audit-review routine** for care lookups. An append-only log nobody reads is
   evidence after the fact, not a control.

### Functional gaps

7. **Nothing sends on a schedule.** `due_reports` is pure and tested; no worker job calls
   it, and the live Teams sender is unverified (no webhook was available).
8. **Four of five runbooks are missing** — only `outbox-backlog.md` exists.
9. **No load test has been run.** No p99 figure for the incident list.
10. **No real-model evals.** `make eval-live` is wired and skips cleanly.
11. **No hybrid retrieval.** The knowledge tool is a simple search with citation
    verification; the in-memory vector backend is a linear scan.
12. **No component unit tests** in the frontend, and no console i18n.
13. **No GNN, no bandit.** Scaffolds exist, are flagged off, and refuse to run.
14. **`make demo` has not been rehearsed from a fresh clone with empty volumes.** Its steps
    *have* been run in sequence on this machine from a wiped database — `seed` → `pipeline`
    → `catalog` → API → web, producing the 17 incidents and 69 correlated complaints above —
    but a fresh clone with empty volumes is a different test and has not been done.

`docs/tech-debt.md` carries the full list with escalation triggers.

---

## 6. Switching to LIVE mode

Config only. No code change.

### 1. Point the mappings at real tables

`config/source_mappings.yaml` has `demo` and `live` blocks. Only the `live` block changes,
and it is the only place in the system that knows a real table or column name. Mappings are
validated at startup: a missing table fails fast with every offending entry listed.

### 2. Set the environment

```bash
APP_MODE=live                 # source reads become read-only
ALLOW_LIVE_WRITES=false       # leave false; writes are restricted to the app schema
DB_BACKEND=oracle
ORACLE_DSN=...                # a read-only account for source schemas
PSEUDONYM_SALT=...            # 32+ random bytes, never the dev value
JWT_SECRET=...                # likewise
AUTH_MODE=oidc                # dev auth is refused outside local/dev
CORS_ALLOW_ORIGINS=https://...  # a wildcard is refused
```

The settings model fails startup with **one** error listing every bad variable, rather than
one per restart. `.env.example` holds every variable with safe placeholders, and CI asserts
that the model and the example agree.

### 3. What `APP_MODE=live` changes by itself

- Source reads are read-only; writes are restricted to the app schema.
- The demo ribbon disappears, and `is_demo` drops off the Teams card.
- `GET /care/samples` returns 404 — it is a subscriber-enumeration endpoint against real
  data.
- Care lookup refuses pseudonym tokens and requires an MSISDN, so an agent cannot walk the
  pseudonym space without holding a number.

### 4. Then, deliberately

```bash
make doctor      # environment, config, DB, LLM probe, vector backend, mappings
make migrate     # forward migrations; every revision has a tested down migration
make catalog     # regenerate the data catalog and the join-coverage report
```

Read the new join-coverage number before anything else. It is the single best early
indicator of whether the mappings are right: if complaint → cell coverage collapses against
the 51.99% baseline, the serving-cell source is wrong and every downstream correlation will
be quietly thin.

### 5. Keep off until deliberately enabled

- `TEAMS_DRY_RUN=true` — flip only with a provisioned webhook, and send one test card
  first.
- `SMS_GATEWAY_MODE=mock` — the only supported value in v1.
- `LLM_BASE_URL` — unset means the deterministic path, which is a working product.
- The GNN and bandit flags.

---

## 7. Where things are

| Looking for | Path |
| --- | --- |
| Source of truth | `PRD.md` |
| Operating manual | `CLAUDE.md` |
| Phase reports | `docs/phase-reports/PHASE_0.md` … `PHASE_6.md` |
| Decisions | `docs/adr/0001` … `0009` |
| Security baseline (9 layers) | `docs/security/security-baseline.md` |
| STRIDE threat model | `docs/threat-model.md` |
| Privacy assessment | `docs/dpia.md` |
| Model cards | `docs/model-cards/` |
| Known debt with triggers | `docs/tech-debt.md` |
| Data catalog and join coverage | `docs/data-catalog.md` |
| Agent doctrine as built | `docs/architecture/agent-layers.md` |

---

## 8. The honest summary

The architecture is defensible and the gates are real: a scenario suite that measures
detection against seeded ground truth, a leakage gate that refuses a lead disguised as a
lag, an eval suite that checks agent *traces* rather than answers, and a frontend whose
accessibility and colour contrast are asserted rather than assumed. Several gates caught
genuine defects during the build — a model that had learned to predict a constant, an
anomaly detector blind to a total collapse, a verification module that had never run
against a real adapter, five holes in the LLM gateway's guards — and each of those is
written up where it happened rather than quietly fixed.

What is missing is mostly operational rather than architectural: erasure, rotation,
retention, audit review, a scheduled sender, a load figure, and a rehearsal from a clean
machine. Those are the next sprint, and §5 says which of them block a pilot.

The thing to be most careful about is the data. Every number in this system is synthetic,
every model is better here than it will be in production, and the product's own honesty
mechanisms — the DEMO DATA ribbon, the UNVERIFIED labels, the caveats on every insight —
exist because a convincing demo on synthetic data is the easiest way to mislead a
stakeholder. Keep them switched on until the data is real.
