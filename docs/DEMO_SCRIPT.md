# TAZAMA — Fast 5-Minute Pitch & Tab-by-Tab Demo Walkthrough

A practical, conversational demo guide for leadership.
Every tab lasts **30 seconds maximum**. You speak like a real engineer having a conversation, not reading a script.

---

## ⏱️ Pitch Timeline (5 Minutes Total)

| Time | Where to Click / Look | Focus |
|---|---|---|
| **0:00 – 0:45** | Slide / Talking directly | **The Problem:** The network knows, but the customer suffers in silence. |
| **0:45 – 1:15** | Tab 1: **Command Center** (`/`) | **The Big Picture:** 17 active incidents, 2 suppressed maintenance, live map. |
| **1:15 – 2:00** | Tab 2: **Incident Card** (`/incidents/INC_...`) | **Root Cause:** 27 cells down, physical fiber alarm, evidence trail. |
| **2:00 – 2:45** | **AI Diagnosis** (Inside Incident Card) | **Safe AI:** Rules first. AI agent suggests runbooks, but cannot touch the network. |
| **2:45 – 3:15** | Tab 3: **Complaints** (`/complaints`) | **Customer Voice:** Multilingual Sheng/Swahili complaints linked to towers. |
| **3:15 – 3:45** | Tab 4: **Care Lookup** (`/care`) | **Customer Care Desk:** 10-second lookup, approved script, zero guessing. |
| **3:45 – 4:15** | Tab 5: **Data Explorer** (`/explorer`) | **Guarded AI SQL:** Ask in plain English, human approves, safety guard refuses deletes. |
| **4:15 – 4:45** | Tab 6: **Insights** (`/insights`) | **Predictive Maintenance:** Lead-lag curves show network drops hours before calls spike. |
| **4:45 – 5:00** | Wrap-up | **The Ask / Roadmap:** Architecture is done; next step is connecting live Oracle data. |

---

## 🎙️ The Spoken Walkthrough (Tab by Tab)

### 0:00 – 0:45 | The Problem (Before Touching the Screen)
**What to say:**
> *"Good morning. Today across our 50 million subscribers, when a cell tower fails or a fiber cable gets cut, the network equipment knows immediately. Alarms start ringing.*
>
> *But who tells us the service is broken? The customer. They get a dropped call or a failed M-PESA transaction, and they dial \*100 or call customer care. Call centers get flooded with thousands of calls, care agents have no idea what's happening on that specific tower, and our NOC engineers are staring at 10,000 blinking red alarms trying to piece the story together.*
>
> *The fundamental problem is this: **The network already knows. But nobody connected the network's data to the customer calling in.**
>
> *Tazama is the real-time bridge that connects them."*

---

### 0:45 – 1:15 | Tab 1: Command Center (`/`)
👉 **Action:** Open `http://localhost:3000` (logged in as Admin).

- **What this tab tells us:**
  The nationwide operational health of the mobile network in one glance.
- **What the Network Team does with it:**
  Prioritizes which incident to tackle first based on affected subscribers, not just alarm noise.
- **What to say (~30s):**
  > *"Here is the live Command Center. Look at these three headline numbers:*
  >
  > 1. *We have **17 active incidents affecting ~140,000 subscribers**, and **69 customer complaints** already linked to specific cell towers.*
  > 2. *Right here: **'2 suppressed'**. These were real degradations, but Tazama automatically suppressed them because they match scheduled maintenance. Engineers don't get woken up for planned work.*
  > 3. *On the map: dot sizes show subscriber impact. The NOC instantly sees where the biggest customer pain is."*

---

### 1:15 – 2:00 | Tab 2: Incident Details Card
👉 **Action:** In the incidents table, click **"Transmission link failure"** (`INC_K8Y583VK8Q1CRF`).

- **What this tab tells us:**
  The complete forensic evidence of one incident: affected cells, customer impact, and the exact physical alarm.
- **What the Network Team does with it:**
  Fixes the fault immediately. Instead of spending 2 hours hunting through logs across 5 tools, all the evidence is right here.
- **What to say (~30s):**
  > *"Let's click into this major incident: Transmission link failure affecting 27 cells.*
  >
  > *In the past, an engineer spent hours opening six databases to see which sites were down and what caused it.*
  >
  > *Here, on one screen:
  > - Exact subscriber and enterprise SLA impact.
  > - The physical evidence: points directly to the upstream fiber switch alarm.
  > - Hourly KPI charts with a dashed line showing the exact minute the link broke.*
  >
  > *The engineer's job shifts from **gathering clues** to **fixing the problem**."*

