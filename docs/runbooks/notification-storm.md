# Runbook — a notification storm

**Severity: high.** This is the failure with the largest customer-facing consequence in the
product: messages cannot be recalled.

---

## Why a storm is unlikely by construction

Worth knowing before you start, because it tells you where to look. Five independent
controls stand between a detected incident and a customer's handset:

1. **Human approval.** No notification is sent without an explicit approval action by a
   named principal. There is no auto-send path and no configuration that creates one.
2. **Per-incident dedupe.** One proposal per incident per template.
3. **Quiet hours** from `config/policies.yaml`.
4. **Rate limits** per incident and per subscriber.
5. **Opt-out**, honoured by the notification service and surfaced to the care agent.

Plus: `SMS_GATEWAY_MODE=mock` is the only supported value in v1, so in the current build
**nothing is actually delivered to a handset** — the mock records what would have been
sent, and the pilot measures calls avoided from those records.

So a "storm" in the current build means one of: a flood of *proposals* awaiting approval, a
bulk approval, or a policy that is wrong.

## Symptoms

- "Notifications waiting" climbs on the Command Center.
- `/api/v1/notifications` returns many `proposed` rows for one incident or one cell.
- Audit shows many `approve` actions in a short window by one actor.
- Care reports customers receiving repeated messages.

## Immediate actions

**Stop the source, not the symptom.**

1. **Identify what is proposing.** Proposals come from the notification service driven by
   incident detection. Many proposals for *one* incident is a dedupe failure; many
   proposals across *many* incidents is over-detection — go to correlation, not
   notification.

   ```bash
   curl -sS "localhost:8000/api/v1/notifications?status=proposed&limit=200" | python -m json.tool
   ```

2. **Do not approve anything further** until the cause is known. Proposals are harmless;
   approvals are not.

3. **If messages are genuinely going out** (a future build with a live gateway):
   set `SMS_GATEWAY_MODE=mock` and restart. That is the kill switch, and it is a config
   change rather than a code deploy on purpose.

4. **Reject the bad proposals** rather than deleting them. A rejection is audited with a
   reason; a deletion loses why it happened.

## Diagnosis

| Finding | Cause | Fix |
| --- | --- | --- |
| Many proposals, one incident | Dedupe key is not stable — likely an incident being re-detected under new ids | Check incident identity bucketing; see tech-debt #19 on `window_end` reconstruction |
| Many incidents from one fault | Correlation under-clustering, or an alarm storm not collapsed | `config/policies.yaml` correlation thresholds; tech-debt #16 |
| Proposals during quiet hours | `quiet_hours` wrong, or `DISPLAY_TZ` wrong | Quiet hours are local-time; check `DISPLAY_TZ` before the policy |
| Opted-out subscribers included | Audience query not honouring `opt_out` | A bug, not a policy. Escalate |
| One actor approved hundreds | A human mistake, or an approval UI that makes bulk approval too easy | Talk to the actor; the audit row names them |

## After it is contained

1. **Count what actually reached customers.** In the current build, query the mock
   gateway's records — that is the authoritative list of what *would* have been sent.
2. **Tell the care team before customers tell them.** An agent who knows a duplicate batch
   went out can handle the call; one who does not cannot.
3. **Fix the policy, then re-run the scenario suite.** The notification-storm suppression
   case lives in `tests/unit/test_notification.py`; if the storm was possible, that suite
   has a gap and the gap should be closed with a test that names this incident.

## What not to do

- **Do not raise the rate limits to clear a backlog of proposals.** The backlog is the
  control working.
- **Do not disable `templates_only` or `respect_opt_out`.** The policy engine refuses to
  load with either disabled — it fails closed — and the right response to wanting them off
  is to find out why.
- **Do not send a corrective message without approval.** An apology is still a customer
  notification and goes through the same gate.

## Escalate immediately if

- An opted-out subscriber was included. That is a compliance matter under the Data
  Protection Act, not an operational one.
- Any message content was not from an approved template. The LLM fills template slots and
  cannot write free text to customers (`NotificationFill` rejects prose in a slot), so
  free text reaching a customer would mean that control was bypassed.
