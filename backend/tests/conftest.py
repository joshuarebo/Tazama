"""Shared fixtures. Tests never touch the network and never read the developer's .env."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

#: A fixed instant used everywhere a test needs "now". Africa/Nairobi is UTC+3, so this
#: is 2026-03-17 14:30 local â€” inside business hours, which several rules care about.
FROZEN_NOW = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)

TEST_ENV: dict[str, str] = {
    "TAZAMA_ENV_FILE": "",  # ignore any .env on the machine running the tests
    "APP_ENV": "local",
    "APP_MODE": "demo",
    "LOG_LEVEL": "WARNING",
    "DISPLAY_TZ": "Africa/Nairobi",
    "ALLOW_LIVE_WRITES": "false",
    "DB_BACKEND": "sqlite",
    "SQLITE_PATH": ":memory:",
    "AUTH_MODE": "dev",
    "DEV_USERS": "admin:admin,noc:noc,care:care,analyst:analyst,manager:manager",
    "JWT_SECRET": "test-jwt-secret-value-0123456789",
    "PSEUDONYM_SALT": "test-pseudonym-salt-0123456789abc",
    "CORS_ORIGINS": "http://localhost:3000",
    "LLM_BASE_URL": "",
    "EMBEDDING_BASE_URL": "",
    "VECTOR_BACKEND": "memory",
    "TEAMS_DRY_RUN": "true",
    "TEAMS_WORKFLOW_WEBHOOK_URL": "",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    "RATE_LIMIT_PER_MIN": "10000",
}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test the same known environment, and reset the settings cache."""
    from tazama.core.settings import reset_settings_cache

    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    # Anything else from the host environment that could leak into settings.
    for leak in ("ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN", "LLM_API_KEY"):
        monkeypatch.delenv(leak, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def settings():
    from tazama.core.settings import get_settings

    return get_settings()


@pytest.fixture
def frozen_clock():
    from tazama.core.clock import FixedClock

    return FixedClock(FROZEN_NOW)


@pytest.fixture
def pseudonymiser():
    from tazama.core.security import Pseudonymiser

    return Pseudonymiser(TEST_ENV["PSEUDONYM_SALT"])


@pytest.fixture
def app(settings):
    from tazama.api.app import create_app

    return create_app(settings)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(settings):
    """Factory: `auth_headers("noc_engineer")` -> Authorization header for that role.

    Tokens are minted against the real clock on purpose: `verify_token` checks `exp`
    against wall-clock time, so a token minted from `frozen_clock` would be expired.
    """
    from tazama.core.auth import issue_token
    from tazama.core.clock import SystemClock
    from tazama.core.security import Principal

    def make(role: str = "admin", subject: str = "test-user") -> dict[str, str]:
        token = issue_token(
            Principal(subject=subject, role=role, display_name=subject),
            secret=settings.JWT_SECRET.get_secret_value(),
            clock=SystemClock(),
            ttl_s=3600,
        )
        return {"Authorization": f"Bearer {token}"}

    return make


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_llm_app():
    """The deterministic fake LLM as an in-process ASGI app (no server, no sockets)."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "tazama_fake_llm",
        root / "scripts" / "fake_llm.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tazama_fake_llm"] = module
    spec.loader.exec_module(module)
    return module.app


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("NO_COLOR", "1")
