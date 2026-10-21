# ADR 0003 — Transactional outbox instead of a broker

Status: accepted · Date: 2026-10-21 · Phase: 0 (implemented in Phase 2)

## Context
The pipeline is a chain of domain events from `ComplaintReceived` to `CaseClosed`. It must
be replayable (a disputed incident has to be reproducible), idempotent (duplicate and
out-of-order events are in the edge-case register) and auditable. Adding Kafka would add
infrastructure, operational burden and a second source of truth for an MVP whose event
rate is modest.

## Decision
A transactional **outbox** table in the app schema plus a polling worker, behind an
`EventBus` port.

* The state change and its events are written in one transaction — no lost events, no
  phantom events.
* Each event carries a version, a deterministic id and a correlation id; consumers are
  idempotent on that id.
* `scripts/replay.py` re-feeds recorded events with a fixed clock.
* A Kafka adapter is a later implementation of the same port; nothing above the port
  changes.

## Consequences
Polling adds latency (seconds, configurable) and the outbox needs a lag probe — exposed in
`/readyz` and covered by `docs/runbooks/outbox-backlog.md`. Ordering is per aggregate, not
global, which is what the correlation logic actually needs.
