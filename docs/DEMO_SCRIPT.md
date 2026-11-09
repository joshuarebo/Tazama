# TAZAMA — Executive Demo & Pitch Script

A punchy 5-minute executive pitch script for senior leadership, followed by the complete screen-by-screen conversational guide, first-principles algorithmic breakdown, and honest answers to leadership questions.

---

## ⚡ 5-Minute Pitch Script (Word-for-Word Track)

*Have <http://localhost:3000> open on your screen signed in as **Admin** before you start.*

### [0:00 - 1:00] The First-Principles Problem & Why It Matters
> *"Good morning leadership. Today, across our network of over 50 million subscribers, when a cell tower degrades or a fiber link drops, our network equipment sees it within milliseconds. Alarms fire in the NOC.*
>
> *Yet, who tells us the network is broken? Our customers. They experience a dropped call or a failed M-PESA transaction, and they dial into customer care or *100 USSD. The contact center spikes with thousands of calls, agents are blind to local tower status, and engineers in the NOC are drowning in 10,000 blinking red alarms trying to piece together what happened.*
>
> *The fundamental gap is this: **The network already knows. But nobody connected the network's data to the customer calling in.**
>
> *TAZAMA closes that gap. We turn raw telemetry, alarms, and customer complaints into real-time incident intelligence, automated root-cause diagnosis, and proactive care before the customer even has to call."*

---

### [1:00 - 2:00] Screen 1: Command Center (`http://localhost:3000`)
*(Point at the screen)*
> *"Here is the live Command Center. Three things to notice immediately:*
>
> 1. *Across our country footprint, we have **17 active incidents affecting ~140,000 subscribers**, with **69 complaints** already correlated to specific cell towers.*
> 2. *Look at this metric in the corner: **'2 suppressed'**. These were real degradations, but Tazama suppressed them because they match scheduled maintenance. We stop alert fatigue before it starts.*
> 3. *And right here: **'complaint → cell coverage: 52%'**. We don't hide the truth. In telco, connecting a customer complaint to their serving cell is the hardest technical problem. We measure it live on every screen so we can improve it rather than pretend it doesn't exist.*
>
> *Notice the map: dot sizes represent actual subscribers affected, giving the NOC an instant geographic heat map of impact."*

---

### [2:00 - 3:00] Screen 2: Incident Deep-Dive (`/incidents/INC_K8Y583VK8Q1CRF`)
*(Click into the Transmission Link Failure incident)*
> *"Let's drill into this major incident: Transmission link failure covering 27 cells and 35 complaints.*
>
> *In the past, an engineer spent two hours opening six different databases to find out which sites were down, which enterprise accounts had SLAs at risk, and what caused it.*
>
> *Here, on one single card, Tazama assembles:*
> - *Exact subscriber and SLA impact.*
> - *Every piece of physical evidence: the specific aggregation node alarm.*
> - *KPI trendlines with a dashed vertical line marking the exact incident start.*
>
> *The engineer's job shifts from **gathering evidence** to **making decisions**."*

---

### [3:00 - 4:00] Screen 3: Safe AI Diagnosis & The Guardrails
*(Press the **Diagnose** button, then expand 'What the agent did')*
> *"Now, where does AI come in? I press **Diagnose**.*
>
> *Notice two critical design decisions:*
> 1. *Tazama checks **deterministic operational rules first**. If the rules are certain, we don't even call the AI. That makes it instant, free, and immune to hallucinations.*
> 2. *When the AI Agent does analyze the incident, expand this panel: **'What the agent did'**. You can see the exact tools called, and look here: **'Tools refused: execute_runbook'**.*
>
> *Our AI is strictly read-and-propose. It recommends actions with citations to real database alarms; it has zero ability to touch or disrupt a network element. A human engineer stays in control."*

---

