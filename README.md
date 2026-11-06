# TAZAMA — Smart Optimization Intelligence Platform

Proactive network-care intelligence: correlate customer complaints and care calls with
network KPIs and alarms, detect and explain incidents, recommend evidence-backed actions,
propose human-approved proactive notifications, and report to Microsoft Teams.

> **Synthetic data only.** Every subscriber, site, cell, complaint and KPI in this repo is
> generated. No real endpoints, credentials or customer data.
>
> **Brand placeholder.** Colours and the wordmark in `web/` are UNVERIFIED placeholders.
> Replace them with official brand values before any external use — see
> `web/src/styles/tokens.css`.

## Quick start

```bash
git clone https://github.com/joshuarebo/Tazama.git && cd Tazama
cp .env.example .env            # demo defaults; no secrets needed
make bootstrap                  # or: ./make.ps1 bootstrap   (Windows, no make needed)
make doctor                     # environment check with actionable fixes
make demo                       # migrate + seed + start API, worker and web
```

Then open <http://localhost:3000> (web) and <http://localhost:8000/docs> (API).

`make demo` starts the deterministic model server too, so the AI panels are live without a
real endpoint and without anything leaving the machine.

`make`, `./make.ps1 <target>` and `python scripts/mk.py <target>` are interchangeable
(ADR 0006). Run `make help` for the full list.

## First five minutes

Sign in as **Admin** — that role holds every capability; the others are deliberately
narrower and the server enforces it.

1. **Command Center** — expect 17 live incidents, 2 suppressed as planned maintenance,
   ~140,201 subscribers affected, 69 complaints correlated. Note the
   *complaint → cell coverage* figure top right: that is the hardest problem in the product,
   measured on every page load.
2. **An incident card** — click any title. Impact, probable cause **with its evidence rows**,
   KPI curves marked at the incident start, and the decision trail.
3. **Press Diagnose** on that card, then expand *"What the agent did"*. Tools called, tools
   **refused**, budget spent. That panel is how "the AI cannot act" becomes checkable.
4. **Data Explorer** — ask *"which regions had the most complaints last week"* in plain
   English, read the SQL it writes, then run it. Then click *"Try to delete data"* and watch
   the guard refuse it.
5. **Stop the model server** and reload. Everything still works, the method says `rules`,
   and the header says *AI degraded*.

Presenting this to someone? `docs/DEMO_SCRIPT.md` is the script, with the numbers and the
honest answer to every question that gets asked.

## Everyday commands
| Command | What it does |
| --- | --- |
| `make ci` | The gate: lint, types, module boundaries, tests, coverage, evals, secret scan |
| `make test` / `make test-unit` | Full suite / fast unit loop |
| `make test-it` | Oracle integration suite (needs Docker; reports *skipped* if absent) |
| `make seed` | Deterministic synthetic data with all labelled scenarios |
| `make pipeline` | Run detection over the seeded data, so there are incidents to look at |
| `make catalog` | Data catalog + the join-coverage number (the metric that matters) |
| `make train` | Train the forecast and the anomaly detector; fails if either stops beating its baseline |
| `make a11y` | Accessibility and design-token contrast checks |
| `make eval` / `make eval-live` | Agent evals against the fake LLM / real Qwen (advisory) |
| `make up` / `make down` | docker-compose stack |
| `make fake-llm` | Deterministic model server on :8088 for offline work |

## Repository map
| Path | Contents |
| --- | --- |
| `PRD.md` | Product requirements — the source of truth |
| `CLAUDE.md` | Operating rules for anyone (human or agent) changing this repo |
| `backend/src/tazama/` | `core` · `domain` · `modules` · `ai` · `infrastructure` · `api` · `worker` |
| `web/` | Next.js app and design system (Phase 5) |
| `config/` | Source mappings, policies, runbooks, taxonomy, notification templates |
| `scripts/` | Task runner, doctor, seed, catalog, replay, fake LLM, SBOM |
| `docs/` | ADRs, architecture, security baseline, runbooks, phase reports, model cards |

## Configuration
Everything comes from environment variables, read in exactly one place
(`backend/src/tazama/core/settings.py`). Start from `.env.example`; the app fails fast and
lists every missing or invalid variable. `.env` is gitignored and must never be committed.

`APP_MODE=demo` uses seeded data and allows writes. `APP_MODE=live` reads real sources
through `config/source_mappings.yaml` with a read-only user and `ALLOW_LIVE_WRITES=false`.
Switching is configuration, never code.

## Security posture
Nine enforced layers — input validation, prompt-injection guard, rate limiting, tool
argument validation, SQL guard, PII pseudonymisation, output sanitisation, append-only
audit, human approval — plus RBAC with server-side authorisation, CSP and security
headers, CORS allowlist, secret scanning, pinned lockfiles and an SBOM per build. Details
and the test for each control: `docs/security/security-baseline.md`.

AI recommends, rules decide, humans approve. All model traffic goes through one gateway,
every output is schema-validated, and the system keeps working with the model offline.

## Status

Phases 0–7 complete, each with a gate and a report in `docs/phase-reports/`.

| | |
| --- | --- |
| `make ci` | lint, format, 5/5 module-boundary contracts, mypy strict, 1,650+ tests, coverage, evals, secret scan |
| Detection | precision 1.00 / recall 1.00 on the labelled scenarios |
| Diagnosis | 7/7 scenarios top-1, by rules alone, with no model consulted |
| Forecast | beats seasonal-naive by 15.7%, and on the hours where demand actually occurred |
| Frontend | 25/25 Playwright journeys, axe clean on every page, `npm audit` 0 vulnerabilities |

**What is left is plugging in data** — one mappings file and a set of environment variables,
no code change (`docs/FINAL_REPORT.md` §6). Three privacy procedures must exist first:
erasure, salt rotation, and a minimum-exposure floor on insights. `docs/ROADMAP.md` has the
full picture.

## Where to read next

| Question | Document |
| --- | --- |
| How do I present this? | `docs/DEMO_SCRIPT.md` |
| How does it work, end to end? | `docs/architecture/overview.md` |
| Why these algorithms and not others? | `docs/architecture/algorithm-choices.md` |
| What is deliberately not built? | `docs/ROADMAP.md` |
| What would I need to fix before going live? | `docs/FINAL_REPORT.md`, `docs/dpia.md` |
| What are the known compromises? | `docs/tech-debt.md` |
