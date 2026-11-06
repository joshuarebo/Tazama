# Roadmap

What is deliberately not built, and what unblocks each item. Ordered by what stands between
the current demo and a pilot on real data.

Nothing here is a surprise or an oversight — each entry was a decision, and the reasoning is
recorded so it can be revisited rather than re-derived.

---

## Blocking: before any real subscriber record

These three are not technical debt. They are obligations under the Kenya Data Protection
Act, and no amount of architecture substitutes for them. Each is days of work, not months.

### 1. Erasure

Deleting a subscriber's pseudonym token must cascade to their complaints, care calls,
incident links and anything derived from them in analytics. Today it does not, because
nothing has ever needed erasing — the data is generated.

*Unblocked by:* writing and testing the cascade. The token is already the single identity
for a subscriber across every table, so this is one procedure rather than a hunt.

### 2. Salt rotation

Rotating `PSEUDONYM_SALT` breaks the link between historical analytics and current
subscribers, silently. There is no procedure for what happens to the history when it
rotates.

*Unblocked by:* deciding the policy first — re-key the history, or accept the break and
archive — then implementing it. The decision is a conversation with the data owner, not an
engineering choice.

### 3. A minimum-exposure floor on insights

A complaint rate for a twelve-subscriber cell at 03:00 is close to identifying. The insights
surface aggregates and normalises, but has no floor below which a cell is not reported
individually.

*Unblocked by:* agreeing the floor (a subscriber count, or a complaint count) and applying
it in `modules/insights`. The analysis already carries caveats; this adds one more that
suppresses rather than annotates.

---

## Next: operational completeness

### 4. Scheduled Teams delivery

`due_reports` is pure and tested — 07:30 Africa/Nairobi daily, 08:00 Monday weekly, local
time stored and converted at comparison so a timezone rule change cannot silently shift it.
No worker job calls it, and the live sender is unverified because no webhook existed during
the build.

*Unblocked by:* a provisioned Workflows webhook. Send one test card, confirm the payload,
then wire the scheduler and keep `TEAMS_DRY_RUN=true` everywhere except production.

### 5. A load figure

`make load` exists; it has not been run. There is no p99 for the incident list.

*Unblocked by:* nothing. This is an afternoon, and it should happen before a pilot rather
than after someone notices.

### 6. The remaining runbook, and an audit-review routine

Four of five runbooks are written. Separately: the audit log is append-only and complete,
and nobody reads it. An append-only log nobody reads is evidence after the fact, not a
control — care lookups in particular need a review cadence.

*Unblocked by:* agreeing who reviews what, how often.

---

## Then: making the AI earn more of its keep

### 7. Real-model evaluation

Every Phase 4 threshold is a statement about the *harness*, measured against a deterministic
fake server. `make eval-live` is wired and skips cleanly when no endpoint is set.

*Unblocked by:* an internal Qwen endpoint. Expect the numbers to be worse, and expect the
deterministic fallbacks to matter more than they appear to today.

### 8. Text-to-SQL accuracy, measured

The natural-language query path ships as a demo capability. Its safety is structural — the
schema refuses non-reads, the guard is the only execution path, a human approves — but its
*accuracy* is unmeasured, and cannot honestly be measured on a synthetic schema.

*Unblocked by:* real data plus an eval set of question→SQL pairs drawn from real usage. The
failure mode to watch is not a dangerous query; it is a valid, permitted, subtly wrong one —
a join that double-counts, a window off by an hour. That is why the SQL is shown rather than
the answer alone.

### 9. A trained complaint classifier

The lexicon reaches 69.1% exact / 76.2% network-vs-non-network with 30% flagged for review.
The LLM path is built and schema-validated; nothing is trained on top of it.

*Unblocked by:* labelled real complaints. The LLM path can bootstrap the labels, and a small
supervised model then replaces it — cheaper, faster and inspectable.

### 10. Hybrid retrieval

Keyword search with citation verification over a few dozen documents. A vector index and
reranker are machinery without a problem at this corpus size; the vector backend exists
behind a capability probe.

*Unblocked by:* the corpus passing a few hundred chunks.

### 11. Drift monitoring

PSI is implemented and tested. Nothing schedules it.

*Unblocked by:* a model being left active across a month of live data — at which point this
stops being optional.

---

## Later: the things that need a conversation, not code

### 12. A spatio-temporal GNN for correlation

The network is a graph and faults propagate along it, so the intuition is sound. But the
rules score precision 1.00 / recall 1.00 on the labelled scenarios, so there is nothing to
beat, and a GNN would trade an explainable grouping for an embedding.

*Unblocked by:* real data where the rules measurably miss something — and a comparison on
the eval set. The scaffold exists so that comparison is cheap, not so the feature can be
switched on.

### 13. A contextual bandit for notification timing

Learning when a proactive message best prevents a call. The blocker is not technical: a
bandit learns by exploring, and exploring here means deliberately messaging some real
customers at worse times.

*Unblocked by:* a decision about whether that is acceptable, and on what basis. That is a
consent conversation before it is an engineering task.

### 14. Longer forecast horizons

One hour ahead is what the data supports. Care staffing is often planned a shift ahead.

*Unblocked by:* rebuilding the target, not retuning the model — a six-hour-ahead forecast is
a different problem, not the same one with a bigger number.

---

## Explicitly not planned

| | Why not |
| --- | --- |
| **Autonomous remediation** | The absence of any execution path is a feature, and the single clearest safety story this product has. It should stay absent |
| **An LLM writing customer messages** | Templates with slots, always. A model fills slots; it does not write to customers. `NotificationFill` rejects prose in a slot |
| **Reinforcement learning on the live network** | No |
| **A general-purpose chat interface over the data** | The Explorer with an approval step covers the need. An open chat invites questions the system cannot answer safely and makes the boundary impossible to explain |

---

## Frontend and platform odds and ends

- **Component unit tests.** vitest is installed with zero tests; the Playwright journeys and
  the axe/token suite cover behaviour and markup contracts.
- **Console i18n.** Customer-facing templates are bilingual (en/sw); the operator console is
  English only.
- **React 19.** React 18 is deprecated in Next 16 and unsupported in Next 17.
- **Oracle integration suite in CI.** Runs locally with Docker; reported as *skipped* rather
  than silently passed when the daemon is absent.
- **`make demo` from a clean clone.** Every step has been run in sequence from a wiped
  database; a fresh clone with empty volumes is a different test and has not been done.

`docs/tech-debt.md` carries the full list with escalation triggers.
