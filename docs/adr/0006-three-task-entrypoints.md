# ADR 0006 — One task runner, three entry points

Status: accepted · Date: 2026-10-21 · Phase: 0

## Context
The PRD specifies `make` targets. GNU make is not installed on the Windows build machine
(and often is not on developer laptops), while CI is Linux. Duplicating recipes across a
Makefile and a PowerShell script guarantees drift.

## Decision
`scripts/mk.py` is the single implementation of every target (pure standard library).
`Makefile` and `make.ps1` are thin forwarders. A test asserts that the Makefile's `.PHONY`
list, its recipes and the runner's task registry are the same set, and CI runs both
`make help` and `python scripts/mk.py help`.

## Consequences
`make <target>`, `./make.ps1 <target>` and `python scripts/mk.py <target>` are
interchangeable everywhere in the docs. Adding a target in one place without the others
fails the gate.
