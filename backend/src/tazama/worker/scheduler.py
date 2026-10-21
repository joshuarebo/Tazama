"""Time-based work: the scheduler half of the worker.

Schedules are expressed in **local time** (Africa/Nairobi) because that is how the business
states them ("the daily digest goes out at 07:30"), and converted to UTC for every
comparison. Getting that conversion wrong by three hours is the classic bug here, so the
conversion lives in one place and is tested at the boundaries.

Jobs are idempotent and keyed: running the scheduler twice in the same minute must not
produce two digests. The key is `(job name, local date, local time)`, recorded by the job
itself through `ScheduledReport.idempotency_key` or an equivalent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from tazama.core.clock import Clock
from tazama.core.logging import get_logger

_log = get_logger(__name__)

JobFn = Callable[[datetime], None]

#: Weekday names accepted in a schedule, Monday first.
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class Schedule:
    """`at` in local time, optionally restricted to given weekdays.

    With `every_minutes` set, the schedule is an interval instead of a wall-clock time —
    used for health sweeps, where "every 15 minutes" is the requirement and the exact
    minute does not matter.
    """

    at: time
    tz: str = "Africa/Nairobi"
    weekdays: tuple[str, ...] = ()
    every_minutes: int | None = None

    def __post_init__(self) -> None:
        if self.every_minutes is not None and not 1 <= self.every_minutes <= 1440:
            raise ValueError("every_minutes must be between 1 and 1440")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def matches(self, moment: datetime) -> bool:
        """Does this schedule fire in the minute containing `moment` (a UTC instant)?"""
        local = moment.astimezone(self.zone)
        if self.weekdays and WEEKDAYS[local.weekday()] not in self.weekdays:
            return False
        if self.every_minutes is not None:
            return (local.hour * 60 + local.minute) % self.every_minutes == 0
        return (local.hour, local.minute) == (self.at.hour, self.at.minute)

    def run_key(self, moment: datetime) -> str:
        """Idempotency key for the run that `moment` belongs to."""
        local = moment.astimezone(self.zone)
        if self.every_minutes is not None:
            bucket = (local.hour * 60 + local.minute) // self.every_minutes
            return f"{local.date().isoformat()}#{bucket}"
        return f"{local.date().isoformat()}T{self.at.hour:02d}:{self.at.minute:02d}"

    def next_after(self, moment: datetime) -> datetime:
        """The next UTC instant this schedule fires after `moment`.

        Computed in local time and converted back, so a DST-observing timezone would be
        handled correctly too (Kenya does not observe DST, but the next deployment might
        not be in Kenya).
        """
        local = moment.astimezone(self.zone)
        candidate = datetime.combine(local.date(), self.at, tzinfo=self.zone)
        if candidate <= local:
            candidate = datetime.combine(
                local.date() + timedelta(days=1), self.at, tzinfo=self.zone
            )
        while self.weekdays and WEEKDAYS[candidate.weekday()] not in self.weekdays:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)


@dataclass(slots=True)
class Job:
    name: str
    schedule: Schedule
    run: JobFn
    description: str = ""
    last_run_key: str | None = None
    failures: int = 0


@dataclass(slots=True)
class TickReport:
    at: datetime
    fired: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def render(self) -> str:
        return (
            f"tick {self.at.isoformat()} fired={len(self.fired)} "
            f"skipped={len(self.skipped)} failed={len(self.failed)}"
        )


class Scheduler:
    """Minute-resolution scheduler. `tick()` is called by the worker loop."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._jobs: list[Job] = []

    def add(
        self,
        name: str,
        schedule: Schedule,
        run: JobFn,
        *,
        description: str = "",
    ) -> None:
        if any(job.name == name for job in self._jobs):
            raise ValueError(f"duplicate scheduled job: {name}")
        self._jobs.append(Job(name=name, schedule=schedule, run=run, description=description))

    @property
    def jobs(self) -> tuple[Job, ...]:
        return tuple(self._jobs)

    def tick(self, moment: datetime | None = None) -> TickReport:
        """Run whatever is due. A failing job never stops the others."""
        now = moment or self._clock.now()
        report = TickReport(at=now)
        for job in self._jobs:
            if not job.schedule.matches(now):
                continue
            key = job.schedule.run_key(now)
            if job.last_run_key == key:
                # Already ran in this window: ticking twice a minute is normal.
                report.skipped.append(job.name)
                continue
            try:
                job.run(now)
            except Exception as exc:
                job.failures += 1
                report.failed.append(job.name)
                _log.error(
                    "scheduled_job_failed",
                    extra={"job": job.name, "error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            job.last_run_key = key
            report.fired.append(job.name)
            _log.info("scheduled_job_ran", extra={"job": job.name, "run_key": key})
        return report

    def describe(self) -> str:
        """Human-readable schedule, for `make doctor` and the admin page."""
        now = self._clock.now()
        lines = ["scheduled jobs:"]
        for job in self._jobs:
            days = ",".join(job.schedule.weekdays) if job.schedule.weekdays else "daily"
            lines.append(
                f"  {job.name:<28} {job.schedule.at.strftime('%H:%M')} {job.schedule.tz} "
                f"({days}) next={job.schedule.next_after(now).isoformat()}",
            )
        return "\n".join(lines)


def daily(at: str, tz: str = "Africa/Nairobi") -> Schedule:
    hour, _, minute = at.partition(":")
    return Schedule(at=time(int(hour), int(minute or 0)), tz=tz)


def weekly(at: str, weekday: str, tz: str = "Africa/Nairobi") -> Schedule:
    hour, _, minute = at.partition(":")
    day = weekday.lower()[:3]
    if day not in WEEKDAYS:
        raise ValueError(f"unknown weekday {weekday!r}")
    return Schedule(at=time(int(hour), int(minute or 0)), tz=tz, weekdays=(day,))


def local_date(moment: datetime, tz: str = "Africa/Nairobi") -> date:
    """The business day a UTC instant belongs to."""
    return moment.astimezone(ZoneInfo(tz)).date()


def build_jobs(
    *,
    settings: object,
    policies: object = None,  # noqa: ARG001 - the Phase 6 reporting jobs need these
    clock: Clock | None = None,  # noqa: ARG001 - same
    engine: object = None,  # noqa: ARG001 - same
) -> tuple[Job, ...]:
    """The worker's scheduled work.

    Declared in one place so `make doctor` and the admin page can print the real schedule
    rather than a comment about it. Reporting jobs (daily digest at 07:30 Africa/Nairobi,
    weekly summary) are added in Phase 6; the health sweep below runs from Phase 2.
    """
    from tazama.infrastructure.eventbus.dispatcher import _outbox_lag_sync

    def outbox_health(_at: datetime) -> None:
        lag, pending = _outbox_lag_sync(settings)  # type: ignore[arg-type]
        if pending:
            # Visible on every sweep so a growing backlog is noticed before it matters
            # (docs/runbooks/outbox-backlog.md).
            _log.info("outbox_health", extra={"lag_s": round(lag, 1), "pending": pending})

    return (
        Job(
            name="outbox-health",
            schedule=Schedule(at=time(0, 0), tz="UTC", every_minutes=15),
            run=outbox_health,
            description="log outbox lag every 15 minutes so a backlog is noticed early",
        ),
    )
