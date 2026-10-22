# Runbook — outbox backlog

**Symptom:** `/readyz` reports the `outbox` probe as `degraded`, or `worker_dead_letters`
appears in the worker log, or the Command Center stops updating while the API is healthy.

**What it means:** events are being written but not dispatched. The pipeline has stalled
somewhere after the write, so incidents may be detected but not prioritised, routed or
notified. Nothing is lost — the events are rows — but the system is behind.

## 1. Measure it
```bash
make doctor                      # the outbox probe prints lag and pending count
# or directly:
curl -s localhost:8000/readyz | python -m json.tool
```
`lag` is the age of the oldest undispatched event in seconds; `pending` is how many.
Lag above 60s makes the probe `degraded`. Lag that keeps climbing means the worker is not
keeping up or is not running.

## 2. Is the worker running?
```bash
docker compose ps worker         # or: ps aux | grep "tazama.worker"
docker compose logs --tail=100 worker
```
* **Not running:** start it (`docker compose up -d worker`). A worker that cannot reach the
  database exits with code 1 and logs `worker_cannot_start` — fix the database first
  (`docs/runbooks/db-down.md`).
* **Running but idle while events are pending:** the events are scheduled in the future
  because they are being retried (see step 3).

## 3. Are events failing?
```sql
select status, count(*) from outbox_event group by status;
select event_type, attempts, last_error, available_at
from outbox_event where status in ('retry','dead_letter')
order by occurred_at desc fetch first 20 rows only;   -- LIMIT 20 on SQLite
```
* `retry` with a recent `last_error`: a consumer is failing. The error text names the
  exception. Backoff is 2s, 10s, 60s, 5m, 15m, so a `retry` row with `available_at` in the
  future is **normal** and not a stall.
* `dead_letter`: the event failed five times and was parked. Nothing will retry it until
  someone asks.

## 4. Fix the consumer, then requeue
Deploy the fix first — requeuing into a broken consumer just burns the attempts again.
```python
from tazama.infrastructure.eventbus import OutboxDispatcher
print(OutboxDispatcher().requeue_dead_letters(), "event(s) requeued")
```
Then watch one poll:
```bash
docker compose logs -f worker | grep outbox_poll
```

## 5. If the backlog is large
A big backlog drains at the poll batch size (100 events per tick, ~0.1s between busy
ticks). If that is too slow:
* raise the batch size (`OutboxDispatcher(batch_size=…)`) — the limit is database load;
* do **not** start a second worker. There is no claim mechanism yet, so two workers
  double-deliver. Consumers are idempotent, so this is survivable rather than corrupting,
  but it doubles the work. See `docs/tech-debt.md`.

## 6. Verify
* `/readyz` outbox probe back to `ok`.
* `select count(*) from outbox_event where status in ('pending','retry')` trending to zero.
* Spot-check one affected incident with `make replay TAZAMA_REPLAY_ID=INC_…` and confirm
  the decisions you expect are recorded.

## Prevention
* The `outbox-health` scheduled job logs lag every 15 minutes — alert on it.
* A consumer should raise only on a *transient* failure. A permanent one (bad payload,
  missing reference) should be recorded and swallowed, or it will dead-letter five times
  over and bury the real problem.
