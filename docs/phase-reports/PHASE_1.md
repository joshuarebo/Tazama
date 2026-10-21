# Phase 1 — Data layer, seed, discovery

Date: 2026-10-21 · Status: **GREEN**

## What was built
| Area | Delivered |
| --- | --- |
| Domain | `domain/value_objects.py` — MSISDN normalisation (all four Kenyan input shapes, full-width digits, NBSPs; landlines/short codes/foreign numbers refused), `GeoPoint` with haversine distance, `TimeWindow` (half-open), the enum vocabulary (Region, Technology, NodeKind, AlarmSeverity, KpiName, Channel, Language, ComplaintCategory, Priority, IncidentStatus, IncidentCause), `KPI_POLARITY` + `is_degraded`, confidence bands |
| Schema | 30 canonical tables (PRD §7) with Oracle-safe identifiers (≤30 chars), UTC-only timestamps, portable JSON-as-text, explicit string lengths, optimistic-locking versions, **no raw MSISDN column anywhere**, and `ground_truth` held apart from the pipeline |
| Types | `UtcDateTime` (refuses naive datetimes, re-attaches UTC on SQLite), `JsonText` (validating codec), `NonEmptyStr` (makes Oracle's `'' IS NULL` explicit), `Embedding` alias |
| Migrations | `0001_canonical_schema` (30 tables, 116 indexes) and `0002_oracle_json_checks` (23 `IS JSON` constraints, Oracle-only, no-op on SQLite); `upgrade → downgrade → upgrade` verified |
| Engine | Dual backend behind one set of ports (ADR 0002): SQLite with FK/WAL pragmas and a shared in-memory mode; Oracle with pooling, `pool_pre_ping`, recycle, explicit NLS and `TIME_ZONE=UTC`; `session_scope(write=True)` refuses writes when `ALLOW_LIVE_WRITES=false` |
| Paths | `core/paths.py` — repo-root resolution so `make migrate` (runs in `backend/`), `make seed` (repo root) and the container all use the same database and the same config file |
| Source mappings | `config/source_mappings.yaml` v2: 10 mapped entities + 20 app-owned, demo (identity) and live (real Oracle names) blocks, per-source timezones, mandatory time-bound filters, `msisdn_source` marked for boundary pseudonymisation; strict validator catching unknown/missing/required columns, bad timezones, unknown types, join-key errors both sides, filter injection and unknown bind parameters |
| Seed | `infrastructure/db/seed.py` — deterministic, 914,907 rows in ~29 s: 5 regions, 200 sites, 600 cells, core→aggregation→link→site→cell topology (831 nodes, 830 edges), 835,200 KPI rows (30 days hourly + 7 days at 15 min, with daily/weekly seasonality and correlated KPIs), 20,377 complaints across 6 channels in English/Swahili/Sheng/code-switched (360 templates), 6,000 care calls, 30,000 pseudonymised subscribers across all four MSISDN shapes, alarms, maintenance windows, runbooks, knowledge docs (one expired) |
| Scenarios | All 11 labelled scenarios in `ground_truth`, plus a complaint→cell map for every complaint so join coverage and (in Phase 2) site identification can be scored honestly |
| Discovery | Profiler (row counts, null %, cardinality, min/max, freshness, index and PII flags, masked samples), `docs/data-catalog.md` generator, join-coverage + time-alignment report, safe SQL explorer, saved queries, masked CSV export, append-only query audit |
| SQL guard | `modules/discovery/sql_guard.py` — sqlglot AST + pre-parse regex: single statement, SELECT-only, allowlisted tables, no comments, banned functions/constructs, dialect-correct row-cap rewrite, PII masking, `SELECT *`-on-PII refusal, full-scan warning, length/depth/join limits, live-mode read-only flag |
| API | `/api/v1/discovery/*` — tables, catalog, per-table profile, join-coverage, mapping validation, query, query/explain, query/export, query/history, saved-queries CRUD; every endpoint declares a capability |

## Gate results
```
$ ./make.ps1 ci
==> lint            ruff: All checks passed!  ·  import-linter: Contracts: 5 kept, 0 broken
==> typecheck       mypy: Success, no issues in 64 source files (strict on core/domain/modules)
==> test            479 passed, 1 warning in 59.75s
==> coverage-gate   domain 99.45% · modules 89.58% · core 92.11% · overall 94.01%   -> ok
==> eval            144 passed (fake-LLM contract + SQL-guard suite)
==> secrets         secret scan: clean
GREEN  ci passed

$ ./make.ps1 migrate seed          # from a deleted database
0001 canonical schema -> 0002 oracle json checks
914,907 rows; content a66afe37d2fba4662e1681face3ae44d2352b79f5519b6f87152352b32622d80
$ ./make.ps1 seed                  # second run, same arguments
914,907 rows; content a66afe37d2fba4662e1681face3ae44d2352b79f5519b6f87152352b32622d80   # identical

$ ./make.ps1 catalog
source mappings v2 (demo mode): 10 mapped entities, 20 app-owned, column coverage 100.0% (120/120)
  no issues
join coverage:
  complaint -> cell                10,593 / 20,377   51.99%
  complaint -> known cell          10,593 / 20,377   51.99%   (no dangling cell ids)
  complaint -> site                      0 / 20,377    0.00%   (Phase 2)
  complaint -> incident                  0 / 20,377    0.00%   (Phase 2)
  complaint -> subscriber          19,763 / 20,377   96.99%
  complaint -> region              18,382 / 20,377   90.21%
  care call  -> cell                2,296 /  6,000   38.27%
  complaint -> cell (ceiling)      20,377 / 20,377  100.00%   per seeded ground truth
time alignment: 100.00% inside the KPI window · 0.20% late (>1h) · 0.00% clock skew
warning: Only 52.0% of complaints resolve to a cell. Site identification is the bottleneck.
wrote docs/data-catalog.md - 30 tables, 914,907 rows
```

**The day-one baseline for the hardest problem: 51.99% of complaints carry a resolvable
cell, against a 100% ceiling.** Phase 2's site identification is measured against that gap.

Test shape: 479 tests — 174 from Phase 0, plus SQL guard 127 (13 accepted shapes, 17
DML/DDL verbs, 12 dangerous functions, 7 dangerous constructs, a 300-example hypothesis
property test), seed determinism 10, seed scenarios 12, mappings 25, profiler/coverage 16,
discovery API 37, schema contract 24, value objects 37.

## Assumptions (stated, per PRD §0)
1. **KPI_CELL is wide, not long.** One row per (cell, bucket) with ten KPI columns instead
   of ten rows: 835k rows instead of 8.3m, directly usable by the Phase 3 feature builder,
   and `granularity` carries the two resolutions the PRD asks for.
2. **KPI resolution is 30 days hourly + 7 days at 15 minutes.** Full 15-minute data for 30
   days would be ~1.7m rows and push the seed past the time budget for no analytical gain;
   `--fine-days` makes it a parameter, not a hard-coded choice.
3. **Legacy-technology KPI baselines are deliberately generous.** The thresholds in
   `KPI_POLARITY` are LTE-shaped, so seeded 2G/3G cells are given baselines that sit just
   inside them — otherwise every legacy cell would read as permanently degraded. Flagged
   UNVERIFIED in `docs/data-catalog.md`; per-technology thresholds are needed before live use.
4. **The seed leaves the pipeline's work undone.** `cell_id` is populated on ~52% of
   complaints (what a source system plausibly knows); `site_id`, `site_confidence`,
   `incident_id`, `is_network`, `sentiment` and `urgency` are NULL. The true cell for every
   complaint is in `ground_truth`, which only the seed writes and only evals read.
5. **All 11 scenarios sit in the final 24 hours** of the window, so a `days=2` test seed
   contains every one of them and no scenario test needs to be slow.
6. **The content hash is salt- and generator-dependent.** It digests row payloads (not a
   read-back), excludes DB-assigned autoincrement keys, and includes HMAC subscriber
   tokens — so it is reproducible for a given `PSEUDONYM_SALT` and generator version, which
   is the property `make seed` guarantees.
7. **`kpi_cell` secondary indexes are dropped and rebuilt around the bulk load on SQLite
   only.** On Oracle, DDL commits implicitly, so a crashed seed could leave an index
   missing; the optimisation is skipped there. The unique constraint is never dropped.
8. **The explorer allowlist is a policy copy.** `sql_guard` must not import infrastructure,
   so it keeps its own copy of the table and PII-column lists; three tests assert the copies
   match `infrastructure.db.models` exactly, including that `SELECT *` cannot leak PII.
9. **Audit, query history, ground truth, seed provenance and saved queries are not
   queryable** through the explorer, for any role. The allowlist is the control, not the role.

## Corrections made during the phase
* **The import-linter layering was wrong, not the code.** `infrastructure → modules` is the
  correct direction (an adapter must reference the port and DTOs it satisfies), so the layer
  order now puts `infrastructure` above `modules`. The separate contract forbidding
  `modules → infrastructure` still enforces the direction that matters. Caught by a subagent.
* **Two working-directory bugs**, both the same class: a relative `config/` path and a
  relative `SQLITE_PATH` resolved differently under `make` (repo root), pytest (`backend/`)
  and the container. Both now go through `core/paths.py`.
* **A forward-referenced FastAPI dependency** (`Annotated["DiscoveryService", Depends(...)]`)
  made Pydantic treat the service as an unresolved query parameter and broke the OpenAPI
  document. Resolved at runtime now.
* **PowerShell 5.1 reads UTF-8 without a BOM as ANSI**, which mangled non-ASCII characters
  in `models.py` during an in-place edit. Repaired, and those files are now plain ASCII.

## Known gaps (Phase 2 and beyond)
* No pipeline yet: nothing populates `incident`, `incident_link`, `diagnosis`,
  `case_record`, `notification`, `verification` or `outbox_event`. Hence 0% on the
  complaint→site and complaint→incident coverage rows.
* `/readyz` vector and outbox probes still report `skipped`; the database probe is live.
* `knowledge_doc.embedding` is NULL until Phase 4 computes embeddings.
* Oracle integration suite is still unexercised — `make test-it` reports *skipped* while the
  Docker daemon is down (tech-debt #2).
* The seed's `reset` guard is deliberately strict; `make seed` passes `--reset` so
  `make demo` stays idempotent.

## Next
Phase 2 — ingestion → site identification → correlation → incidents → impact →
prioritisation/routing → notification proposals → verification, on the transactional outbox,
with the scenario suite scored against the 11 seeded ground-truth scenarios and the
51.99% join-coverage baseline recorded above.
