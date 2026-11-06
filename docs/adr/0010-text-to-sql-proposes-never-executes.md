# ADR 0010 — Text-to-SQL proposes; it never executes

**Status:** accepted (Phase 7)
**Supersedes:** nothing
**Related:** ADR 0003 (SQL guard), `docs/architecture/algorithm-choices.md` §7

---

## Context

The Data Explorer answers "explore the data" safely: SELECT-only, AST-parsed, an allowlist
of tables, a mandatory row cap, a timeout, PII masking and an audit row per attempt. It also
requires someone who can write SQL.

Natural-language querying removes that requirement and is the single most requested AI
feature in products of this shape. It is also the highest-risk AI surface in a system holding
subscriber data: it converts a sentence into executable code against the warehouse.

My engineering recommendation was to defer it to the roadmap — the Explorer already covers
the need, accuracy cannot be measured on a synthetic schema, and the feature demos far
better than it de-risks. The product owner decided to build it now, which is their call. This
ADR records the design that makes that decision defensible.

## Decision

**The model writes SQL. A human reads it. The existing guard executes it.**

```
question → POST /api/v1/ai/sql → {sql, explanation, tables_used, confidence, guard_verdict}
         → rendered as code, with the guard's verdict beside it, NOT executed
         → the user presses "Use this query" → it lands in the editor
         → the user presses "Run" → POST /api/v1/discovery/query → the guard executes
```

Four consequences, each deliberate:

1. **No execution path exists on the AI route.** Not "is disabled" — does not exist. The
   route's response type carries a string and a verdict. A test asserts the absence of three
   plausible execution endpoints, which is worth more than any assertion about a 200.

2. **Three independent layers, attacked separately.** The `SqlSuggestion` schema refuses
   anything that is not a single read, so an obviously-bad suggestion never reaches a screen.
   The guard — unchanged, reused as-is — gives a verdict without running anything, then is
   the sole executor. A person approves.

3. **The audit row belongs to the human.** Suggesting a query writes nothing to
   `query_audit`, because no query ran. The audit entry appears when the person presses Run,
   attributed to them — which is the correct actor for a query a human approved.

4. **Gated on `explorer:query`, not `ai:ask`.** Suggesting a query to someone who may not
   run one is theatre. The two surfaces have identical audiences by construction.

## Alternatives considered

**Let the model query directly and show the results.** Rejected. It removes the only step
that catches the dangerous failure mode, which is not a destructive statement — the schema
and guard stop those — but a query that is *valid, permitted and subtly wrong*: a join that
double-counts, a window off by an hour, a filter that silently excludes half the rows. A
wrong number presented as an answer, in front of leadership, is the actual risk. Showing the
SQL makes that failure reviewable; showing only results makes it invisible.

**A denylist of dangerous keywords.** Rejected. The guard was already an allowlist for good
reason, and a model is an excellent generator of phrasings a denylist has not seen.

**Fine-tune or few-shot against the real schema.** Deferred. It would improve accuracy, and
accuracy cannot be measured until there is real data and an eval set of question→SQL pairs
from real usage. Adding tuning before measurement is adding confidence without evidence.

**Defer the whole feature.** My recommendation, overruled with reasons. Recorded here so the
trade-off is visible rather than lost: what shipped is a safe *mechanism* with
**unmeasured accuracy**, and `docs/ROADMAP.md` §8 carries that as an explicit gap.

## Consequences

**Good.** Anyone who can read SQL can now answer their own questions, and the reading step
doubles as the review step. The guard needed no change, so the existing ~40 attack tests
still cover the only execution path. The feature degrades cleanly: with no model configured
the box reports that no suggestion could be made and points at the examples.

**Bad.** It is slower than asking a question and getting an answer, and someone will ask for
the approval step to be removed. The answer is no, and this ADR is why.

**Honest.** Accuracy is unmeasured. The fake model server returns one canned query, so the
tests prove the *chain* works — schema, guard, execution, audit — not that a real model
writes good SQL. Treat the feature as a demonstrated mechanism, not a demonstrated
capability, until §8 of the roadmap is done.