---

### 2:00 – 2:45 | Inside Incident: AI Diagnosis & Guardrails
👉 **Action:** On the incident card, click the **Diagnose** button. When the card loads, click to expand **"What the agent did"**.

- **What this tells us:**
  How AI reached its diagnosis, what tools it used, and what it was forbidden to do.
- **What the Network Team does with it:**
  Gets an instant recommended runbook with confidence scores, knowing the AI cannot accidentally trigger a network outage.
- **What to say (~30s):**
  > *"Now watch what happens when I click **Diagnose**.*
  >
  > *First, Tazama checks **operational rules**. If the cause is obvious, like scheduled maintenance, it answers instantly without calling AI. Free, fast, and zero hallucinations.*
  >
  > *Second, when the AI agent does analyze ambiguous cases, look at this trace panel: **'What the agent did'**. It lists the tools called, and look at this line: **'Tools refused: execute_runbook'**.*
  >
  > *Our AI is strictly read-and-propose. It recommends actions, but has zero permissions to touch or disrupt a network element. A human engineer stays in control."*

---

### 2:45 – 3:15 | Tab 3: Complaints (`/complaints`)
👉 **Action:** Click **Complaints** in the top navigation bar.

- **What this tab tells us:**
  Incoming customer complaint feeds (USSD \*100, call center, mobile app) categorized by local language.
- **What the Network & Care Teams do with it:**
  Network sees real customer sentiment per region. Care sees which channels are spiking.
- **What to say (~30s):**
  > *"Here is the **Complaints** tab. Customer feedback comes from \*100 USSD, call centers, and SMS.*
  >
  > *Notice how complaints are categorized. Kenyan subscribers speak in English, Swahili, and Sheng: 'simu haina neti', 'network imedrop'.*
  >
  > *Instead of sending every message to an expensive overseas LLM, Tazama uses a lightweight local telco dictionary to classify over 70% of complaints instantly in under 2 milliseconds."*

---

### 3:15 – 3:45 | Tab 4: Care Lookup (`/care`)
👉 **Action:** Click **Care lookup** in the top navigation bar. Click one of the sample tokens below the input box.

- **What this tab tells us:**
  What a call center agent sees when a customer calls in with an issue.
- **What the Care Team does with it:**
  Resolves calls in 10 seconds with honest, accurate information instead of guessing.
- **What to say (~30s):**
  > *"Now imagine a customer calls customer care or dials \*100. The agent opens **Care lookup** and enters the customer's number.*
  >
  > *Notice: the phone number is immediately pseudonymized for privacy—no personal data is exposed.*
  >
  > *The care agent sees: 'Service degradation on cell NRB-042 due to transmission link work, estimated restore time 45 mins'.*
  >
  > *And right below it: an **approved script template with a Copy button and NO free-text compose box**. The agent reads an approved message; they don't guess or make false promises."*

---

### 3:45 – 4:15 | Tab 5: Data Explorer (`/explorer`)
👉 **Action:** Click **Data Explorer**. Click the example button *"Complaints per region"*, then click the button *"Try to delete data"*.

- **What this tab tells us:**
  Conversational analytics where anyone can ask questions in plain English, with strict security guardrails.
- **What Leadership & Analysts do with it:**
  Get instant answers to ad-hoc business questions without waiting days for a BI ticket.
- **What to say (~30s):**
  > *"On the **Data Explorer**, anyone can type a question in plain English: 'Which regions had the most complaints last week?'.*
  >
  > *Tazama's AI writes the SQL query, but **does not run it automatically**. A human reviews the SQL and clicks Run.*
  >
  > *And look at our security test: I click **'Try to delete data'**. Look at this yellow panel: **Refused by the SQL guard**. The guard enforces SELECT-only queries. Even if an attacker or buggy model writes a DELETE statement, it is physically blocked before reaching the database."*

---

### 4:15 – 4:45 | Tab 6: Insights (`/insights`) & Predictive Maintenance
👉 **Action:** Click **Insights** in the top navigation bar. Point at the **Lead-Lag** chart and the **Complaint Rate** curve.

- **What this tab tells us:**
  Predictive analytics connecting network performance to future customer behavior.
- **What the Network Team does with it:**
  Fixes towers before calls spike. This is the foundation for **Predictive Maintenance**.
