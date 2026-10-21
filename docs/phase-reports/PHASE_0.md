# Phase 0 — Foundations and harness

Date: 2026-10-21 · Status: **GREEN**

## What was built
| Area | Delivered |
| --- | --- |
| Repo | `PRD.md` (source of truth), `CLAUDE.md` (§19 operating rules incl. the 9 security layers and the 5 agent layers), `README.md`, `.gitignore`, `.env.example` (60 variables) |
| Config | `core/settings.py` — the single `pydantic-settings` module; fails fast listing every offending variable; live-mode lockdown; placeholder-secret refusal outside dev; `CORS_ORIGINS='*'` refused |
| Core | `clock.py` (injected time, UTC-only, bucket flooring, half-open windows), `ids.py` (time-sortable + deterministic ids, correlation ids), `errors.py` (problem+json, RFC 9457), `logging.py` (JSON logs with hard redaction), `security.py` (HMAC pseudonymiser, RBAC capability matrix, token bucket, safe paths, output sanitiser), `auth.py` (pinned-algorithm JWT, dev users), `tracing.py` (OTel, scrubbed span attributes) |
| API | App factory with ordered middleware (correlation id → security headers → body limit → rate limit → CORS), problem+json handlers for every failure class, `/healthz`, `/readyz` (5 probes), `/api/v1/meta`, `/api/v1/auth/{login,whoami,oidc}`, OpenAPI published |
| Harness | `scripts/mk.py` (37 targets; the single implementation), `Makefile` + `make.ps1` forwarders, `scripts/doctor.py`, `scripts/fake_llm.py` (deterministic + 9 injectable failure modes), `scan_secrets.py`, `coverage_gate.py`, `sbom.py`, `export_openapi.py`, plus CLI stubs for seed/catalog/replay |
| Containers | `backend/Dockerfile` (multi-stage, non-root uid 10001, healthcheck), `docker-compose.yml` with `oracle`, `web`, `fake-llm`, `observability` profiles, env-driven throughout |
| CI | `.github/workflows/ci.yml` (lint, mypy, boundaries, tests+coverage, coverage gate, evals, task-runner parity, doctor, OpenAPI, SBOM, pip-audit, gitleaks + fallback scanner, frontend job guarded until Phase 5), `.pre-commit-config.yaml` (ruff, mypy, gitleaks, boundaries, env parity, no-commit-to-main) |
| Docs | ADRs 0001–0008, `docs/security/security-baseline.md` (9 layers + the full web/LLM risk checklist, each mapped to its test), `docs/architecture/{overview,agent-layers,module-boundaries}.md`, `docs/tech-debt.md` (15 items) |

## Gate results
```
$ ./make.ps1 doctor
doctor: OK (10 warning(s) — see fixes above)      # warnings are later-phase subsystems +
                                                  # Docker daemon down + optional gitleaks

$ ./make.ps1 ci
==> lint          ruff check + ruff format --check + import-linter: 5 contracts kept, 0 broken
==> typecheck     mypy: Success, no issues in 50 source files (strict on core/domain/modules)
==> test          174 passed, 1 warning in 27.61s
==> coverage-gate domain 100% · modules 100% · core 100% · overall 92.07%  -> ok
==> eval          17 passed (fake-LLM contract: determinism + 9 failure injections)
==> secrets       secret scan: clean
GREEN  ci passed

$ docker compose --profile oracle --profile web --profile fake-llm --profile observability config
exit 0   (all four profiles parse; env substitution resolved)
```

Test shape: 174 tests — settings contract (22), clock/ids incl. a hypothesis property
test (18), logging redaction (9), core security (33 security-marked), auth + HTTP edge
(24 security-marked), health/meta/openapi/CORS (8), repo contract (6), fake-LLM
contract (17), errors (16).

## Assumptions (stated, per PRD §0)
1. **Dual DB backend** (ADR 0002, user-approved): SQLite is the default for demo/CI;
   Oracle Free carries the integration suite. A deviation from PRD §5, recorded in
   `docs/tech-debt.md` #2. `DB_BACKEND` selects; the same contract tests run against both.
2. **The fake LLM is the eval gate** (ADR 0007, user-approved); real-Qwen evals are wired
   and non-blocking, as PRD §9 allows.
3. **Python 3.12.12 via uv** (pinned in `backend/.python-version`), although the machine's
   system Python is 3.13 — the PRD asks for 3.12 and the container uses 3.12-slim.
4. **`make` is absent on Windows**, so `scripts/mk.py` is the single implementation with
   `Makefile` and `make.ps1` as forwarders (ADR 0006); a test asserts the three agree.
5. **`tzdata` added as a dependency** — Windows ships no IANA database and
   `DISPLAY_TZ=Africa/Nairobi` must resolve everywhere. Justified in ADR 0001.
6. **`sqlglot` and `PyJWT` added** ahead of their phases (SQL guard; dev/OIDC tokens) so
   the doctor can verify the whole dependency set at once. Justified in ADR 0001.
7. **Capability names are ours** (`incident:approve`, `explorer:query`, …): the PRD names
   roles, not capabilities, so endpoints declare a capability and the matrix maps it to
   roles — least privilege with admin enumerated, not a wildcard.
8. **Readiness reports `skipped`, never `ok`, for subsystems a later phase owns**, so a
   green probe always means a real check ran.

## Deviations from the PRD
| PRD says | Built | Why |
| --- | --- | --- |
| §5 Oracle Free everywhere | Oracle **and** SQLite behind one port set | Gates must be green on a clean machine without a 4 GB pull and a running daemon (ADR 0002) |
| §16 gate: "`make up` healthy" | `docker compose config` validated for all profiles; `make up` refuses with a clear message while the daemon is down | The Docker daemon is not running on the build machine; images and compose file are complete and will start once it is |

## Known gaps (closed by later phases)
* No database schema, migrations, seed or source mappings yet — Phase 1.
* `modules/*` are empty packages with docstrings; `domain/` has no entities yet — Phase 1/2.
* `/readyz` database, vector and outbox probes report `skipped` — Phases 1, 2, 4.
* No frontend; `web/` does not exist and its CI job self-skips — Phase 5.
* `gitleaks` is not installed locally, so `make secrets` uses the bundled fallback
  scanner; CI runs the real gitleaks action.
* Runbooks and the threat model are placeholders until Phase 6.

## Next
Phase 1 — canonical schema + migrations, source-mapping layer, deterministic seed with
all 11 labelled scenarios, profiler/data catalog, join-coverage report, and the safe SQL
explorer with its ~40-case attack suite.