### [4:00 - 4:45] Screen 4: Proactive Customer Care (`/care`) & Safe Data Explorer (`/explorer`)
*(Click **Care** in the top navigation)*
> *"What does this mean for customer care? A customer dials *100 or calls an agent.*
>
> *The care agent looks up the pseudonymized subscriber. Instead of guessing, they see immediately: 'Service degradation on cell NRB-042 due to transmission link maintenance, estimated recovery 45 mins'.*
>
> *Beside it is an **approved script template with a Copy button and NO free-text compose box**. The agent delivers an honest, accurate message in 10 seconds.*
>
> *And on our **Data Explorer**, leadership or analysts can ask questions in plain English: 'Which regions had the most complaints last week?'. Tazama's AI writes the SQL, verifies it through a SELECT-only security guard, and lets a human review it before running. If someone attempts an unsafe query, the SQL guard immediately refuses it."*

---

### [4:45 - 5:00] The Closing & Next Steps
> *"In summary: TAZAMA protects customer trust, saves millions in avoidable call center costs, and shortens mean-time-to-repair from hours to minutes.*
>
> *What you've seen is fully built, tested across 1,600 automated tests, running with seed data on our real architecture. The only step remaining to go live is mapping our Oracle production tables and finalizing our data protection erasure routines. Thank you, and I welcome your questions."*

---

## 🧠 The First-Principles Thinking: Explain Like I'm Five

If senior leadership asks why we designed it this way, here is the plain, mundane translation of every architectural and algorithmic choice:

| Algorithm / Capability | Mundane Analogy | Why This Matters (First Principles) |
|---|---|---|
| **Spatio-Temporal Correlation** (Graph & Topology) | *10 burglaries happen on one block within 10 minutes. You don't open 10 separate detective files; you open one case because they share the same street.* | When 20 cell towers drop simultaneously because an upstream fiber cable was cut, Tazama groups them into **1 incident** pointing at the cable. No duplicate tickets. |
| **Rules-First, AI-Second** (Diagnostic Engine) | *If you see smoke pouring out of a window and the fire alarm is ringing, you don't call an AI to diagnose why the room is hot.* | Telecommunication failures (fiber cuts, scheduled maintenance) usually have unambiguous signatures. Rules are instant, 100% free, and 100% explainable. AI is only reserved for ambiguous edge cases. |
| **Robust Anomaly Detection** (Median Absolute Deviation) | *Traffic on Mombasa Road on Friday at 6 PM is bumper-to-bumper. That's normal. Traffic on Tuesday at 3 AM is an anomaly.* | Global thresholds fail because cell tower traffic is cyclical (day vs night, weekday vs weekend). Tazama compares each tower to **itself at that exact hour of the week**, using medians so one past storm doesn't skew normal baselines. |
| **LightGBM Poisson Forecasting** (Call Surge Prediction) | *A hospital needs to predict ambulance arrivals. Predicting 'zero' all day has a low error on paper, but leaves you helpless when an emergency hits.* | Telco complaints are non-negative integer spikes (0, 3, 25 calls). A Poisson model specifically optimizes for predicting the spikes so call centers can staff up proactively. |
| **Multilingual Lexicon** (Swahili/Sheng Routing) | *A local shopkeeper who immediately understands Sheng ("simu imedrop", "haina neti") without needing a dictionary.* | Kenyan subscribers mix English, Swahili, and Sheng. A lightweight local lexicon instantly classifies 70%+ of complaints in under 2ms without sending customer text to an external LLM. |
| **Sandboxed Agent with Refusals** | *An intern who can research and propose recommendations, but doesn't have the keys to the master server room.* | The AI agent can read telemetry and suggest an operational runbook, but cannot execute commands on network switches. The tool to execute is explicitly registered to be **refused and logged**. |

---

## 🗺️ What Each UI Page Represents & Where It Connects