- **What to say (~30s):**
  > *"Here is **Insights**, and this is where the biggest future value lives: **Predictive Maintenance for Cell Towers**.*
  >
  > *Look at this Lead-Lag curve. It proves that network KPIs—like packet drops and radio frequency degradation—start degrading **2 to 6 hours before** customers begin calling in.*
  >
  > *Today, we wait for towers to break. With Tazama, we detect the gradual decline in radio traffic and battery backup telemetry early. We dispatch field technicians to service the tower or generator before the outage ever happens.*
  >
  > *This shifts Safaricom from Reactive Repair to **Predictive Reliability**."*

---

### 4:45 – 5:00 | Wrap-Up & Closing Pitch
👉 **Action:** Click back to **Command Center** (`/`).

**What to say:**
> *"To summarize: TAZAMA protects customer trust, saves millions in avoidable call center operations, and cuts network troubleshooting time from hours to minutes.*
>
> *Everything you saw today is fully built, backed by 1,600 automated tests, running live on seed data. To go live, we only need to map our production Oracle tables. The architecture is done. Thank you, and I'd love to take your questions."*

---

## 🧩 The Architecture of the Problem (Explain Like I'm Five)

If leadership asks you to explain the system design or why we chose these specific approaches, keep it this simple:

| Approach / Choice | The Problem in Plain Words | How Tazama Solves It |
|---|---|---|
| **Graph Topology Grouping** | When one fiber cable breaks, 20 cell towers go down. If each tower raises an alarm, the NOC gets 20 alarms and creates 20 tickets. | Tazama knows the physical network topology. It collapses all 20 towers into **1 single incident** pointing directly at the broken cable. |
| **Rules First, AI Second** | Calling an LLM for every alarm is slow, expensive, and can hallucinate. | Obvious problems (scheduled maintenance, fiber cuts) are diagnosed instantly by deterministic rules (100% free, 1ms). AI is only invoked for the 15% complex, ambiguous cases. |
| **Median Anomaly Detection** | Mobile traffic is cyclical. Friday at 6 PM has heavy traffic; 3 AM has light traffic. A static threshold doesn't work. | Tazama compares each tower to **itself at that exact hour of the week** using medians, so a past storm doesn't skew normal traffic baselines. |
| **Poisson Surge Forecasting** | Standard AI models lazily predict "zero calls" because most hours are quiet, which leaves call centers understaffed when a disaster happens. | Tazama uses a Poisson model built specifically for count spikes, so call centers know hours in advance when to staff up. |
| **Read-and-Propose Agent** | Autonomous AI agents that execute network commands can bring down an entire country's telecom network. | Tazama's agent has a strict permissions ceiling. It can read telemetry and suggest a runbook, but is physically blocked from executing network commands. |

---

## 🔮 Expanding the Future Outlook: Predictive Maintenance for Cell Towers

When leadership asks: *"How does this expand into cell tower predictive maintenance?"*

Explain it across three concrete scenarios:

1. **Power & Generator Degradation:**
   - *What happens today:* Mains power cuts out; battery backups drain; the tower abruptly dies without warning.
   - *With Tazama:* Tazama tracks voltage drop curves and diesel fuel sensor telemetry. If voltage drops faster than historical discharge rates, it flags a *Battery Health Warning* 3 hours before the tower goes dark.
2. **Backhaul & Optical Transceiver Degradation:**
   - *What happens today:* Optical fiber transceivers degrade over weeks due to dust, heat, or micro-bends until the link suddenly fails.
   - *With Tazama:* Tazama monitors Bit Error Rates (BER) and Frame Check Sequence (FCS) errors. When errors rise above the cell's weekly baseline, it flags transceiver wear before packet loss hits customers.
3. **Radio Frequency (RF) & Antenna Misalignment:**
   - *What happens today:* Heavy winds or structural shifts tilt an antenna, shrinking the coverage footprint and overloading adjacent towers.
   - *With Tazama:* Tazama monitors Handover Success Rate and Signal-to-Interference-plus-Noise Ratio (SINR). A steady drop in handover success on one sector identifies physical tilt without needing a physical site survey first.

---

## 🛡️ Quick-Fire Leadership Q&A

**Q: "Can the AI make changes to the network?"**
> *"No. The AI has zero credentials and no network adapter. It can only propose runbooks. Execution is physically refused and logged."*

**Q: "Is customer data safe under Kenyan data privacy laws?"**
> *"Yes. Phone numbers are hashed using HMAC encryption at the boundary before analytics or AI see them. Outbound reports are continuously scanned for PII."*

**Q: "How long until we can plug this into real Oracle data?"**
> *"The code, APIs, and UI are completely built. Connecting to production Oracle tables is a configuration mapping (`source_mappings.yaml`). The only work left is operational: testing the privacy deletion procedure and salt rotation policy."*
