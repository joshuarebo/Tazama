# TAZAMA — demo script

A 15-minute walkthrough for senior leadership, with the exact numbers to expect and the
honest answer to every question they will ask.

**Before you start:** `make demo`, then open <http://localhost:3000> and sign in as
**Admin**. That role holds all seven capabilities; the others are deliberately narrower and
you will demonstrate that at the end.

Everything on screen is synthetic. Say so once, early, and let the ribbon at the top of
every page carry it from there.

---

## 1. The problem, in two sentences

> Today a network fault becomes a customer problem before anyone in the NOC knows it is a
> fault. The network's own data saw it first — a cell's availability dropped, an alarm
> fired — but nobody connected that to the forty people now calling the contact centre.

That gap is the whole product. Everything else follows from closing it.

---

## 2. Why it matters — three audiences, three reasons

Lead with the customer. The cost case is real but it is the third reason, not the first.

**For the customer.** Right now they find out their service is broken by trying to use it,
then wait on hold to be told something the operator already knew. TAZAMA inverts that: the
operator tells them first, with a specific message about their area and an honest statement
of what is being done. A customer who was told is a customer who does not call, and far
more importantly, is a customer who was treated as though their time mattered.

**For the network team.** An engineer's day is currently spent assembling context: which
cells, how many subscribers, is there maintenance, has this happened before, what did we do
last time. TAZAMA assembles it before they open the ticket — the incident card is that
context on one screen, with the evidence attached. The work shifts from *gathering* to
*deciding*, which is what you hired engineers for.

**For the business.** Avoidable contacts are the measurable line: every proactive
notification that lands before a call is a contact that did not happen, and the product
records exactly how many. Alongside it: faster mean time to repair because diagnosis starts
with evidence rather than a blank page, and SLA exposure visible per incident rather than
discovered at the end of the month.

> If you only remember one framing: **the network already knows. TAZAMA is the part that
> tells somebody.**

---

## 3. The walkthrough

### Command Center — 2 minutes

Open <http://localhost:3000>.

Expected: **17 live incidents, 2 suppressed, ~140,201 subscribers affected, 69 complaints
correlated.** Causes: congestion 13, cell outage 3, transmission link failure 1.

Three things to point at:

1. **"2 suppressed."** Two incidents were detected and then deliberately *not* raised,
   because planned maintenance covers them. A system that pages someone about scheduled
   work gets ignored within a week.
2. **"complaint → cell coverage 52.0%"** in the top right. This is the hardest technical
   problem in the product — matching a customer's complaint to the cell that serves them —
   and it is on screen on every page load rather than buried in a report. *Say the number
   out loud.* Half the complaints cannot be placed yet, and we measure it precisely so it
   can be improved rather than assumed away.
3. **The map.** No basemap, by design: an operator's NOC may have no internet route, and
   the geography of a fault is readable without one. Dot size is subscribers affected.

### The incident card — 4 minutes

Click **"Transmission link failure"** (`INC_K8Y583VK8Q1CRF`) — 35 complaints, 27 cells.

This is the screen that replaces the assembling work.

- **Impact first**: subscribers, cells, complaints, enterprise accounts, SLA risk.
- **Probable cause, with evidence rows.** Each row is a pointer to a database record — an
  alarm id, a topology node, a complaint count. Nothing here is an assertion without a
  trail.
- **KPI charts** with a dashed *incident start* marker. Ask them to check that the curve
  actually turns at the marker. That is them auditing the detection rather than trusting it.
- **The decision trail**: who or what decided, when, under which rule version.

### The AI — 4 minutes, and the most important part

Still on the incident card, press **Diagnose**.

You get a cause, a confidence score, the evidence it rests on — and then expand
**"What the agent did"**. That panel is the demo.

- **Tools called**: the agent read the incident, its alarms, the topology, the complaint
  counts, the runbook catalogue.
- **Tools refused**: `execute_runbook` is registered specifically so that an agent reaching
  for it is *recorded*. It is refused before any code runs.
- **Budget**: steps, tool calls and wall-clock, each capped. A loop that cannot terminate
  cannot take the pipeline with it.
- **Method**: `rules` or `llm`. If it says `rules`, the deterministic path was conclusive
  and **no model was consulted at all**.

Say this plainly:

> The model does not decide and cannot act. It proposes, with evidence we verify against
> the records it was actually shown. If it cites an alarm that does not exist, we throw the
> whole answer away.

Now the **Data Explorer** (`/explorer`). Type a question in plain English:
*"which regions had the most complaints last week"*.

It writes SQL. **It does not run it.** You read the query, see the guard's verdict beside
it, press **Use this query**, then press **Run**. Expected: Nairobi ~1,550, Mombasa ~915,
Nakuru ~888, Kisumu ~767.

