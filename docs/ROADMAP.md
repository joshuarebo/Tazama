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

## Predictive maintenance

The single largest opportunity, and the one most often misdescribed. Worth stating the
distinction precisely, because it is where this conversation usually goes wrong.

TAZAMA today catches a degradation from KPIs and alarms and connects it to customer impact.
That is **early detection**: the fault has already begun. **Predicting** a failure before it
starts needs telemetry describing a component *wearing out*, and those streams are not in
the KPI feed the product currently reads.

What already exists and would carry this: the per-cell hour-of-week anomaly baseline (§4 of
`architecture/algorithm-choices.md`), the topology and failure-domain model, and the impact
calculation that can quantify what a predicted failure *would* cost. The method is the same
in every case below — establish what normal looks like for that specific component, then
flag departure from it. What is missing is the telemetry, which makes this a data-access
conversation with the network team rather than a modelling problem.

Ordered by how easily each could be added.

### 15. Power and generator degradation

*Today:* mains power cuts, the backup battery drains faster than anyone expected, and the
site goes dark with no warning.

*With this:* track the voltage discharge curve and fuel-level telemetry per site. When a
battery is draining faster than its own historical curve for that load, raise a
battery-health warning hours ahead of the outage, so a field crew refuels or swaps the unit
before customers are affected.

*Unblocked by:* a site power and environment feed — rectifier voltage, battery discharge,
fuel level, door and temperature alarms. **Start here.** It is a single source, the existing
robust z-score transfers directly (a discharge curve is exactly the shape it handles well),
and the payoff is the easiest of the three to verify: the site either went dark or it did
not.

### 16. Optical backhaul degradation

*Today:* transceivers accumulate heat, dust and micro-bends over weeks until the link fails
outright, taking every cell beneath it down at once.

*With this:* ingest bit error rate and frame-error counters per link. When errors climb above
that link's own weekly baseline, flag transceiver wear days before failure — a planned swap
in a maintenance window instead of an emergency at 2am.

*Unblocked by:* transport-layer counters, which usually live in a different system from the
radio KPIs. This fits the architecture well: `topology_edge` already models which cells share
a failure domain, so a degrading link maps onto the cells it would take down and the impact
of a *predicted* failure can be costed before it happens.

### 17. Antenna tilt and RF misalignment

*Today:* wind or a structural shift tilts an antenna, coverage shrinks, adjacent cells absorb
the load, and the first clear signal is a rise in complaints.

*With this:* watch handover success rate and signal quality per sector. A sustained one-sided
drop on a single sector, while its neighbours are unchanged, points at physical misalignment
— identifying it without sending somebody to climb the tower first.

*Unblocked by:* per-sector handover and signal-quality KPIs with neighbour relations, plus a
longer history than the current 336-hour window. This is the hardest of the three to make
trustworthy, because slow drift is easy to confuse with a changing traffic pattern. The
honest way to build it is to start by *measuring* how often the pattern precedes a confirmed
tilt, and to ship nothing until that number exists.

### 18. Survival analysis, once there is failure history

The natural fit for all three above: estimating a component's remaining life rather than
flagging an anomaly in it. Not implementable today at all — it needs a history of actual
failures with timestamps and the replacement record for each, which lives in maintenance
systems rather than KPI feeds.

*Unblocked by:* access to the maintenance and field-work history. Worth asking for at the
same time as the power feed, because the value of §15 goes up sharply once a warning can be
scored against what actually happened.

---

## Complaint understanding, beyond the lexicon

### 19. Topic modelling (LDA or LSA) for category discovery

The lexicon can only find categories somebody thought of in advance. Topic modelling over a
month of real complaints would surface clusters nobody anticipated — a handset model, a
tariff change, a specific app.

The honest framing: this is **exploratory, not operational**. Topics are unlabelled and
drift, so a person reads and names them, and the output is *new categories to add to the
lexicon* rather than a live classifier. It also needs real text in volume: synthetic
complaints are generated from templates, so topic modelling on the seed would only
rediscover those templates.

*Unblocked by:* a month of real complaint text.

### 20. Sentence embeddings for complaint similarity

Grouping complaints that mean the same thing in different words, across English, Swahili and
Sheng, without maintaining phrase lists. Costs the inspectability the lexicon has, and needs
a multilingual model that genuinely handles Sheng.

*Unblocked by:* nothing — this is measurable today against the lexicon's own labels, which
makes it a cheap experiment rather than a leap. It earns its place only if it beats 69.1%
exact / 76.2% binary on held-out complaints.

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
