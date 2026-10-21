# ADR 0005 — Capability probe in front of Oracle AI Vector Search

Status: accepted · Date: 2026-10-21 · Phase: 0 (implemented in Phase 4)

## Context
Oracle AI Vector Search (the `VECTOR` type, `VECTOR_DISTANCE`, vector indexes) exists only
on recent Oracle versions. The deployment target version is not verified, and RAG must
work offline during development.

## Decision
`VECTOR_BACKEND=oracle|memory` selects an adapter behind a `VectorStore` port, and the
Oracle adapter runs a **capability probe** at startup (can we declare a `VECTOR` column?
is `VECTOR_DISTANCE` available? is there a text index for BM25?). An unsupported version
degrades to the in-memory backend and the UI shows the backend in use rather than failing.

Hybrid retrieval (BM25 + vectors + reranker) is fused in application code when native
hybrid search is unavailable, so retrieval quality does not depend on the database version.

`VECTOR_BACKEND=oracle` with `DB_BACKEND != oracle` is refused by the settings validator —
a configuration that cannot work should never start.

## Consequences
Two retrieval code paths to test; both are covered by the same retrieval eval set. The
in-memory backend is not for production volumes, which is recorded in `docs/tech-debt.md`.
