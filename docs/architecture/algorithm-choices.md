# Why these algorithms

Every modelling choice in TAZAMA, in plain language, with the alternative it was chosen
over. Written so a non-specialist can follow the reasoning and a specialist can check it.

One principle runs through all of it, from the PRD: **classical before clever.** A baseline
before a model, a rule before a network, and nothing ships until it beats the thing it
replaces on data it has not seen.

---

## 1. Correlation: does this complaint belong to that fault?

**What it does.** Groups complaints and network degradations into a single incident using
three signals: *where* (the cell, and the nodes upstream of it), *when* (a time window), and
*what* (the complaint's category).

**Chosen: rule-based spatio-temporal clustering with a topology join.**

**Over: a learned clustering model.** Two reasons, and the second is the real one.

The first is that the rules work: on the eleven labelled scenarios in the seed, detection
scores **precision 1.00 and recall 1.00** against floors of 0.85 and 0.80. A model cannot
beat that, and would cost explainability to match it.

The second is that an engineer has to defend the grouping. When someone asks "why are these
nine cells one incident and not nine?", the answer is *"they share an upstream aggregation
node, and the alarm is on that node"* — a sentence, pointing at a row. A clustering model
would answer with a distance metric, and that is not an answer anyone can act on at 3am.

**The part that is genuinely hard.** Not the clustering — the *join*. Matching a complaint
to the cell serving that customer is where the uncertainty lives, so it is measured on every
run (`make catalog`) and shown on the Command Center. It is currently **52.0%**, and that
number is the honest ceiling on how much of the customer signal the product can use.

---

## 2. Diagnosis: what is actually wrong?

**Chosen: deterministic rules first, an LLM only where the rules cannot separate two
causes.** The rules are consulted first and, above 0.80 certainty, the model is **not called
at all**.

**Over: an LLM-first pipeline.** Not to save money. Three better reasons:

- A rule that is certain is a *better* answer than a model that agrees with it: it is
  explainable, free, instant, and it cannot be talked into a different answer by the text of
  a complaint.
- The rules are ordered by how conclusive each signal is, which encodes real operational
  knowledge: planned maintenance explains an incident completely, so it is checked first; a
  transmission alarm on a shared node explains both the fault *and* its footprint, so it
  comes next; congestion is last among the positives because high utilisation accompanies
  plenty of other faults.
- It makes the model's contribution measurable. Because `method` records `rules` or `llm`
  per diagnosis, you can see exactly how often the model added anything.

**Measured:** the rules path alone diagnoses **7 of 7** labelled scenarios, top-1, with no
model involved.

**When the model does answer**, every reference it cites is checked against the evidence the
agent was actually shown. A diagnosis citing `ALM-9999` when no such alarm was provided is
discarded entirely — a fabricated justification is worse than no diagnosis, because it
survives review.

---

## 3. Prioritisation: which incident first?

**Chosen: a transparent weighted rule** over subscribers affected, enterprise exposure, SLA
risk, complaint volume and duration — with the contribution of each factor stored alongside
the score.

**Over: a trained ranker.** A P1 has to be defensible months later, in a conversation where
someone disputes it. "Subscribers affected contributed 0.4, SLA risk 0.3" is a conversation.
A ranker's output is not.

A gradient-boosted priority model exists and is registered, but the rule remains in control
and the model card says why: its labels are distilled from the existing rule, so it can only
learn to imitate it. It becomes useful when there are real outcome labels — resolution time,
repeat-contact rate — to learn from instead.

---

## 4. Anomaly detection: is this cell behaving unusually?

**Chosen: a robust z-score against each cell's own hour-of-week baseline**, using the median
absolute deviation rather than the standard deviation.

**Over a global threshold** (for example "availability below 95%"): a threshold cannot tell
the difference between a cell that is always busy at 8pm and a cell that has just broken.
Mobile traffic is profoundly seasonal — hour of day, day of week — so the comparison has to
be against *this cell at this hour of this weekday*.

**Over a mean and standard deviation**: a single catastrophic hour drags both, so the fault
inflates the very baseline it should be measured against. The median and MAD barely move,
which is the whole point of "robust".

**Over an unsupervised model** (Isolation Forest, autoencoder): one is implemented for the
multivariate case the z-score misses, but the z-score leads because it is *attributable*. It
says "availability on this cell, at this hour, is 11 robust deviations below its own norm" —
which names the KPI and the cell. A model saying "this row is anomalous" starts the
investigation over.

**The gate is recall plus alert volume, not precision.** Measured: recall **0.600** against
the seeded incident windows, flagging **3.93%** of cell-hours. Precision is recorded (~0.015)
but deliberately not gated, because the labels are *incidents* — and an incident exists only
where correlation had enough evidence to raise one, so a real degradation that produced no
incident counts as a false positive. Gating on a number that cannot be achieved teaches a
team to switch the gate off. The flag rate answers the question that actually matters: *will
this drown whoever is on shift?*

---

## 5. Call-surge forecasting: how many contacts, where, next hour?

This one is worth reading in full, because the first version of the gate passed on a model
that had learned nothing.

**Chosen: LightGBM with a Poisson objective, at region-hour granularity, against a
seasonal-naive baseline.**

### Why gradient boosting, not deep learning

The data is tabular, the volume is modest (thousands of rows, not millions), and the
features are human-meaningful lags of named KPIs. Gradient boosting is the strongest family
on exactly that shape, trains in seconds, is reproducible to the last digit with a fixed
seed, and pairs with SHAP so every prediction can be attributed to specific features. A
neural network would need far more data to match it and would give up the attribution.

### Why Poisson, not squared error or absolute error

The first version used `regression_l1` (absolute error) — a sensible-looking choice, since
MAE is the headline metric. It produced a model that **predicted a constant zero** and beat
the baseline by 40%.

The target — complaints per 1,000 subscribers per cell-hour — is **97.6% zeros**. The
optimum of absolute error on a mostly-zero series is its median, which is zero. So the model
correctly learned to predict nothing, scored an excellent MAE, and beat a seasonal-naive
baseline that honestly carries last week's spikes forward and is therefore wrong whenever a
spike does not repeat.

The symptom was visible all along: every SHAP value printed as `0.00000`. A model with 400
trees and zero attribution is a model whose output does not depend on its input.

Poisson deviance is the right objective for non-negative counts, and unlike absolute error
it cannot collapse to the median.

### Why region-hour, not cell-hour

Measured: **97.6% zeros per cell-hour, 16.7% per region-hour.** No objective beat the
baseline on the hours that mattered at cell granularity — the signal is not there.

It is also the right unit on the merits. A call-surge forecast exists so care staffing can
be prepared, and staffing is planned per region, never per cell. Aggregation happens *before*
the lagged features are built, so each lag describes the region's own history rather than an
average of per-cell lags.

A related trap was caught on the way: the region frame was initially built from a 60-cell
subsample, which makes the "regional rate" a rate over an arbitrary tenth of each region.
The same model went from beating the baseline by 18% to losing by 3% purely because of the
subsample.

### What the gate checks now

Overall MAE is necessary but nowhere near sufficient, so three conditions must all hold:

1. the model beats the baseline on overall MAE by at least 5%;
2. its predictions have non-zero variance (a constant predictor fails outright);
3. it beats the baseline on the rows where **demand actually occurred**.

**Measured:** test MAE 0.0057 against the baseline's 0.0067 (**+15.7%**), prediction
standard deviation 0.0045, and it wins on the non-zero rows. The top SHAP drivers are
`hour_of_day`, then setup-success rates — *falling* setup success raises complaints — then
active users and the busy-hour flag. Every sign is the direction physics requires, which is
the best available evidence that it learned something real rather than fitting noise.

### Validation

Time-ordered only: train → gap → validation → test, taken from the end backwards, with the
gap at least as wide as the longest feature lag so a training row's history cannot touch
validation. Cross-validation is expanding-window. **There is no shuffled split anywhere**,
and there should never be: shuffling a time series trains on the future.

A leakage gate runs before a single tree is grown and is deliberately *name-independent* — a
column honestly called `availability_pct_lag1` that was actually built with `shift(-1)` is
caught, because that is how leakage arrives in practice.

---

## 6. Complaint classification: what is this person telling us?

**Chosen: a deterministic multilingual lexicon** over English, Swahili, Sheng and
code-switched text, with phrase matching weighted above single words.

**Over a trained text classifier (or an LLM) as the primary path**, for now: it is
inspectable, instant, free, and it was tuned against the seed's own labels so its accuracy
is known — **69.1% exact, 76.2% network-vs-non-network, with 30% flagged for human review.**
Those are modest numbers, stated honestly, and the 30% review rate is the feature: a lexical
guess that knows it is a guess routes to a person.

Two refinements worth knowing:

- A category matched only by *suggestive* words can never clear the confidence floor,
  however many of them appear. Three vague words are still a guess.
- A complaint that plainly mentions the network but names no symptom ("mtandao ni mbaya" —
  "the network is bad") counts towards an incident while staying unclassified. That phrasing
  is an enormous share of real traffic, and treating it as non-network would hide exactly
  the signal the product exists to catch.

The LLM path is built and schema-validated; it is not the default because nothing has yet
been trained on top of it.

---

## 7. Natural-language querying: ask a question, get SQL

**Chosen: the model writes SQL, a human reads it, the existing guard executes it.**

**Over letting the model query directly.** This is the highest-risk AI surface in a system
holding subscriber data, and the mitigation is structural rather than behavioural:

```
question → model writes SQL → schema check (single read?) → guard verdict (shown, not run)
         → a person reads it → a person presses Run → guard parses the AST and executes
```

Three independent layers, each attacked separately in the tests:

1. **The schema** refuses anything that is not one SELECT — so `DROP TABLE`, or a second
   statement smuggled after a semicolon, never reaches a screen.
2. **The guard** parses the AST (not string matching), enforces an *allowlist* of tables
   rather than a denylist, caps rows, sets a timeout, masks PII columns and audits every
   attempt.
3. **A person** approves. The audit row is attributed to them, which is correct: they
   approved it.

The test that matters most simulates a *fully compromised* model that returns
`DROP TABLE complaint`. Nothing happens — the suggestion is rejected and there is no
execution path on the route at all.

**What this does not solve:** a query that is valid, permitted, and subtly wrong — a join
that double-counts, a window off by an hour. That is why the SQL is shown rather than the
answer alone, and why accuracy cannot honestly be claimed until it has been measured against
real questions on real data.

---

## 8. Retrieval for the knowledge assistant

**Chosen: keyword search over the SOP corpus, with expiry filtering and mandatory
citations.**

**Over a vector index with a reranker**: the corpus is a few dozen documents. A hybrid
retriever over a dozen chunks is machinery without a problem. What makes an answer *safe* is
not retrieval sophistication — it is citation integrity, and that is built:

- An answer citing a document that was never retrieved is **rejected outright**. A
  fabricated citation is worse than an uncited answer because it passes a reviewer's glance.
- **Expired procedures are never retrieved.** A stale SOP is worse than no SOP, because an
  engineer acts on it. (The seed contains a deliberately superseded escalation tree, so
  asking about escalation correctly returns nothing.)
- No retrieval means **no answer**. The assistant declines rather than drawing on whatever
  the model remembers about mobile networks in general — which might belong to a different
  operator's network entirely.

The vector backend exists behind a capability probe for when the corpus grows.

---

## 9. What is built and deliberately switched off

Both are registered, both refuse to run, and both are gated on the same condition.

**A spatio-temporal graph neural network** for correlation. The intuition is sound — the
network *is* a graph and faults propagate along it. But the rules already score 1.00/1.00 on
the labelled scenarios, so there is nothing to beat, and a GNN would replace an explainable
answer with an embedding.

**A contextual bandit** for notification timing — learning when a proactive message is most
likely to prevent a call. The blocker is not technical: a bandit learns by exploring, and
exploring here means deliberately sending some messages at worse times to real customers.
That needs a conversation about consent before it needs code.

Per the PRD, neither is enabled until it beats the baseline on the eval set. The scaffolds
exist so that comparison is cheap to run, not so the feature can be quietly switched on.

---

## 10. The pattern, if you only remember one thing

Every choice above follows the same shape:

| | |
| --- | --- |
| **Start with the dumbest thing that could work** | seasonal-naive, a lexicon, a weighted rule |
| **Measure it honestly** | on data the model has not seen, in time order |
| **Only then add a model** | and only if it beats that baseline on the metric that matters |
| **Keep the baseline running** | it is the fallback, and the product works on it alone |
| **Record which path answered** | `method: rules` or `method: llm`, on every decision |

The last two are what make the system safe to operate. The product does not degrade from
"AI" to "broken" — it degrades from "AI" to "the rules", which score 7/7 on diagnosis and
1.00/1.00 on detection. That is not a fallback anyone needs to apologise for.
