# ADR 0002 — Two database backends behind one set of ports

Status: accepted · Date: 2026-10-21 · Phase: 0 · **Deviation from PRD §5**

## Context
The PRD names Oracle Database Free (container) as the database for every mode. In
practice the Oracle Free image is a multi-gigabyte pull that needs a running Docker
daemon and several minutes to become healthy, and the daemon is not running on the build
machine. Phase gates (`make ci`, `make demo`) must be green on a clean machine, and the
test suite must be fast enough to run on every change.

## Decision
Define persistence as ports (repository protocols) with **two adapters**:

* `infrastructure/db/sqlite/` — default for `APP_MODE=demo`, `make ci`, unit and scenario
  tests. No Docker, no network, sub-second start.
* `infrastructure/db/oracle/` — the production-shaped adapter. All Oracle specifics live
  here: `'' IS NULL`, identifiers ≤ 30 chars, IDENTITY columns, explicit NLS, CLOB/JSON
  with `IS JSON`, bound parameters only, time-bounded partition-friendly queries, pooling
  with health checks.

Selected by `DB_BACKEND`. The *same* contract tests run against both adapters, so a
behaviour that only works on SQLite fails the gate. Alembic revisions are written so they
apply to both (dialect-conditional where Oracle needs different DDL).

## Consequences
* Phase 1–6 gates are reachable without Docker; `make test-it` runs the Oracle suite and
  reports **skipped** (never "passed") when the daemon is down.
* Risk: SQLite hides Oracle-specific defects. Mitigations — contract tests shared across
  adapters, the Oracle suite in CI once a container is available, and an explicit entry in
  `docs/tech-debt.md`.
* LIVE mode is unaffected: it still reads real Oracle through `config/source_mappings.yaml`
  with no code change.