| Page Route | Purpose in Demo | Underlying Data & Backend Connection |
|---|---|---|
| **Command Center** (`/`) | High-level operational pulse for NOC directors & VPs. | Aggregates active incidents from `/api/v1/incidents`, joined with cell coordinates and complaint correlation counts (`/api/v1/discovery`). |
| **Incident Card** (`/incidents/[id]`) | Detailed engineering investigation screen. | Connects to `/api/v1/incidents/{id}`, pulling physical alarms (`IC_STAGE.SRC_ALARMS`), upstream topology links, hourly KPI curves, and audit logs. |
| **AI Diagnosis** (Modal/Panel) | Demonstrates safe GenAI reasoning with inspectable traces. | Calls `POST /api/v1/ai/diagnose/{id}`. Runs through rule gates, invokes `DiagnosisAgent` if needed, and returns step-by-step tool traces & refusals. |
| **Complaints** (`/complaints`) | Voice of the customer connected to network cells. | Connects to `/api/v1/complaints`. Shows incoming complaint feeds (USSD *100, call center, mobile app) categorized by Sheng/Swahili NLP lexicon. |
| **Care Lookup** (`/care`) | Proactive customer care desk interface. | Connects to `/api/v1/care/lookup`. Takes a hashed subscriber token, checks active cell outages, and outputs a canned script for the care agent. |
| **Data Explorer** (`/explorer`) | Self-service conversational analytics. | Connects to `POST /api/v1/ai/sql` (text-to-SQL) and `POST /api/v1/discovery/query` (guarded execution). Proposes SQL, verifies SELECT-only rules, human clicks Run. |
| **Insights** (`/insights`) | Predictive maintenance & traffic analytics. | Connects to `/api/v1/insights`. Shows de-seasonalized cell traffic, KPI lead-lag relationships, candidate failure drivers, and regional surge forecasts. |
| **Reports** (`/reports`) | Daily automated executive briefing. | Connects to `/api/v1/reports/digest`. Shows the Microsoft Teams Adaptive Card preview with automated PII redaction and dry-run safety flags. |
| **AI Status & Evals** (`/ai`) | Operational transparency & AI observability. | Connects to `GET /api/v1/ai/status`. Shows model health, degraded-mode readiness, budget spent, and accuracy against ground truth. |

---

## 🔮 The Opportunity: Predictive Maintenance for Cell Towers

When leadership asks: *"Where can this go next?"*

> *"The immediate next frontier is **Predictive Maintenance**.*
>
> *Today, towers fail abruptly due to thermal throttling, battery backup degradation during power cuts, optical transceiver degradation on fiber uplinks, or gradual antenna misalignment caused by heavy winds.*
>
> *Because Tazama ingests continuous radio and link KPIs (such as Setup Success Rate, PRB Utilization, RSRP, and Bit Error Rates), we don't just react to outages. We can detect the **gradual degradation curve 4 to 12 hours before a hard failure**.*
>
> *For example: if a cell's PRB utilization is spiking abnormally while Handover Success Rate is dropping by 2% per hour, Tazama can flag a **'Pre-Failure Anomaly'**, alerting field engineers to inspect the antenna tilt or dispatch generator fuel before subscribers ever experience service interruption.*
>
> *This transitions Safaricom from Reactive Repair to **Predictive Reliability**."*

---

## 🛡️ Leadership Q&A: The Three Toughest Questions

### 1. "Can this AI accidentally trigger a network outage or make unauthorized changes?"
**Your answer:**
> *"No, and that is architectural, not just policy. There is literally no network adapter, no credential, and no code path from Tazama into any network element. The agent can only read telemetry and propose recommendations. When you inspected the trace, you saw that tools like `execute_runbook` are deliberately programmed to refuse execution and log the refusal. A human engineer must always take the action."*

### 2. "How are we handling customer privacy and Kenya Data Protection Act compliance?"
**Your answer:**
> *"Phone numbers and subscriber IDs are HMAC-pseudonymized at the very edge of ingestion using a cryptographically secure salt before any analytics, AI models, or UI screens touch the data. No raw MSISDN ever enters our database or prompt contexts. Outbound Teams digests and exports are continuously scanned, and any detected PII immediately blocks transmission rather than quietly leaking."*

### 3. "How long until we can plug this into production data?"
**Your answer:**
> *"The engineering architecture, database models, API services, and user interfaces are completely built and green across 1,600 automated tests. Moving to live data is purely configuration: editing `config/source_mappings.yaml` to point to our production Oracle tables. The only prerequisites are operational: implementing the data deletion cascade, rotating the pseudonymization key, and establishing a cell-level reporting privacy floor. We are looking at weeks, not months."*
