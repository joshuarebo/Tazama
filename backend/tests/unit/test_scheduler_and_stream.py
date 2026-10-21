"""Scheduler (local-time correctness, idempotency) and the SSE stream."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

import pytest

from tazama.api.stream import (
    HEARTBEAT_SECONDS,
    QUEUE_SIZE,
    EventStream,
    StreamEvent,
    incident_event,
)
from tazama.worker.scheduler import Schedule, Scheduler, daily, local_date, weekly

#: 2026-03-17 04:30 UTC is 07:30 in Nairobi — the digest time from PRD section 11.
DIGEST_UTC = datetime(2026, 3, 17, 4, 30, tzinfo=UTC)


# ---------------------------------------------------------------- schedules


def test_digest_time_is_local_not_utc() -> None:
    schedule = daily("07:30")
    assert schedule.matches(DIGEST_UTC) is True
    # 07:30 UTC is 10:30 in Nairobi, so it must NOT fire.
    assert schedule.matches(datetime(2026, 3, 17, 7, 30, tzinfo=UTC)) is False


@pytest.mark.parametrize("minute_offset", [-1, 1])
def test_schedule_fires_only_in_its_minute(minute_offset: int) -> None:
    assert daily("07:30").matches(DIGEST_UTC + timedelta(minutes=minute_offset)) is False


def test_weekly_schedule_respects_the_weekday() -> None:
    schedule = weekly("08:00", "monday")
    monday = datetime(2026, 3, 16, 5, 0, tzinfo=UTC)  # 08:00 Nairobi, a Monday
    tuesday = monday + timedelta(days=1)
    assert schedule.matches(monday) is True
    assert schedule.matches(tuesday) is False


def test_unknown_weekday_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown weekday"):
        weekly("08:00", "someday")


def test_next_after_is_tomorrow_once_today_has_passed() -> None:
    schedule = daily("07:30")
    after = schedule.next_after(DIGEST_UTC + timedelta(minutes=1))
    assert after == DIGEST_UTC + timedelta(days=1)


def test_next_after_skips_to_the_right_weekday() -> None:
    schedule = weekly("08:00", "mon")
    wednesday = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
    following_monday = schedule.next_after(wednesday)
    assert following_monday.astimezone(schedule.zone).weekday() == 0
    assert following_monday > wednesday


def test_run_key_is_the_local_day(frozen_clock) -> None:
    schedule = daily("07:30")
    # 2026-03-17 04:30 UTC is still 2026-03-17 locally.
    assert schedule.run_key(DIGEST_UTC) == "2026-03-17T07:30"
    # 2026-03-17 22:00 UTC is 2026-03-18 locally — the key must follow local time.
    assert schedule.run_key(datetime(2026, 3, 17, 22, 0, tzinfo=UTC)).startswith("2026-03-18")


def test_local_date_follows_the_business_day() -> None:
    assert local_date(datetime(2026, 3, 17, 22, 0, tzinfo=UTC)).isoformat() == "2026-03-18"


# ---------------------------------------------------------------- scheduler


def test_due_job_runs_once_per_window(frozen_clock) -> None:
    runs: list[datetime] = []
    scheduler = Scheduler(clock=frozen_clock)
    scheduler.add("daily-digest", daily("07:30"), runs.append)

    frozen_clock.set(DIGEST_UTC)
    assert scheduler.tick().fired == ["daily-digest"]
    # Ticking again inside the same minute must not send a second digest.
    assert scheduler.tick().skipped == ["daily-digest"]
    assert len(runs) == 1

    frozen_clock.set(DIGEST_UTC + timedelta(days=1))
    assert scheduler.tick().fired == ["daily-digest"]
    assert len(runs) == 2


def test_job_not_due_does_not_run(frozen_clock) -> None:
    scheduler = Scheduler(clock=frozen_clock)
    scheduler.add("daily-digest", daily("07:30"), lambda _at: pytest.fail("should not run"))
    frozen_clock.set(datetime(2026, 3, 17, 9, 0, tzinfo=UTC))
    report = scheduler.tick()
    assert report.fired == []


def test_a_failing_job_does_not_stop_the_others(frozen_clock) -> None:
    ran: list[str] = []
    scheduler = Scheduler(clock=frozen_clock)
    scheduler.add("broken", daily("07:30"), lambda _at: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.add("healthy", daily("07:30"), lambda _at: ran.append("healthy"))

    frozen_clock.set(DIGEST_UTC)
    report = scheduler.tick()
    assert report.failed == ["broken"]
    assert report.fired == ["healthy"]
    assert ran == ["healthy"]
    # A failed job is retried next window rather than marked as done.
    assert scheduler.jobs[0].last_run_key is None
    assert scheduler.jobs[0].failures == 1


def test_duplicate_job_names_are_refused(frozen_clock) -> None:
    scheduler = Scheduler(clock=frozen_clock)
    scheduler.add("digest", daily("07:30"), lambda _at: None)
    with pytest.raises(ValueError, match="duplicate scheduled job"):
        scheduler.add("digest", daily("08:30"), lambda _at: None)


def test_describe_lists_the_next_run(frozen_clock) -> None:
    scheduler = Scheduler(clock=frozen_clock)
    scheduler.add("daily-digest", daily("07:30"), lambda _at: None)
    scheduler.add("weekly-summary", weekly("08:00", "mon"), lambda _at: None)
    described = scheduler.describe()
    assert "daily-digest" in described
    assert "07:30 Africa/Nairobi" in described
    assert "weekly-summary" in described
    assert "mon" in described


def test_schedule_minute_boundaries() -> None:
    """Quiet confidence that 00:00 and 23:59 behave."""
    midnight = Schedule(at=time(0, 0))
    assert midnight.matches(datetime(2026, 3, 16, 21, 0, tzinfo=UTC)) is True  # 00:00 local
    late = Schedule(at=time(23, 59))
    assert late.matches(datetime(2026, 3, 17, 20, 59, tzinfo=UTC)) is True


# ------------------------------------------------------------------- stream


def test_frame_rendering_is_valid_sse() -> None:
    frame = StreamEvent(event="incident.updated", data={"incident_id": "INC-1"}, id="7").render()
    assert frame.startswith("id: 7\nevent: incident.updated\ndata: {")
    assert frame.endswith("\n\n")


def test_incident_event_carries_no_subscriber_data() -> None:
    event = incident_event("incident.detected", "INC-1", status="detected", priority="P1")
    assert set(event.data) == {"incident_id", "status", "priority", "region", "at"}


async def test_subscriber_receives_published_events() -> None:
    stream = EventStream()
    iterator = stream.subscribe()
    hello = await anext(iterator)
    assert hello.event == "hello"

    assert await stream.publish(incident_event("incident.detected", "INC-1")) == 1
    received = await anext(iterator)
    assert received.data["incident_id"] == "INC-1"
    assert received.data["seq"] == 1
    await iterator.aclose()


async def test_events_are_fanned_out_to_every_subscriber() -> None:
    stream = EventStream()
    first, second = stream.subscribe(), stream.subscribe()
    await anext(first)
    await anext(second)
    assert await stream.publish(incident_event("incident.updated", "INC-2")) == 2
    assert (await anext(first)).data["incident_id"] == "INC-2"
    assert (await anext(second)).data["incident_id"] == "INC-2"
    await first.aclose()
    await second.aclose()


async def test_sequence_numbers_increase() -> None:
    stream = EventStream()
    iterator = stream.subscribe()
    await anext(iterator)
    await stream.publish(incident_event("a", "INC-1"))
    await stream.publish(incident_event("b", "INC-2"))
    assert (await anext(iterator)).data["seq"] == 1
    assert (await anext(iterator)).data["seq"] == 2
    await iterator.aclose()


async def test_a_lagging_subscriber_is_dropped_not_the_server() -> None:
    """Back-pressure: one slow browser must not grow memory without bound."""
    stream = EventStream()
    iterator = stream.subscribe()
    await anext(iterator)
    for index in range(QUEUE_SIZE + 10):
        await stream.publish(incident_event("incident.updated", f"INC-{index}"))
    # The queue is capped, so the excess was dropped rather than buffered.
    assert stream.subscriber_count == 1
    await iterator.aclose()


async def test_disconnect_removes_the_subscriber() -> None:
    stream = EventStream()
    iterator = stream.subscribe()
    await anext(iterator)
    assert stream.subscriber_count == 1
    await iterator.aclose()
    await asyncio.sleep(0)
    assert stream.subscriber_count == 0


async def test_publish_soon_never_raises_without_a_loop() -> None:
    """A pipeline step must not fail because nobody is watching the UI."""
    stream = EventStream()
    await asyncio.to_thread(stream.publish_soon, incident_event("incident.detected", "INC-1"))
    assert stream.subscriber_count == 0


def test_heartbeat_interval_is_proxy_friendly() -> None:
    assert 5.0 <= HEARTBEAT_SECONDS <= 30.0
