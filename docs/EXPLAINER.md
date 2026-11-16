# TAZAMA, explained

Everything you need to present this platform, in the order you would say it.
Written to be read once and then spoken from memory.

---

## Part 0. Running it

### The one command

From the repository root:

```
make demo
```

On Windows, where GNU `make` usually is not installed, these are the same thing:

```
./make.ps1 demo
python scripts/mk.py demo
```

That does the whole clean-machine path: creates `.env` with freshly generated local
secrets, installs backend and frontend dependencies, applies migrations, seeds the
synthetic data, runs detection so there are actual incidents to look at, builds the data
catalog, then starts four processes and leaves them running.

When it settles you will see:

| | |
|---|---|
| **Console** | <http://localhost:3000> |
| **API docs** | <http://localhost:8000/docs> |
| **Model server** | <http://127.0.0.1:8088> |

Open the console, pick a role on the sign-in screen, and start at the Command Center.
**Admin** holds every capability, so use that unless you are specifically demonstrating
that roles see different things.

`Ctrl+C` stops everything.

### What you need installed first

- **Python 3.12 or 3.13**
- **Node 20 or newer**
- **Docker**, only for the Oracle integration tests. The demo runs on SQLite without it.

If something does not start:

```
make doctor
```

That checks the environment, the config, the database, the model endpoint and the vector
backend, and prints the fix rather than a stack trace.

### The model server, and why it matters for a demo

`make demo` starts a small deterministic model server on port 8088 and points the app at
it. It is local, it is offline, and nothing leaves the machine.

This matters because without it every AI panel shows **AI degraded** and falls back to the
rules. That fallback is real and worth demonstrating, but you want to choose when to show
it rather than discover it live.

**To demonstrate the degraded path on purpose:** stop the model server, reload the console,
and show that every screen still works and says which path answered. That is one of the
strongest moments in the walkthrough. Start it again with `make fake-llm`.

### Running pieces separately

Useful when you are iterating rather than demoing.

| Command | What it starts |
|---|---|
| `make api` | API on :8000 with reload |
| `make web` | Console on :3000 |
| `make worker` | Event worker and scheduler |
| `make fake-llm` | Deterministic model server on :8088 |
| `make seed` | Re-seed the synthetic data |
| `make pipeline` | Re-run detection over the seeded data |

### Resetting

```
make clean
make demo
```

The seed uses a fixed random seed, so the same data comes back every time and the numbers
in this document stay correct.

### The offline version

`docs/demo/tazama-demo.html` is a single file that replicates the console with the same
data. No install, no server, works offline. That is the one to email to somebody who
should see the product without a laptop setup.

---

## Part 1. The problem, in plain language

### The one sentence

> The network already knows when something breaks. TAZAMA is the part that tells somebody.

### The two minute version

A cell site in Nairobi starts dropping calls at 10am. Two things happen immediately, in two
different systems owned by two different teams.

The **network** notices. An alarm fires. The hourly performance figures for that cell fall
off a cliff. That data lands in the performance management system, where it sits.

The **customers** notice. They try to use their phone, it does not work, and over the next
hour forty of them send a message through the app, dial into the call centre, or post
something. That lands in CRM, where it sits.

Nothing joins those two facts together. So three things go wrong at once:

1. **The customer finds out by failing.** They discover their service is broken by trying to
   use it, then wait on hold to be told something the operator already knew an hour ago.
2. **The engineer starts from nothing.** They open five systems to answer basic questions.
   Which cells? How many subscribers? Is there maintenance? Has this happened before? That is
   the first hour of every incident, spent gathering rather than fixing.
3. **The business pays twice.** Once for the fault, and again for thousands of contact centre
   calls that only happened because nobody was told.

### What TAZAMA does about it

It joins the two halves. Network signals on one side, customer signals on the other. When
they line up it raises **one** incident, works out exactly who is affected, explains the
likely cause with the evidence attached, and drafts a message for a human to approve.

### Why it is not just a dashboard

Three things make it different from a monitoring screen:

- **It works without complaints.** A degradation in the performance data raises an incident
  whether or not anybody called. The complaints are confirmation, not the trigger.
