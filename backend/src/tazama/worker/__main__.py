"""The worker process: drain the outbox, run scheduled work, exit cleanly.

`python -m tazama.worker` runs one loop that does two things on every tick: dispatch
pending events, then run anything the scheduler says is due. One process, one loop, no
hidden concurrency — a second worker would double-deliver, so scaling out needs a claim
mechanism first (recorded in `docs/tech-debt.md`).

Shutdown is graceful: SIGINT/SIGTERM finishes the current tick and stops, so an event is
never half-dispatched.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from dataclasses import dataclass
from types import FrameType

from tazama.core.clock import SystemClock
from tazama.core.logging import configure_logging, get_logger
from tazama.core.policy import get_policies
from tazama.core.settings import ConfigurationError, Settings, get_settings
from tazama.core.tracing import configure_tracing
from tazama.infrastructure.db.engine import check_connection, get_engine
from tazama.infrastructure.eventbus import OutboxDispatcher
from tazama.worker.consumers import build_consumers
from tazama.worker.scheduler import Scheduler, build_jobs

_log = get_logger(__name__)

#: How long to wait between ticks when there was nothing to do. Short enough that the UI
#: feels live, long enough not to hammer the database.
IDLE_SLEEP_S = 2.0
BUSY_SLEEP_S = 0.1


@dataclass
class _Shutdown:
    requested: bool = False

    def request(self, signum: int, _frame: FrameType | None) -> None:
        self.requested = True
        _log.info("worker_shutdown_requested", extra={"signal": signum})


def run(settings: Settings | None = None, *, max_ticks: int | None = None) -> int:
    """Run the worker loop. `max_ticks` bounds it for tests and for `make demo` smoke runs."""
    settings = settings or get_settings()
    configure_logging(level=settings.LOG_LEVEL, service="tazama-worker", mode=settings.APP_MODE)
    configure_tracing(
        "tazama-worker", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT, environment=settings.APP_ENV
    )

    ok, detail = check_connection(settings)
    if not ok:
        # Fail fast and loudly: a worker that cannot reach the database has nothing to do,
        # and silently retrying forever hides an outage (see docs/runbooks/db-down.md).
        _log.error("worker_cannot_start", extra={"reason": detail})
        return 1

    policies = get_policies()
    clock = SystemClock()
    engine = get_engine(settings)
    dispatcher = OutboxDispatcher(engine=engine, clock=clock)
    scheduler = Scheduler(clock=clock)

    consumers = build_consumers(settings=settings, policies=policies, clock=clock, engine=engine)
    dispatcher.register_many(consumers)
    for job in build_jobs(settings=settings, policies=policies, clock=clock, engine=engine):
        scheduler.add(job.name, job.schedule, job.run, description=job.description)

    _log.info(
        "worker_start",
        extra={
            "app_mode": settings.APP_MODE,
            "db_backend": settings.DB_BACKEND,
            "event_types": len(dispatcher.registered_types),
            "jobs": len(scheduler.jobs),
        },
    )
    if scheduler.jobs:
        _log.info("worker_schedule", extra={"schedule": scheduler.describe()})

    shutdown = _Shutdown()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Not every platform (or thread) allows installing a handler; a worker without
        # graceful shutdown still works, it just stops abruptly.
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, shutdown.request)

    ticks = 0
    while not shutdown.requested:
        if max_ticks is not None and ticks >= max_ticks:
            break
        ticks += 1
        report = dispatcher.poll_once()
        scheduler.tick()
        if report.dead_lettered:
            _log.error("worker_dead_letters", extra={"count": report.dead_lettered})
        if max_ticks is None:
            time.sleep(BUSY_SLEEP_S if not report.idle else IDLE_SLEEP_S)

    _log.info("worker_stop", extra={"ticks": ticks})
    return 0


def main() -> int:
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    max_ticks_env = os.environ.get("TAZAMA_WORKER_MAX_TICKS", "").strip()
    return run(settings, max_ticks=int(max_ticks_env) if max_ticks_env.isdigit() else None)


if __name__ == "__main__":
    sys.exit(main())
