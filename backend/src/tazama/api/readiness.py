"""Readiness probes, shared by `/readyz` and `scripts/doctor.py`.

Each probe is independent, time-boxed and never raises: a probe failure is data, not an
exception. Phases add subsystems as they land (DB in Phase 1, outbox in Phase 2, vector
store and LLM in Phase 4) and a probe reports `skipped` until its subsystem exists — it
never reports `ok` for something that is not there.

Subsystems are resolved dynamically so an unfinished phase cannot break startup.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from tazama.api.v1.routers.health import Check, CheckStatus
from tazama.core.logging import get_logger
from tazama.core.settings import Settings

_log = get_logger(__name__)

Probe = Callable[[Settings], Awaitable[Check]]


def _resolve(module_path: str, attribute: str) -> Any | None:
    """Return `module.attribute` if the phase that owns it has landed, else None."""
    try:
        module = importlib.import_module(module_path)
    except ImportError:
        return None
    return getattr(module, attribute, None)


async def _timed(name: str, settings: Settings, fn: Probe) -> Check:
    started = time.perf_counter()
    try:
        check = await fn(settings)
    except Exception as exc:  # a probe must never take the endpoint down
        _log.warning("probe_failed", extra={"probe": name, "error": str(exc)})
        check = Check(name=name, status="down", detail=type(exc).__name__)
    check.duration_ms = round((time.perf_counter() - started) * 1000, 2)
    return check


async def _config_probe(settings: Settings) -> Check:
    # Reaching this point means the settings model validated, so configuration is sound.
    return Check(
        name="config",
        status="ok",
        detail=f"mode={settings.APP_MODE} env={settings.APP_ENV}",
    )


async def _database_probe(settings: Settings) -> Check:
    ping = _resolve("tazama.infrastructure.db", "ping")
    if ping is None:
        return Check(name="database", status="skipped", detail="not implemented until Phase 1")
    ok, detail = await ping(settings)
    return Check(
        name="database",
        status="ok" if ok else "down",
        detail=f"{settings.DB_BACKEND}: {detail}",
    )


async def _llm_probe(settings: Settings) -> Check:
    if not settings.llm_configured:
        return Check(
            name="llm",
            status="degraded",
            detail="LLM_BASE_URL unset: AI features use deterministic fallbacks",
        )
    probe_summary = _resolve("tazama.ai.llm.capability_probe", "probe_summary")
    if probe_summary is None:
        return Check(name="llm", status="skipped", detail="not implemented until Phase 4")
    ok, detail = await probe_summary(settings)
    return Check(name="llm", status="ok" if ok else "degraded", detail=detail)


async def _vector_probe(settings: Settings) -> Check:
    describe = _resolve("tazama.infrastructure.vectorstore", "describe_backend")
    if describe is None:
        return Check(name="vector", status="skipped", detail="not implemented until Phase 4")
    ok, detail = await describe(settings)
    return Check(
        name="vector",
        status="ok" if ok else "degraded",
        detail=f"{settings.VECTOR_BACKEND}: {detail}",
    )


async def _outbox_probe(settings: Settings) -> Check:
    outbox_lag = _resolve("tazama.infrastructure.eventbus", "outbox_lag")
    if outbox_lag is None:
        return Check(name="outbox", status="skipped", detail="not implemented until Phase 2")
    lag, pending = await outbox_lag(settings)
    # Sustained lag means events are piling up: see docs/runbooks/outbox-backlog.md.
    status: CheckStatus = "ok" if lag < 60 else "degraded"
    return Check(name="outbox", status=status, detail=f"lag={lag:.1f}s pending={pending}")


PROBES: dict[str, Probe] = {
    "config": _config_probe,
    "database": _database_probe,
    "llm": _llm_probe,
    "vector": _vector_probe,
    "outbox": _outbox_probe,
}


async def run_checks(settings: Settings) -> list[Check]:
    return [await _timed(name, settings, probe) for name, probe in PROBES.items()]