- **It collapses noise.** One broken fibre link that takes 27 cells offline becomes one
  incident pointing at the link, not 27 alarms.
- **It closes the loop to the customer.** Detection is only half of it. The other half is
  telling the person whose service is broken, before they call.

---

## Part 2. How we approach the problem

Say this if anyone asks "is this an AI product?". The honest answer is that there are three
different kinds of intelligence in it and they do different jobs.

### Layer 1, rules and statistics, which carries the weight

Grouping faults, ranking them, and spotting unusual cell behaviour are all deterministic.
They are explainable, instant, free to run, and they cannot be talked into a different answer
by the wording of a complaint.

On our labelled test cases the rules alone score **1.00 precision and 1.00 recall** on
detection and get the cause right **7 times out of 7**.

### Layer 2, machine learning, where a pattern beats a rule

Forecasting how many complaints a region will get in the next hour so care can staff for it.
Spotting a cell behaving unlike its own history. These are places where no rule could be
written by hand.

### Layer 3, AI agents, narrow and bounded

Three agents. One explains an ambiguous incident. One answers an operational question from
the written procedures, or declines. One drafts the daily report. Each works through a fixed
list of tools, each has a hard budget, and every answer is checked against a schema before
anyone sees it.

### The rule that makes it safe

> **AI recommends. Rules decide. Humans approve.**

No language model runs a repair procedure, writes a network command, sends a customer a
message, or sees a raw phone number. Models fill slots in approved templates. They never
write prose to a customer.

And if the model is switched off entirely, the product does not break. It falls back to the
rules, which score 7 out of 7. **The system degrades from AI to rules, not from AI to
broken.**

---

## Part 3. The walkthrough, tab by tab

For each screen: what it shows, what it tells you, what the network team does with it.

---

### Tab 1. Command Center

**What it shows.** Every live incident, ranked by how many customers it is hurting. Five
headline numbers across the top, a map of affected cells, the breakdown by cause and region,
and the incident list.

**What it tells you.** The state of the network as the customer is experiencing it, right
now. Not how many alarms are open, which is a network-centric number, but how many customers
are affected, which is the number that matters.

**What the network team does with it.** Decides what to work on first. Today that decision
is made from alarm counts, which do not tell you whether an alarm is hurting ten people or
seventy thousand.

**The number to point at.** 17 live incidents, roughly 140,000 subscribers affected, 1,053
complaints in 24 hours of which 69 are tied to a specific incident.

**The honest line to say out loud.** In the top right there is a figure reading "complaint to
cell coverage 52%". That is the share of complaints we can tie to a specific cell. We measure
it on every run and show it rather than hiding it, because it is the real ceiling on how much
of the customer signal we can use. Improving that number on real data is worth more than any
new model.

---

### Tab 2. The incident card

**What it shows.** One incident in full. The shared piece of equipment, the cells beneath it,
subscribers and enterprise accounts affected, the alarm, whether there is maintenance in the
window, and the diagnosis with its evidence.

**What it tells you.** Everything an engineer currently spends an hour assembling from five
systems, on one screen, before they open the ticket.

**What the network team does with it.** Goes straight to fixing. The work shifts from
gathering context to making a decision, which is what you hired engineers to do.

**The moment worth demonstrating.** Open the transmission link failure. It is titled like a
fault, but the diagnosis says **planned maintenance, confidence 0.95**, with the maintenance
record attached. The system checked the change management window before raising an alarm, and
nobody gets woken up at 2am for approved work.

Say this: *a system that cannot tell planned work from a fault generates false alarms until
people stop trusting it. That is how monitoring tools die.*

**Then press the AI diagnosis button.** It shows the agent's trace: six tools called, all of
them read-only, the time each took, and the budget it spent. Point at the tool list and note
that `execute_runbook` is registered at tier **never**. It exists so that an attempt to act is
refused loudly and recorded, rather than failing quietly somewhere nobody looks.

---

### Tab 3. Complaints

**What it shows.** Customer messages arriving from the app, SMS, USSD, the call centre and
social, each with a language and an assigned category.

**What it tells you.** What customers are actually saying, in the words they actually use,
including Swahili, Sheng and all of it mixed together in one sentence.

