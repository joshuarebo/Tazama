# Runbook — a source mapping is wrong

**Severity: high, and usually silent.** This is the failure mode most likely to produce a
plausible-looking console that is quietly wrong, which makes it more dangerous than an
outage.

---

## Why this has its own runbook

`config/source_mappings.yaml` is the only place in the system that knows a real table or
column name. That is deliberate — switching demo → live is a config edit, never a code
change — and it means a wrong mapping does not crash anything. It produces an answer.

A serving-cell column pointed at the wrong field will resolve complaints to the wrong
cells, correlate them into the wrong incidents, and put a confident diagnosis with evidence
on a card. Nothing raises.

## Symptoms

Ranked by how early you will see them.

| Symptom | Where | What it suggests |
| --- | --- | --- |
| Startup fails listing offending entries | logs | A missing table or column. **The good case** — caught at the boundary |
| Join coverage collapses against its baseline | `make catalog`, Command Center header | The complaint → cell source is wrong |
| "Unlocated" complaints jump | `/complaints?unresolved_only=true` | Same |
| Incidents detected with implausible footprints | Command Center | Topology mapping is wrong |
| Subscriber counts obviously wrong | incident cards | `cell.subscribers_est` is pointing at the wrong column |
| KPI values out of range | Insights, incident card charts | A unit mismatch — a percentage mapped to a ratio column, or vice versa |

## Diagnosis

Start with coverage, because it is one number and it moves first:

```bash
make catalog
```

Compare `complaint_to_cell_pct` against the recorded baseline in
`docs/data-catalog.md`. **The day-one baseline is 51.99%.** A drop of more than a few points
after a mapping change means the mapping, not the data.

Then check what the mappings resolved to:

```bash
curl -sS localhost:8000/api/v1/discovery/mappings | python -m json.tool
curl -sS localhost:8000/api/v1/discovery/catalog/<table> | python -m json.tool
```

The catalog endpoint reports row counts, null rates and value ranges per column. A column
that is 100% null, or whose range is 0–1 where a percentage is expected, is the answer.

For KPI units specifically, compare against `docs/data-catalog.md`, which records the
modelled ranges — and remember those are **UNVERIFIED** (tech-debt #10): they are modelled
from public RAN practice, not from the operator's counters. A mismatch may mean the mapping
is wrong *or* that the documented expectation is.

## Actions

1. **Fix the mapping in `config/source_mappings.yaml`.** Only the `live` block.
2. **Restart.** Mappings are validated at startup and the error lists every offending entry
   at once rather than one per restart.
3. **Re-run `make catalog`** and confirm coverage recovered.
4. **Decide what to do about the data produced while it was wrong.** This is the part that
   needs a judgement call, not a command:
   - Incidents detected from wrong joins are wrong. They do not self-correct, because
     correlation is idempotent on the *incident identity*, not on the inputs.
   - Notifications already approved and sent cannot be recalled. Check
     `/api/v1/notifications` for anything sent during the window.
   - The audit trail is intact and records which mapping version produced each decision.

## What not to do

- **Do not "fix" low coverage by loosening site identification confidence.** A complaint
  located at low confidence is flagged for a human precisely so that a mapping problem
  surfaces as a queue rather than as a wrong answer. Loosening the threshold converts a
  visible problem into an invisible one.
- **Do not edit mappings to make a specific query work.** The mappings serve every consumer.
- **Do not change the demo block to match live.** They exist separately so the demo stays
  reproducible.

## Prevention

- Startup validation already catches missing tables and columns.
- `make catalog` prints coverage on every run; read it after any mapping change.
- CI asserts the settings model and `.env.example` agree, which catches a renamed variable
  but not a wrong value — the catalog is the check for that.

## Escalate if

- Coverage recovered but incident footprints still look wrong — suspect the topology
  mapping, which affects the shared-upstream join rather than the complaint join and does
  not show up in complaint coverage at all.
- Any notification was sent during the window. That is a customer-facing consequence and
  the care team needs to know before a customer calls about it.
