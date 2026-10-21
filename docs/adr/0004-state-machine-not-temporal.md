# ADR 0004 — An explicit persisted state machine, not a workflow engine

Status: accepted · Date: 2026-10-21 · Phase: 0 (implemented in Phase 2)

## Context
The complaint/incident lifecycle has approvals, reopen paths and long waits (verification
quiet periods). Temporal or similar would manage that, at the cost of another service,
another deployment story and a second place where state lives.

## Decision
Model the lifecycle as a single declared state machine in `domain/state_machine.py`:
states, allowed transitions, the guard for each transition, and the event each emits.
State is persisted on the aggregate; the worker drives time-based transitions.

Illegal transitions raise rather than silently writing a status, and the transition table
is tested exhaustively (every state × every event).

## Consequences
Long waits are implemented as scheduled checks instead of durable timers — adequate for
quiet periods measured in minutes. Temporal stays deferred; if human-in-the-loop waits
grow to days with escalation ladders, revisit this ADR rather than bolting timers on.
