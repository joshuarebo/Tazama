"""The environment contract (PRD section 4) is enforced, not merely documented."""

from __future__ import annotations

import pytest

from tazama.core.settings import ConfigurationError, Settings, get_settings, reset_settings_cache


def _load(monkeypatch: pytest.MonkeyPatch, **overrides: str):
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    return get_settings()


def test_demo_defaults_are_safe(settings: Settings) -> None:
    assert settings.APP_MODE == "demo"
    assert settings.ALLOW_LIVE_WRITES is False
    assert settings.TEAMS_DRY_RUN is True
    assert settings.SMS_GATEWAY_MODE == "mock"
    assert settings.AI_GNN_ENABLED is False
    assert settings.AI_BANDIT_ENABLED is False
    assert settings.source_writes_allowed is True  # demo mode may write


def test_live_mode_requires_source_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        _load(monkeypatch, APP_MODE="live")
    message = str(excinfo.value)
    assert "SOURCE_ORACLE_USER" in message
    assert "SOURCE_ORACLE_DSN" in message


def test_live_mode_defaults_to_no_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(
        monkeypatch,
        APP_MODE="live",
        SOURCE_ORACLE_USER="reader",
        SOURCE_ORACLE_PASSWORD="x",
        SOURCE_ORACLE_DSN="host:1521/PDB",
    )
    assert settings.ALLOW_LIVE_WRITES is False
    assert settings.source_writes_allowed is False


def test_prod_refuses_live_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="ALLOW_LIVE_WRITES"):
        _load(
            monkeypatch,
            APP_ENV="prod",
            APP_MODE="live",
            ALLOW_LIVE_WRITES="true",
            AUTH_MODE="oidc",
            OIDC_ISSUER="https://login.example/tenant",
            OIDC_CLIENT_ID="client",
            OIDC_CLIENT_SECRET="secret",
            JWT_SECRET="a-real-secret-value-32-characters",
            PSEUDONYM_SALT="another-real-salt-32-characters!!",
            SOURCE_ORACLE_USER="reader",
            SOURCE_ORACLE_PASSWORD="x",
            SOURCE_ORACLE_DSN="host:1521/PDB",
        )


def test_oracle_backend_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        _load(monkeypatch, DB_BACKEND="oracle")
    assert "ORACLE_USER" in str(excinfo.value)
    assert "ORACLE_DSN" in str(excinfo.value)


def test_error_lists_every_problem_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        _load(monkeypatch, LOG_LEVEL="LOUD", DISPLAY_TZ="Mars/Olympus", APP_MODE="sideways")
    message = str(excinfo.value)
    assert "LOG_LEVEL" in message
    assert "DISPLAY_TZ" in message
    assert "APP_MODE" in message
    assert "make doctor" in message


def test_cors_wildcard_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="allowlist"):
        _load(monkeypatch, CORS_ORIGINS="*")


def test_cors_origins_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(monkeypatch, CORS_ORIGINS="http://localhost:3000, https://tazama.internal")
    assert settings.cors_origins == ["http://localhost:3000", "https://tazama.internal"]


def test_placeholder_secrets_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError) as excinfo:
        _load(
            monkeypatch,
            APP_ENV="staging",
            AUTH_MODE="oidc",
            OIDC_ISSUER="https://login.example/tenant",
            OIDC_CLIENT_ID="client",
            OIDC_CLIENT_SECRET="secret",
            PSEUDONYM_SALT="replace-with-salt",
            JWT_SECRET="dev-only-jwt",
        )
    message = str(excinfo.value)
    assert "PSEUDONYM_SALT" in message
    assert "JWT_SECRET" in message


def test_dev_auth_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="AUTH_MODE=dev"):
        _load(
            monkeypatch,
            APP_ENV="prod",
            PSEUDONYM_SALT="a-real-salt-value-32-characters!",
            JWT_SECRET="a-real-jwt-value-32-characters!!",
        )


def test_oidc_requires_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="OIDC_ISSUER"):
        _load(monkeypatch, AUTH_MODE="oidc")


def test_teams_real_send_requires_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="TEAMS_WORKFLOW_WEBHOOK_URL"):
        _load(monkeypatch, TEAMS_DRY_RUN="false")


def test_oracle_vector_backend_requires_oracle_db(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="VECTOR_BACKEND=oracle"):
        _load(monkeypatch, VECTOR_BACKEND="oracle")


def test_pool_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="ORACLE_POOL_MAX"):
        _load(
            monkeypatch,
            DB_BACKEND="oracle",
            ORACLE_USER="u",
            ORACLE_PASSWORD="p",
            ORACLE_DSN="host:1521/PDB",
            ORACLE_POOL_MIN="8",
            ORACLE_POOL_MAX="2",
        )


def test_bad_llm_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match="LLM_BASE_URL"):
        _load(monkeypatch, LLM_BASE_URL="not-a-url")


def test_llm_unset_means_degraded(settings: Settings) -> None:
    assert settings.llm_configured is False
    assert settings.embeddings_configured is False


def test_trailing_slash_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(monkeypatch, LLM_BASE_URL="http://llm.internal:8000/v1/")
    assert settings.LLM_BASE_URL == "http://llm.internal:8000/v1"


def test_dev_users_roles_are_canonicalised(settings: Settings) -> None:
    users = settings.dev_users
    assert users["noc"] == "noc_engineer"
    assert users["care"] == "care_agent"
    assert users["admin"] == "admin"


def test_dev_users_rejects_unknown_role(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(monkeypatch, DEV_USERS="bob:superuser")
    with pytest.raises(ConfigurationError, match="superuser"):
        _ = settings.dev_users


def test_redacted_dump_hides_every_secret(settings: Settings) -> None:
    dump = settings.redacted()
    for secret_field in (
        "ORACLE_PASSWORD",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "PSEUDONYM_SALT",
        "JWT_SECRET",
        "OIDC_CLIENT_SECRET",
        "TEAMS_WORKFLOW_WEBHOOK_URL",
        "SOURCE_ORACLE_PASSWORD",
    ):
        assert dump[secret_field] == "***", secret_field
    assert dump["APP_MODE"] == "demo"


def test_display_tz_is_nairobi_and_resolvable(settings: Settings) -> None:
    assert settings.DISPLAY_TZ == "Africa/Nairobi"
    assert settings.display_tz.key == "Africa/Nairobi"


def test_settings_are_cached(settings: Settings) -> None:
    assert get_settings() is settings
