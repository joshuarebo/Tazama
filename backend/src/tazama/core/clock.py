"""Time is injected, never read ambiently (CLAUDE.md section 5).

Everything is stored and computed in UTC. Conversion to `DISPLAY_TZ` happens at the
presentation edge only, through `to_display()`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    """A source of the current time. Inject this; never call `datetime.now()`."""

    def now(self) -> datetime:
        """Current instant, timezone-aware, in UTC."""
        ...


class SystemClock:
    """Real wall clock, always UTC."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """Deterministic clock for tests and replay."""

    __slots__ = ("_now",)

    def __init__(self, now: datetime) -> None:
        self._now = ensure_utc(now)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def set(self, now: datetime) -> None:
        self._now = ensure_utc(now)


class SequenceClock:
    """Yields a scripted series of instants, then repeats the last one.

    Used by replay and by tests that assert on ordering without sleeping.
    """

    __slots__ = ("_i", "_instants")

    def __init__(self, instants: Iterable[datetime]) -> None:
        self._instants = [ensure_utc(i) for i in instants]
        if not self._instants:
            raise ValueError("SequenceClock needs at least one instant")
        self._i = 0

    def now(self) -> datetime:
        instant = self._instants[min(self._i, len(self._instants) - 1)]
        self._i += 1
        return instant


def ensure_utc(value: datetime) -> datetime:
    """Normalise any datetime to an aware UTC datetime.

    A naive datetime is a bug in this codebase: we refuse it rather than guess a zone.
    """
    if value.tzinfo is None:
        raise ValueError(
            "naive datetime refused: TAZAMA stores UTC and requires timezone-aware values",
        )
    return value.astimezone(UTC)


def to_display(value: datetime, tz: ZoneInfo) -> datetime:
    """Convert a stored UTC instant to the display timezone (presentation edge only)."""
    return ensure_utc(value).astimezone(tz)


def floor_to(value: datetime, minutes: int) -> datetime:
    """Floor an instant to a KPI bucket (15-minute and hourly buckets are both used)."""
    if minutes <= 0 or 1440 % minutes:
        raise ValueError(f"bucket size {minutes} does not divide a day")
    value = ensure_utc(value)
    total = value.hour * 60 + value.minute
    floored = total - (total % minutes)
    return value.replace(
        hour=floored // 60,
        minute=floored % 60,
        second=0,
        microsecond=0,
    )


def window(end: datetime, span: timedelta) -> tuple[datetime, datetime]:
    """Half-open window `[end - span, end)` — the convention used by every query."""
    end = ensure_utc(end)
    if span <= timedelta(0):
        raise ValueError("window span must be positive")
    return end - span, end