**What the network team does with it.** Sees customer sentiment per region and per cell as a
signal alongside the KPIs, rather than as a monthly report after the fact.

**The row to point at.** *"Mtandao ni mbaya sana leo"* means "the network is very bad today".
It plainly names the network but describes no symptom, so it stays **unclassified** and still
counts towards the incident. That phrasing is a huge share of real traffic. Treating it as
non-network would throw away exactly the signal we exist to catch.

**The honest line.** Classification is 69% exact and 76% on the network-versus-not decision,
with 30% routed to a human. Those are modest numbers and we state them, because the 30%
review rate is a feature: a guess that knows it is a guess goes to a person.

---

### Tab 4. Care lookup

**What it shows.** What a contact centre agent sees while the customer is on the line. Who
they are, which cell is serving them, whether there is a known problem in their area, and the
approved words to say.

**What it tells you.** Whether the person calling is affected by something we already know
about.

**What the care team does with it.** Resolves the call in ten seconds with accurate
information, instead of guessing or escalating.

**Two things to point at.**

The customer is identified by a token, not a phone number. The number is replaced at the
point of ingestion, so this screen, the logs, the reports and the AI all see the token and
never the number. It is not a setting someone can switch off downstream.

There is **no free text box**. The agent reads an approved template. A model may fill slots in
it and may never write prose to a customer. An invented restoration time, repeated across a
contact centre, is a broken promise at scale.

---

### Tab 5. Data Explorer

**What it shows.** A SQL window, plus a box where you can ask a question in plain English.

**What it tells you.** Anything you want to ask of the data, without raising a BI ticket and
waiting three days.

**What the team does with it.** Answers its own ad hoc questions. "Which regions had the most
complaints last week" is a thirty second question, not a request.

**The flow to demonstrate.** Ask the question. The model writes SQL. **The SQL is shown, and
nothing runs.** A person reads it, approves it, and only then does the existing guard execute
it.

Say this: *the dangerous failure here is not someone deleting a table. The schema and the
guard stop that, and we test it against a completely compromised model that returns DROP
TABLE. The dangerous failure is a query that is valid, permitted, and subtly wrong. A join
that double counts. A window off by an hour. Showing the SQL makes that reviewable. Showing
only the answer makes it invisible, and a wrong number presented to leadership is the real
risk.*

**Then press "Try to delete data".** It is refused by the guard, which parses the statement
into a syntax tree rather than matching strings, allows only reads, and audits the attempt
against the role that made it.

---

### Tab 6. Insights

**What it shows.** Where network behaviour and customer behaviour are compared. The
complaint forecast, the anomaly detector, and a lead-lag analysis asking whether KPIs move
before complaints do.

**What it tells you.** Whether there is a predictive relationship worth acting on.

**What the network team does with it.** Staffs care for a predicted surge, and investigates
cells behaving unlike themselves.

**This is the most important tab to be honest about.** On our demo data the lead-lag analysis
finds **nothing**. The strongest correlation anywhere in the scan is 0.11, which is very weak,
and the tool reports that the KPI does not lead. It then publishes its own three reasons for
distrusting even that number.

Do not skip this slide. Say: *this is synthetic data, and the tool correctly refuses to claim
a relationship that is not there. A dashboard that turned 0.11 into "the network warns us
three hours ahead" would be telling you a story the data does not support, and somebody would
eventually check. On real data this becomes a genuine measurement.*

That single behaviour will earn you more credibility with a network team than any number on
any other screen.

---

### Tab 7. Reports

**What it shows.** The daily and weekly digest, exactly as it would be posted to a Teams
channel.

**What it tells you.** What the shift and the management line need to know without opening the
console.

**What the team does with it.** Reads it in the channel they already live in.

**The line to say.** Nothing is actually sent. Outbound delivery is off by default, the
webhook address is never written to a log, and no card ever carries a phone number. Turning
delivery on is a deliberate per-environment change, not a default.

---

### Tab 8. AI quality

**What it shows.** Whether a model is reachable, how many calls and tokens have been spent
against the ceiling, the three agents with their permission ceilings, and every tool with its
tier.

**What it tells you.** What the AI is costing and what it is allowed to touch.

