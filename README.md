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

`make`, `./make.ps1 <target>` and `python scripts/mk.py <target>` are interchangeable
(ADR 0006). Run `make help` for the full list.

## Everyday commands
| Command | What it does |
| --- | --- |
| `make ci` | The gate: lint, types, module boundaries, tests, coverage, evals, secret scan |
| `make test` / `make test-unit` | Full suite / fast unit loop |
| `make test-it` | Oracle integration suite (needs Docker; reports *skipped* if absent) |
| `make seed` | Deterministic synthetic data with all labelled scenarios |
| `make catalog` | Data catalog + the join-coverage number (the metric that matters) |
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
Built in phases with a gate per phase; see `docs/phase-reports/`.
