# ADR 0001 — Stack and shape

Status: accepted · Date: 2026-10-21 · Phase: 0

## Context
TAZAMA must correlate care contacts with network telemetry, explain incidents, and keep a
full audit trail, in an Oracle-centric enterprise with a Microsoft-centric collaboration
stack and an internal Qwen LLM endpoint. The team's strongest skills are Python and SQL.

## Decision
A **modular monolith**, API-first, event-driven internally, ports-and-adapters.

| Choice | Why (one line each) |
| --- | --- |
| Python 3.12 | Team skill, and the data/ML ecosystem the insights module needs. |
| FastAPI + Pydantic v2 | Typed request/response contracts and OpenAPI for free. |
| SQLAlchemy 2 + python-oracledb (thin) | Oracle access with no Instant Client install. |
| Alembic | Reviewable, reversible migrations. |
| uv | Fast, lockfile-first dependency management. |
| Next.js + TypeScript + Tailwind | One deployable frontend with SSR and strong typing. |
| ECharts / MapLibre | Dense charts; maps that still work with no tile server. |
| OpenTelemetry | One tracing vocabulary across API, worker and LLM calls. |
| tzdata | Windows ships no IANA database; `Africa/Nairobi` must resolve everywhere. |
| sqlglot | SQL parsed into an AST so the Data Explorer guard is not regex-only. |
| PyJWT | Dev-mode sessions and OIDC token validation through one code path. |

Rejected: microservices (no team or traffic reason, and a distributed audit trail is
harder to trust); Django (ORM-first, less natural for event flows); Java/Spring (no
in-team depth).

## Consequences
Module boundaries are the only thing preventing the monolith from rotting, so they are
enforced mechanically by import-linter contracts in `make lint`, not by convention.