**What the team does with it.** Audits it. This is the screen you show a security reviewer.

**The line to say.** Nine tools can read. Two can propose, which writes a recommendation for a
human and has no outward effect. One can never be called at all. There is no tier that
touches a network element. The ceiling is enforced by the harness that runs the tools, not by
the instructions we give the model, so it cannot be talked out of it.

---

## Part 4. The questions you will be asked

**"Can the AI break the network?"**
No. It has no credentials and no adapter that could reach a network element. The only tool
that could act is registered so that calling it is refused and recorded. Execution does not
exist as a code path, which is stronger than it being disabled.

**"Is customer data safe?"**
Phone numbers are replaced with a one-way token at the point of data entry, before anything
else sees them. Nothing leaves the network. Everything runs against an internal model, not a
public one.

**"How accurate is it?"**
On labelled test cases the rules score 1.00 precision and 1.00 recall on detection and 7 of 7
on diagnosis. Be careful with that number in the room: it describes the mechanism working on
generated data. It does not predict performance on real traffic, and anyone who has worked
with real network data will know that.

**"How long until it runs on real data?"**
Connecting the data is a configuration change rather than new development. Real table names
live in one file, validated at startup. The genuine blockers are three privacy procedures
that must exist before any real subscriber record enters the system: deletion that cascades,
a policy for rotating the key that hides phone numbers, and a minimum group size below which
a single cell is not reported on its own.

**"What happens when the AI is unavailable?"**
Every path has a deterministic fallback and the fallbacks are good. Diagnosis reverts to
rules scoring 7 of 7, the digest to a rendered template, the assistant to the retrieved
document with its citation. Every screen records which path answered.

**"What is next?"**
Predictive maintenance. Today we detect a fault early. Predicting one needs telemetry we do
not yet collect. The first step is the site power feed: track each battery's discharge curve
against its own history and flag it hours before the site goes dark. One data source, and the
method we already use for anomaly detection transfers to it directly.

---

## Part 5. The closing

> Today a fault becomes a customer problem before the operations centre knows it is a fault.
> TAZAMA closes that gap. It protects customer trust, it removes avoidable calls from the
> contact centre, and it turns the first hour of an incident from gathering into fixing.
>
> Everything you have seen is built and tested. What is left is pointing it at our own data.

---

## Appendix. If you are asked about algorithms

Short answers, in the order they usually come up.

| They ask about | Say this |
|---|---|
| **Grouping faults** | Rules over place, time and symptom, joined through the network topology. Chosen over a learned clustering model because an engineer has to defend the grouping at 3am, and "they share an upstream node and the alarm is on that node" is a sentence pointing at a row. A distance score is not. |
| **Diagnosis** | Rules first. If the rules are confident the model is never called at all. That is cheaper, faster, and it makes the model's contribution measurable, because every decision records which path answered it. |
| **Priority** | A transparent weighted rule that stores what each factor contributed. A P1 has to be defensible months later in an argument. "Subscribers contributed 0.4, SLA risk 0.3" is a conversation. A trained ranker's score is not. |
| **Anomaly detection** | Each cell compared against its own history at that hour of that weekday, using the median rather than the average. A fixed threshold cannot tell a cell that is always busy at 8pm from one that has just broken. |
| **Forecasting** | Gradient boosting with a Poisson objective, at region level, for care staffing. Worth telling the story: our first version used a different objective, predicted a constant zero, and still beat the baseline by 40%, because the target is 97.6% zeros. We caught it because every feature attribution printed as zero. The gate now requires three things at once, and that is why. |
| **Complaint text** | A multilingual dictionary covering English, Swahili, Sheng and code switching, not a language model. Inspectable, instant, free, and its accuracy is known rather than assumed. |
| **Why not deep learning** | The data is tabular and modest in volume. Gradient boosting is simply stronger on that shape, trains in seconds, and every prediction can be attributed to specific features. A neural network would need far more data to match it and would give up the explanation. |
| **GNN or reinforcement learning** | Both are scaffolded and deliberately switched off. The rules already score 1.00, so a graph model has nothing to beat and would trade an explainable grouping for an embedding. Reinforcement learning on a live network is not something we will do. |
