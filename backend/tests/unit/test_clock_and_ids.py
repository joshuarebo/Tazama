"""Time and identifier rules — the edge cases in PRD section 15 that bite silently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

from tazama.core.clock import (
    FixedClock,
    SequenceClock,
    SystemClock,
    ensure_utc,
    floor_to,
    to_display,
    window,
)
from tazama.core.ids import deterministic_id, new_correlation_id, new_id

NAIROBI = ZoneInfo("Africa/Nairobi")


def test_system_clock_is_utc_aware() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_naive_datetime_is_refused() -> None:
    with pytest.raises(ValueError, match="naive datetime"):
        ensure_utc(datetime(2026, 3, 17, 11, 30))


def test_nairobi_input_is_converted_not_relabelled() -> None:
    local = datetime(2026, 3, 17, 14, 30, tzinfo=NAIROBI)
    assert ensure_utc(local) == datetime(2026, 3, 17, 11, 30, tzinfo=UTC)


def test_display_conversion_is_three_hours_ahead() -> None:
    stored = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)
    shown = to_display(stored, NAIROBI)
    assert (shown.hour, shown.minute) == (14, 30)
    # Kenya has no DST, so the offset is constant year round.
    assert to_display(datetime(2026, 12, 25, 4, 30, tzinfo=UTC), NAIROBI).hour == 7


def test_daily_digest_time_maps_to_utc() -> None:
    """07:30 Africa/Nairobi (PRD section 11) must be scheduled as 04:30 UTC."""
    local = datetime(2026, 3, 17, 7, 30, tzinfo=NAIROBI)
    assert ensure_utc(local).strftime("%H:%M") == "04:30"


@pytest.mark.parametrize(
    ("minute", "bucket", "expected"),
    [(0, 15, 0), (7, 15, 0), (14, 15, 0), (15, 15, 15), (59, 15, 45), (59, 60, 0), (31, 60, 0)],
)
def test_floor_to_buckets(minute: int, bucket: int, expected: int) -> None:
    value = datetime(2026, 3, 17, 9, minute, 42, 500, tzinfo=UTC)
    floored = floor_to(value, bucket)
    assert floored.minute == expected
    assert floored.second == 0
    assert floored.microsecond == 0


def test_floor_to_rejects_bucket_that_does_not_divide_a_day() -> None:
    with pytest.raises(ValueError, match="does not divide a day"):
        floor_to(datetime(2026, 3, 17, 9, 0, tzinfo=UTC), 7)


@hyp_settings(max_examples=200, deadline=None)
@given(
    st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2035, 1, 1),
        timezones=st.just(UTC),
    ),
    st.sampled_from([5, 15, 30, 60]),
)
def test_floor_is_idempotent_and_never_moves_forward(value: datetime, bucket: int) -> None:
    once = floor_to(value, bucket)
    assert floor_to(once, bucket) == once
    assert once <= value
    assert (value - once) < timedelta(minutes=bucket)


def test_window_is_half_open() -> None:
    end = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)
    start, stop = window(end, timedelta(hours=1))
    assert start == datetime(2026, 3, 17, 11, 0, tzinfo=UTC)
    assert stop == end


def test_window_rejects_non_positive_span() -> None:
    with pytest.raises(ValueError, match="positive"):
        window(datetime(2026, 3, 17, tzinfo=UTC), timedelta(0))


def test_fixed_clock_advances_only_when_told() -> None:
    clock = FixedClock(datetime(2026, 3, 17, 11, 30, tzinfo=UTC))
    first = clock.now()
    assert clock.now() == first
    clock.advance(timedelta(minutes=15))
    assert clock.now() - first == timedelta(minutes=15)


def test_sequence_clock_repeats_last_instant() -> None:
    base = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)
    clock = SequenceClock([base, base + timedelta(minutes=1)])
    assert clock.now() == base
    assert clock.now() == base + timedelta(minutes=1)
    assert clock.now() == base + timedelta(minutes=1)


def test_ids_are_time_sortable() -> None:
    early = new_id("INC", at=datetime(2026, 3, 17, 11, 30, tzinfo=UTC))
    later = new_id("INC", at=datetime(2026, 3, 17, 11, 31, tzinfo=UTC))
    assert early < later
    assert early.startswith("INC_")


def test_new_id_requires_a_time_source() -> None:
    with pytest.raises(ValueError, match="clock"):
        new_id("INC")


def test_deterministic_ids_are_stable_and_distinct() -> None:
    first = deterministic_id("CMP", "2547", "2026-03-17T11:30", "no service")
    assert first == deterministic_id("CMP", "2547", "2026-03-17T11:30", "no service")
    assert first != deterministic_id("CMP", "2547", "2026-03-17T11:31", "no service")


def test_correlation_ids_are_unique_and_safe() -> None:
    ids = {new_correlation_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(cid.isalnum() for cid in ids)
