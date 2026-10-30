# Runbook — the database is unavailable

**Severity: high.** Unlike the model, the database is not optional: every read path and the
outbox depend on it. The product's job here is to fail loudly and lose nothing.

---

## Symptoms

- `/readyz` fails; `/healthz` may still answer (it deliberately does not touch the DB).
- The console shows error panels with a correlation id on every page.
- The worker logs a startup refusal, or `dispatch` reports repeated failures.
- `make doctor` fails at the DB check.

## What is guaranteed while it is down

- **Nothing is silently dropped.** Complaint intake is a write; with no database the API
  returns 503 problem+json and the channel's own retry owns the message. TAZAMA never
  acknowledges an intake it did not persist.
- **No duplicate side effects on recovery.** Domain events were committed in the same
  transaction as their state change (the outbox pattern), dispatch is idempotent, and every
  handler tolerates redelivery. The scenario suite asserts that draining twice changes
  nothing.
- **The worker refuses to start** rather than spinning against a dead database
  (`test_worker_refuses_to_start_without_a_database`). A worker that appears to be running
  while achieving nothing is worse than one that is plainly down.

## Diagnosis

```bash
make doctor                  # environment, config, DB, mappings
echo $DB_BACKEND             # sqlite | oracle
```

**SQLite** (`DB_BACKEND=sqlite`, the demo default):

```bash
ls -l data/tazama.db         # present? non-zero?
```

A missing file usually means `SQLITE_PATH` is being resolved from a different working
directory. `core/paths.py` anchors relative paths to the repo root precisely because this
bit us twice during the build — but an absolute path in `.env` overrides that.

**Oracle** (`DB_BACKEND=oracle`):

```bash
docker compose ps oracle
docker compose logs --tail=50 oracle
```

Oracle Free takes a few minutes on first start. "Unavailable" immediately after
`make up` is usually "not ready yet"; the compose healthcheck is the thing to watch, not
the port.

## Actions

| Cause | Action |
| --- | --- |
| Container not started | `make up` |
| Container still initialising | Wait for the healthcheck; do not restart it in a loop |
| Wrong DSN or credentials | Fix the env; startup fails fast and lists every bad variable at once |
| Connection pool exhausted | Check for a long-running Explorer query; the guard caps rows and sets a timeout, but a pathological plan can still hold a connection |
| Disk full (SQLite) | Free space; SQLite fails writes before reads, so reads may still work and mask it |
| Schema drift after a deploy | `make migrate` — every revision has a tested down migration |

## After recovery

```bash
make doctor                  # confirm clean
make migrate                 # no-op if already at head
```

Then check the outbox has drained rather than assuming it:

```bash
TAZAMA_REPLAY_ID=<incident> make replay    # replay is deterministic
```

If events accumulated while the database was down, they did not — there was nowhere to
write them. The backlog to watch for is the opposite case: the database recovered while the
worker was still stopped. Follow `outbox-backlog.md`.

## What not to do

- **Do not delete and re-seed to "fix" it** outside a demo. `make seed --reset` destroys
  the audit trail, which is the one thing in this system that cannot be reconstructed.
- **Do not set `ALLOW_LIVE_WRITES=true`** to work around a permissions error. The read-only
  source access is deliberate; a permissions failure on a source schema means the mapping
  is pointing at the wrong thing.
- **Do not bypass Alembic** by hand-patching the schema. The down migration is what makes a
  bad deploy recoverable.

## Escalate if

- The database is up but the app reports schema drift that `make migrate` does not resolve
  — that means a migration was applied out of band.
- Any source-schema write is attempted or succeeds in `APP_MODE=live`. That is a
  containment failure, not a database problem.
