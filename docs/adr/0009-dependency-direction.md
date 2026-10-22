# ADR 0009 — Dependency direction: infrastructure depends on modules

Status: accepted · Date: 2026-10-21 · Phase: 1 (correction) · Supersedes part of ADR 0001

## Context
The original import-linter layering listed `modules` above `infrastructure`, which reads
naturally ("business logic sits on top of plumbing") but is the wrong way round for
ports-and-adapters. An adapter has to name the port it satisfies and the DTOs it returns,
so `infrastructure.db.discovery_repo` importing `modules.discovery.models` broke the
contract — while being exactly the dependency the architecture wants.

## Decision
Dependencies point **inward**, and the layer order is:

```
api | worker          ← composition root, wires everything
infrastructure        ← adapters: may import the ports and DTOs they satisfy
ai
modules               ← business logic: may NOT import infrastructure
core
domain
```

The rule that actually matters is unchanged and still enforced separately: **a module must
never import an adapter.** That contract (`modules do not reach into infrastructure or the
api layer`) is what keeps modules testable with fakes; the layer order only decides which
side is allowed to know the other exists.

## Consequences
* Adapters are typed against the ports they implement, so a signature drift is a type
  error rather than a runtime surprise.
* A module still cannot reach a database: five import-linter contracts hold the line, and
  `make lint` fails on a violation.
* Where an adapter would otherwise need a module's constant (the explorer table allowlist,
  for instance), the constant stays in infrastructure and the module keeps a copy that a
  test asserts is identical — a module must not import infrastructure even for a constant.

## Note
Caught by a subagent's verification run rather than by review, which is the argument for
keeping `lint-imports` in the gate: an architectural rule that is not executable is a
preference.