> The model wrote that query. A person read it. The database only ever executes what a
> person approved, through a guard that allows SELECT and nothing else.

Then click **"Try to delete data"** and Run. A yellow panel: *refused by the SQL guard,
never sent to the database*. Yellow, not red — a refusal is a correct outcome.

### The safety demonstration — 2 minutes

This is the slide that wins the room, and it costs nothing to show.

Stop the model server (`Ctrl+C` in the `make fake-llm` window, or just note it) and reload
the incident card. Press **Diagnose** again.

**You get the same cause.** Method says `rules`, the header shows *AI degraded*, and the AI
Quality page explains why.

> The product does not depend on the model. If it is slow, down, or wrong, everything keeps
> working and the screen tells you which path produced the answer.

### Care, reports and roles — 3 minutes

**Care lookup** (`/care`, as a Care agent). Click a seeded subscriber token. The agent sees
whether there is a known problem *on that subscriber's cell* — and if the only live incident
is elsewhere in the region, the panel says so explicitly rather than letting them overclaim.
The wording they read to the customer is an approved template with a Copy button and **no
text box**. An agent reads an approved line; they do not compose one.

**Reports** (`/reports`). The Microsoft Teams digest, rendered as the card *and* as the
exact JSON that would be posted. `TEAMS_DRY_RUN = true`, "nothing has been posted", and no
send button — because there is no send endpoint. Mention that the card is scanned for phone
numbers and subscriber tokens before it is returned, and a hit blocks the report rather
than quietly redacting it.

**Roles.** Sign out, sign in as **Care agent**. The Data Explorer, Insights and Reports
disappear. Now type `localhost:3000/explorer` directly — refused by the server, not just
hidden. Hiding a link is not authorisation.

---

## 4. What is real and what is not

Be first to say this. It buys you every other claim in the room.

| Real | Not real |
| --- | --- |
| The architecture, the pipeline, the correlation engine, the guards | The data — every subscriber, site, cell, complaint and KPI is generated |
| Every number on screen, computed from that data by the real code | The KPI thresholds, which are modelled from public practice and labelled UNVERIFIED |
| The 1,605 automated tests and every gate result | The brand colours and wordmark, which are placeholders |
| The agents, their evals, their refusals | Teams delivery — built and validated, never sent, because no webhook exists yet |
| The ML gate: the forecast beats its baseline by 15.7% | The accuracy figures in production, which will be worse than these |

On that last row, say it unprompted:

> Every model here looks better than it will in production, because this data contains the
> relationships we put in it. The honest number is "it beats the baseline on our data", and
> the gate is built so that if it ever stops beating the baseline, it stops shipping.

---

## 5. The three questions you will be asked

**"Can it act on its own?"**

No, and not as a policy — as an absence. There is no adapter, no credential and no code
path from TAZAMA to a network element. Runbooks are recommended and executed by a human
elsewhere. The tool that would execute one exists only so that an agent asking for it gets
recorded.

**"What happens to customer data?"**

Phone numbers are converted to irreversible tokens at the moment they arrive, before
anything else sees them. The model never receives an identifier, and every outbound text —
to a model, a log, a Teams card, an export — is scanned. A Teams card containing a phone
number *blocks the report* rather than being quietly cleaned, because the point is to fix
wherever it came from. What does not exist yet: a deletion procedure. That is first on the
list before any real subscriber record goes near this.

**"How long to go live?"**

Switching to real data is configuration, not code: one mappings file and a set of
environment variables. The honest blocker is not technical — it is three operational
procedures that must exist first: **deleting a subscriber's data on request, rotating the
pseudonymisation key, and a floor below which a small cell is not reported individually**
(because a complaint rate on a twelve-subscriber cell at 3am is close to identifying).
Those are days of work, not months, and they are not optional.

---

## 6. If something looks wrong on the day

| Symptom | Most likely cause |
| --- | --- |
| Command Center shows 0 incidents | The detection pass has not run — `make pipeline` |
| Every AI panel says "AI degraded" | The model server is not running — `make fake-llm` |
| A query returns no rows | It asked for "the last 7 days" against data that ends in March; anchor the window on the data |
| A page shows an error panel | The API is not running — check <http://127.0.0.1:8000/healthz> |

---

## 7. The closing line

> What you have seen is the whole journey working on synthetic data: the network's own
> signals and the customer's complaint arriving at the same conclusion, with the evidence
> attached, a proposed action, and a human in front of every decision that reaches a
> customer. The remaining work is not building — it is plugging in the real tables and
> writing three privacy procedures. The architecture is done.
