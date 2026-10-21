"""Deterministic replay (PRD section 13).

Replay answers one question an operator will eventually ask: *why did the system decide
that?* Given an incident (or a time range), it re-reads the recorded events and walks them
through the pipeline again with a **fixed clock**, so the run is reproducible, and it can
compare the result with what was stored.

Replay never writes to the app schema. It is a read-and-explain tool, not a repair tool:
re-processing in place would overwrite the evidence you are trying to examine.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from tazama.core.clock import FixedClock, SequenceClock
from tazama.core.logging import get_logger
from tazama.domain.events import DomainEvent, EventType
from tazama.infrastructure.db.engine import get_engine, session_factory
from tazama.infrastructure.db.models import AuditLog, Incident, IncidentLink, OutboxEvent

_log = get_logger(__name__)

Observer = Callable[[DomainEvent], None]

MAX_EVENTS = 5_000


@dataclass(frozen=True, slots=True)
class ReplayedEvent:
    sequence: int
    event_type: str
    aggregate_id: str
    occurred_at: datetime
    payload_keys: tuple[str, ...]
    correlation_id: str | None


@dataclass(slots=True)
class ReplayResult:
    target: str
    events: list[ReplayedEvent] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    ok: bool = True

    def render(self) -> str:
        lines = [f"replay of {self.target}: {len(self.events)} event(s)"]
        lines += [
            f"  {e.sequence:>3}. {e.occurred_at.isoformat()}  {e.event_type:<24} "
            f"{e.aggregate_id}  ({', '.join(e.payload_keys) or 'no payload'})"
            for e in self.events
        ]
        if self.decisions:
            lines += ["", "recorded decisions:"] + [f"  {d}" for d in self.decisions]
        if self.differences:
            lines += ["", "differences from the stored outcome:"] + [
                f"  {d}" for d in self.differences
            ]
        else:
            lines += ["", "no differences from the stored outcome"]
        return "\n".join(lines)


def _events_for_incident(engine: Engine, incident_id: str) -> list[OutboxEvent]:
    """Every event touching this incident, in the order it happened.

    Both the incident's own events and the events of the complaints linked to it, because
    the interesting part of a replay is usually what the complaints did.
    """
    with session_factory(engine)() as session:
        linked = (
            session.execute(
                select(IncidentLink.entity_id).where(IncidentLink.incident_id == incident_id),
            )
            .scalars()
            .all()
        )
        aggregate_ids = {incident_id, *linked}
        rows = (
            session.execute(
                select(OutboxEvent)
                .where(OutboxEvent.aggregate_id.in_(aggregate_ids))
                .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
                .limit(MAX_EVENTS),
            )
            .scalars()
            .all()
        )
    return list(rows)


def _events_since(engine: Engine, since: datetime | None) -> list[OutboxEvent]:
    with session_factory(engine)() as session:
        stmt = (
            select(OutboxEvent).order_by(OutboxEvent.occurred_at, OutboxEvent.id).limit(MAX_EVENTS)
        )
        if since is not None:
            stmt = stmt.where(OutboxEvent.occurred_at >= since)
        return list(session.execute(stmt).scalars().all())


def _to_event(row: OutboxEvent) -> DomainEvent:
    return DomainEvent.from_record(
        {
            "event_type": row.event_type,
            "event_version": row.event_version,
            "aggregate_type": row.aggregate_type,
            "aggregate_id": row.aggregate_id,
            "payload": row.payload,
            "occurred_at": row.occurred_at,
            "dedupe_key": row.dedupe_key or row.id,
            "correlation_id": row.correlation_id,
        },
    )


def replay_events(
    rows: Sequence[OutboxEvent],
    *,
    target: str,
    observers: Sequence[Observer] = (),
) -> ReplayResult:
    """Walk recorded events through the observers with a deterministic clock.

    The clock is a `SequenceClock` over the recorded instants, so anything that asks for
    "now" during the replay sees the time the step originally ran — the single most common
    reason a replay would otherwise diverge.
    """
    result = ReplayResult(target=target)
    if not rows:
        result.ok = False
        result.differences.append("no recorded events found for this target")
        return result

    clock = SequenceClock([row.occurred_at for row in rows])
    _log.info("replay_start", extra={"target": target, "events": len(rows)})

    for index, row in enumerate(rows, start=1):
        event = _to_event(row)
        result.events.append(
            ReplayedEvent(
                sequence=index,
                event_type=row.event_type,
                aggregate_id=row.aggregate_id,
                occurred_at=row.occurred_at,
                payload_keys=tuple(sorted((row.payload or {}).keys())),
                correlation_id=row.correlation_id,
            ),
        )
        for observe in observers:
            try:
                observe(event)
            except Exception as exc:
                result.ok = False
                result.differences.append(
                    f"observer failed on event {index} ({row.event_type}): "
                    f"{type(exc).__name__}: {exc}",
                )
        clock.now()  # advance in step with the events, even with no observers
    return result


def replay_incident(
    incident_id: str,
    *,
    engine: Engine | None = None,
    diff: bool = False,
    observers: Sequence[Observer] = (),
) -> ReplayResult:
    """Replay one incident and, with `diff`, compare against what was stored."""
    engine = engine or get_engine()
    rows = _events_for_incident(engine, incident_id)
    result = replay_events(rows, target=f"incident {incident_id}", observers=observers)

    with session_factory(engine)() as session:
        incident = session.get(Incident, incident_id)
        decisions = (
            session.execute(
                select(AuditLog)
                .where(AuditLog.entity_id == incident_id)
                .order_by(AuditLog.occurred_at),
            )
            .scalars()
            .all()
        )
    result.decisions = [
        f"{row.occurred_at.isoformat()}  {row.action:<16} -> {row.outcome}"
        f"  (actor {row.actor}, rules {row.rule_version or '-'}, "
        f"confidence {row.confidence if row.confidence is not None else '-'})"
        for row in decisions
    ]

    if incident is None:
        result.ok = False
        result.differences.append(f"incident {incident_id} no longer exists")
    elif diff:
        detected = [e for e in result.events if e.event_type == EventType.INCIDENT_DETECTED]
        if not detected:
            result.differences.append(
                "no IncidentDetected event recorded, yet the incident exists: "
                "it was created outside the pipeline",
            )
        if incident.status not in ("closed", "resolved", "suppressed") and not result.decisions:
            result.differences.append("open incident with no recorded decisions")
    return result


def replay_since(
    since: str | datetime | None,
    *,
    engine: Engine | None = None,
    diff: bool = False,  # noqa: ARG001 - accepted for CLI symmetry
    observers: Sequence[Observer] = (),
) -> ReplayResult:
    """Replay the whole event stream after an instant."""
    engine = engine or get_engine()
    moment: datetime | None
    if isinstance(since, str) and since:
        moment = datetime.fromisoformat(since)
        if moment.tzinfo is None:
            raise ValueError("--since must include a timezone offset, e.g. 2026-03-17T06:00+00:00")
    else:
        moment = since if isinstance(since, datetime) else None
    rows = _events_since(engine, moment)
    return replay_events(
        rows,
        target=f"events since {moment.isoformat() if moment else 'the beginning'}",
        observers=observers,
    )


def fixed_clock_for(rows: Sequence[OutboxEvent]) -> FixedClock:
    """A clock pinned to the first recorded instant, for code that needs a single `now`."""
    if not rows:
        raise ValueError("cannot build a clock from an empty event list")
    return FixedClock(rows[0].occurred_at)
