"""The worker process: wiring, bounded ticks, graceful failure, and replay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tazama.core.policy import load_policies
from tazama.domain.events import (
    AggregateType,
    ComplaintReceived,
    DomainEvent,
    EventType,
    IncidentDetected,
)
from tazama.infrastructure.db import session_factory
from tazama.infrastructure.db.models import AuditLog, Incident, IncidentLink
from tazama.infrastructure.eventbus import SessionOutboxWriter
from tazama.worker import replay as replay_module
from tazama.worker.consumers import AUDITED_EVENTS, build_consumers
from tazama.worker.scheduler import build_jobs

T0 = datetime(2026, 3, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def policies(repo_root):
    return load_policies(repo_root / "config" / "policies.yaml")


def _publish(db_engine, clock, event: DomainEvent) -> None:
    with session_factory(db_engine)() as session, session.begin():
        SessionOutboxWriter(session, clock).publish(event)


def _incident_detected(incident_id: str = "INC-1", **payload: object) -> IncidentDetected:
    return IncidentDetected(
        aggregate_type=AggregateType.INCIDENT,
        aggregate_id=incident_id,
        occurred_at=T0,
        dedupe_key=f"{incident_id}:detected",
        correlation_id="CID-REPLAY",
        payload={"region": "Nairobi", "priority": "P1", "status": "detected", **payload},
    )


# ------------------------------------------------------------------ wiring


def test_consumers_cover_every_audited_event(settings, policies, frozen_clock, db_engine) -> None:
    consumers = build_consumers(
        settings=settings,
        policies=policies,
        clock=frozen_clock,
        engine=db_engine,
    )
    assert set(AUDITED_EVENTS) <= set(consumers)
    for handlers in consumers.values():
        assert handlers


def test_the_worker_does_not_reach_into_the_api_layer() -> None:
    """The worker is a separate process, so pushing into the API's in-process SSE stream
    could never have worked in production — and the import direction was wrong. The API
    tails the outbox instead (`api/stream_feed.py`)."""
    source = (
        Path(__file__).resolve().parents[1].parent / "src" / "tazama" / "worker" / "consumers.py"
    ).read_text(encoding="utf-8")
    assert "tazama.api" not in source


def test_milestone_events_are_audited(
    settings, policies, frozen_clock, db_engine, db_session
) -> None:
    consumers = build_consumers(
        settings=settings,
        policies=policies,
        clock=frozen_clock,
        engine=db_engine,
    )
    for handler in consumers[EventType.INCIDENT_DETECTED]:
        handler(_incident_detected())

    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.action == "event:IncidentDetected"
    assert row.entity_id == "INC-1"
    assert row.outcome == "observed"
    assert row.inputs["region"] == "Nairobi"


def test_non_milestone_events_are_not_audited(
    settings, policies, frozen_clock, db_engine, db_session
) -> None:
    consumers = build_consumers(
        settings=settings,
        policies=policies,
        clock=frozen_clock,
        engine=db_engine,
    )
    handlers = consumers.get(EventType.CASE_PRIORITIZED, ())
    for handler in handlers:
        handler(
            ComplaintReceived(
                aggregate_type=AggregateType.CASE,
                aggregate_id="CASE-1",
                occurred_at=T0,
                dedupe_key="dk",
            ),
        )
    assert db_session.execute(select(AuditLog)).all() == []


def test_scheduled_jobs_are_declared(settings) -> None:
    jobs = build_jobs(settings=settings)
    assert [job.name for job in jobs] == ["outbox-health"]
    assert jobs[0].schedule.every_minutes == 15
    assert jobs[0].description


# ------------------------------------------------------------- worker loop


def test_worker_runs_bounded_and_exits_cleanly(settings, db_engine) -> None:
    from tazama.worker.__main__ import run

    assert run(settings, max_ticks=2) == 0


def test_worker_dispatches_what_it_finds(settings, db_engine, frozen_clock, db_session) -> None:
    from tazama.worker.__main__ import run

    _publish(db_engine, frozen_clock, _incident_detected())
    assert run(settings, max_ticks=1) == 0
    # The audit consumer ran, which proves the event was delivered.
    assert db_session.execute(select(AuditLog)).scalar_one().entity_id == "INC-1"


def test_worker_refuses_to_start_without_a_database(settings, monkeypatch) -> None:
    """A worker that cannot reach the database must fail loudly, not spin."""
    from tazama.worker import __main__ as worker_main

    monkeypatch.setattr(worker_main, "check_connection", lambda _s: (False, "connection refused"))
    assert worker_main.run(settings, max_ticks=1) == 1


# ----------------------------------------------------------------- replay


def test_replay_of_an_unknown_incident_is_honest(db_engine) -> None:
    result = replay_module.replay_incident("INC-NOPE", engine=db_engine)
    assert result.ok is False
    assert "no recorded events" in result.differences[0]


def test_replay_lists_events_in_order(db_engine, frozen_clock, db_session) -> None:
    db_session.add(
        Incident(
            id="INC-1",
            title="Westlands outage",
            status="detected",
            priority="P1",
            region="Nairobi",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.add(
        IncidentLink(
            incident_id="INC-1",
            entity_type="complaint",
            entity_id="CMP-1",
            weight=1.0,
            linked_at=T0,
        ),
    )
    db_session.commit()

    _publish(db_engine, frozen_clock, _incident_detected())
    _publish(
        db_engine,
        frozen_clock,
        ComplaintReceived(
            aggregate_type=AggregateType.COMPLAINT,
            aggregate_id="CMP-1",
            occurred_at=T0 - timedelta(minutes=5),
            dedupe_key="dk-1",
        ),
    )

    result = replay_module.replay_incident("INC-1", engine=db_engine)
    assert [e.event_type for e in result.events] == ["ComplaintReceived", "IncidentDetected"]
    assert result.events[0].sequence == 1
    rendered = result.render()
    assert "replay of incident INC-1" in rendered
    assert "no differences from the stored outcome" in rendered


def test_replay_is_deterministic(db_engine, frozen_clock, db_session) -> None:
    db_session.add(
        Incident(
            id="INC-2",
            title="Link down",
            status="detected",
            priority="P2",
            region="Kisumu",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.commit()
    _publish(db_engine, frozen_clock, _incident_detected("INC-2"))

    first = replay_module.replay_incident("INC-2", engine=db_engine).render()
    second = replay_module.replay_incident("INC-2", engine=db_engine).render()
    assert first == second


def test_replay_feeds_observers_with_recorded_events(db_engine, frozen_clock, db_session) -> None:
    db_session.add(
        Incident(
            id="INC-3",
            title="Congestion",
            status="detected",
            priority="P3",
            region="Mombasa",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.commit()
    _publish(db_engine, frozen_clock, _incident_detected("INC-3"))

    seen: list[DomainEvent] = []
    result = replay_module.replay_incident("INC-3", engine=db_engine, observers=[seen.append])
    assert result.ok is True
    assert [e.aggregate_id for e in seen] == ["INC-3"]
    assert seen[0].correlation_id == "CID-REPLAY"


def test_replay_reports_an_observer_failure_instead_of_crashing(
    db_engine, frozen_clock, db_session
) -> None:
    db_session.add(
        Incident(
            id="INC-4",
            title="Flapping",
            status="detected",
            priority="P3",
            region="Nakuru",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.commit()
    _publish(db_engine, frozen_clock, _incident_detected("INC-4"))

    def explode(_event: DomainEvent) -> None:
        raise RuntimeError("observer bug")

    result = replay_module.replay_incident("INC-4", engine=db_engine, observers=[explode])
    assert result.ok is False
    assert "observer bug" in result.differences[0]


def test_replay_shows_recorded_decisions(db_engine, frozen_clock, db_session) -> None:
    from tazama.infrastructure.db.audit_repo import SqlAuditSink
    from tazama.modules.audit.service import AuditService

    db_session.add(
        Incident(
            id="INC-5",
            title="Outage",
            status="detected",
            priority="P1",
            region="Eldoret",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.commit()
    _publish(db_engine, frozen_clock, _incident_detected("INC-5"))
    AuditService(sink=SqlAuditSink(db_engine), clock=frozen_clock).record(
        action="correlate",
        entity_type="incident",
        entity_id="INC-5",
        outcome="created",
        inputs={"complaints": 9},
        confidence=0.9,
        rule_version="policies-v1",
    )

    result = replay_module.replay_incident("INC-5", engine=db_engine, diff=True)
    assert any("correlate" in line and "created" in line for line in result.decisions)
    assert "policies-v1" in "\n".join(result.decisions)


def test_diff_flags_an_open_incident_with_no_decisions(db_engine, frozen_clock, db_session) -> None:
    db_session.add(
        Incident(
            id="INC-6",
            title="Mystery",
            status="detected",
            priority="P2",
            region="Nairobi",
            detected_at=T0,
            created_at=T0,
            updated_at=T0,
        ),
    )
    db_session.commit()
    _publish(db_engine, frozen_clock, _incident_detected("INC-6"))

    result = replay_module.replay_incident("INC-6", engine=db_engine, diff=True)
    assert any("no recorded decisions" in d for d in result.differences)


def test_replay_since_requires_a_timezone(db_engine) -> None:
    with pytest.raises(ValueError, match="timezone offset"):
        replay_module.replay_since("2026-03-17T06:00", engine=db_engine)


def test_replay_since_walks_the_whole_stream(db_engine, frozen_clock) -> None:
    _publish(db_engine, frozen_clock, _incident_detected("INC-7"))
    _publish(db_engine, frozen_clock, _incident_detected("INC-8"))
    result = replay_module.replay_since(None, engine=db_engine)
    assert len(result.events) == 2
    assert "events since the beginning" in result.render()


def test_replay_window_excludes_older_events(db_engine, frozen_clock) -> None:
    _publish(db_engine, frozen_clock, _incident_detected("INC-9"))
    later = (T0 + timedelta(hours=1)).isoformat()
    result = replay_module.replay_since(later, engine=db_engine)
    assert result.events == []
    assert result.ok is False
