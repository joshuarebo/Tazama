# Runbook: rolling back a deployment

**Covers standard 5.10.** Read this before you need it.

The hard part of a rollback is not the application. It is the database.

---

## Decide first: is this a rollback or a fix-forward?

| Symptom | Action |
|---|---|
| New version throws on most requests, old version was fine | **Roll back.** |
| New version is slow but correct | Fix forward. A rollback during a load spike makes it worse. |
| A migration applied and the new code depends on it | **Do not roll back blindly.** See below. |
| Config or a feature flag is wrong | Change the ConfigMap. No rollback needed. |
| Only one component is broken | Roll back that Deployment only. |

A rollback is cheap when it is only code. It is expensive when the schema has moved.

---

## The application rollback

The pipeline has a manual `rollback:production` job. It reverts all three Deployments to
their previous ReplicaSet.

Manually, if the pipeline is unavailable:

```sh
oc project "$OPENSHIFT_PROJECT_PROD"

# What you are rolling back from and to.
oc rollout history deployment/tazama-api

oc rollout undo deployment/tazama-api
oc rollout undo deployment/tazama-worker
oc rollout undo deployment/tazama-web

oc rollout status deployment/tazama-api --timeout=600s
```

`maxUnavailable: 0` means the old pods stay up until the rolled-back ones are ready, so
there is no gap in service.

Then prove it:

```sh
python scripts/smoke_check.py https://tazama.apps.internal
```

That checks more than a probe does: that a protected route still refuses anonymous
callers, that the security headers are present, and that metrics are being produced.

---

## The database, which is the actual risk

**Migrations are not rolled back automatically, and that is deliberate.** The pipeline runs
`alembic upgrade head` as a Job before any rollout. The rollback job does **not** run
`alembic downgrade`.

Why: an automatic down-migration during an incident is how a rollback becomes data loss.
A down-migration that drops a column discards everything written to it since the deploy,
which is exactly the data from the period you are trying to recover.

Every revision has a down-migration and they are tested, so a downgrade is *possible*. It
is a deliberate decision, not a default.

### Work out whether you need one

```sh
oc exec deploy/tazama-api -- alembic current
oc exec deploy/tazama-api -- alembic history --verbose | head -40
```

Then:

- **Additive migration** (new nullable column, new table, new index): the old code ignores
  it. **Do nothing.** Roll back the application and leave the schema ahead. This is the
  normal case, and migrations are written to be additive for exactly this reason.
- **Destructive or incompatible** (dropped or renamed column, narrowed constraint): the
  old code will fail against the new schema. Decide between:
  1. **Fix forward.** Usually correct. Ship a patch rather than unwinding the schema.
  2. **Restore from backup** to a point before the migration, accepting the data loss
     between that point and now. Requires the data owner's agreement, in the moment.
  3. **Downgrade one revision**, only if you have read its docstring and confirmed what it
     discards:
     ```sh
     oc exec deploy/tazama-api -- alembic downgrade -1
     ```

Any destructive migration carries a reviewed note in its revision docstring saying what it
drops. Read that note before running the downgrade, not after.

---

## The outbox

The worker is a single replica with a `Recreate` strategy, so there is never more than one
scheduler. During a rollback it will be briefly absent. That is safe:

- Events stay in the outbox table until dispatched. Nothing is lost.
- Consumers are idempotent, so anything half-processed is safe to replay.
- The scheduler compares against stored local times, so a missed window is picked up on
  the next tick rather than skipped.

Check the backlog has drained afterwards:

```sh
oc logs deploy/tazama-worker --tail=50 | grep -E "worker_dead_letters|outbox"
curl -s https://tazama.apps.internal/api/v1/readyz | python -m json.tool
```

Readiness includes an outbox-lag check, so a backlog that is not draining shows up there.

---

## Afterwards

1. **Write the postmortem** before the detail fades. What shipped, what broke, what the
   first signal was, and how long it took to notice. If the first signal was a person
   rather than an alert, that gap is the finding.
2. **Note whether the smoke check caught it.** If the deploy was green and broken, the
   check is missing an assertion. Add it.
3. **Keep the rolled-back image.** `revisionHistoryLimit: 5`, so you have the previous
   five. Do not retag over them while investigating.

---

## What makes a rollback unnecessary

Worth saying, because the best rollback is the one not needed:

- **Additive migrations only**, so the schema can always run one version ahead.
- **Feature flags default off**, so new behaviour is enabled separately from being
  deployed. A flag flip is instant and needs no rollout.
- **Staging first**, with the same manifests and the same smoke check.
- **Immutable image tags.** The deployment pins a commit SHA, never `latest`, so "the
  previous version" is unambiguous.
