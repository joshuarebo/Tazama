"""The ONE place that reads the environment (PRD section 4, CLAUDE.md section 4).

Rules enforced here:
  * No hardcoded URL, host, DSN, credential, model name, salt or webhook anywhere else.
  * Fail fast: invalid configuration raises a single error that lists every offending variable.
  * `APP_MODE=live` tightens the defaults (writes off, source credentials required).

Anything that needs configuration imports `get_settings()`; nothing else calls `os.environ`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "dev", "staging", "prod"]
AppMode = Literal["demo", "live"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DbBackend = Literal["sqlite", "oracle"]
ToolCalling = Literal["auto", "on", "off"]
VectorBackend = Literal["memory", "oracle"]
AuthMode = Literal["dev", "oidc"]
AdapterMode = Literal["mock", "live_stub"]
SmsMode = Literal["mock"]

#: Secrets must be at least this long to be worth the name.
MIN_SECRET_LEN = 16

Role = Literal["care_agent", "noc_engineer", "analyst", "manager", "admin"]
ALL_ROLES: tuple[str, ...] = ("care_agent", "noc_engineer", "analyst", "manager", "admin")


class ConfigurationError(RuntimeError):
    """Raised at startup when the environment contract is not satisfied."""


class Settings(BaseSettings):
    """Validated snapshot of the environment contract."""

    model_config = SettingsConfigDict(
        # The env file is chosen at *instantiation* time (see `get_settings`) so that tests
        # and the doctor can opt out of a developer's local .env.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        validate_default=True,
    )

    # ------------------------------------------------------------------ app
    APP_ENV: AppEnv = "local"
    APP_MODE: AppMode = "demo"
    LOG_LEVEL: LogLevel = "INFO"
    DISPLAY_TZ: str = "Africa/Nairobi"
    ALLOW_LIVE_WRITES: bool = False

    # ------------------------------------------------------------- database
    DB_BACKEND: DbBackend = "sqlite"
    SQLITE_PATH: str = "./data/tazama.db"
    ORACLE_USER: str = ""
    ORACLE_PASSWORD: SecretStr = SecretStr("")
    ORACLE_DSN: str = ""
    ORACLE_POOL_MIN: Annotated[int, Field(ge=0, le=64)] = 1
    ORACLE_POOL_MAX: Annotated[int, Field(ge=1, le=256)] = 8
    ORACLE_WALLET_DIR: str = ""

    # --------------------------------------------- live read-only source db
    SOURCE_ORACLE_USER: str = ""
    SOURCE_ORACLE_PASSWORD: SecretStr = SecretStr("")
    SOURCE_ORACLE_DSN: str = ""
    SOURCE_QUERY_TIMEOUT_S: Annotated[int, Field(ge=1, le=600)] = 30
    SOURCE_MAX_ROWS: Annotated[int, Field(ge=1, le=1_000_000)] = 5000

    # ------------------------------------------------------------------ llm
    LLM_BASE_URL: str = ""
    LLM_API_KEY: SecretStr = SecretStr("")
    LLM_MODEL_REASONING: str = "qwen-reasoning"
    LLM_MODEL_FAST: str = "qwen-fast"
    LLM_TIMEOUT_S: Annotated[float, Field(gt=0, le=600)] = 30.0
    LLM_MAX_RETRIES: Annotated[int, Field(ge=0, le=10)] = 2
    LLM_TOOL_CALLING: ToolCalling = "auto"
    LLM_CACHE_TTL_S: Annotated[int, Field(ge=0, le=86_400)] = 900
    LLM_MAX_PROMPT_CHARS: Annotated[int, Field(ge=1_000, le=1_000_000)] = 24_000

    # ----------------------------------------------------------- embeddings
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: SecretStr = SecretStr("")
    EMBEDDING_MODEL: str = "qwen-embedding"
    EMBEDDING_DIM: Annotated[int, Field(ge=16, le=4096)] = 256
    VECTOR_BACKEND: VectorBackend = "memory"

    # ---------------------------------------------------------------- teams
    TEAMS_WORKFLOW_WEBHOOK_URL: SecretStr = SecretStr("")
    TEAMS_DRY_RUN: bool = True

    # ------------------------------------------------------------- security
    PSEUDONYM_SALT: SecretStr = SecretStr("dev-only-salt-not-for-production")
    AUTH_MODE: AuthMode = "dev"
    DEV_USERS: str = "admin:admin,noc:noc,care:care,analyst:analyst,manager:manager"
    OIDC_ISSUER: str = ""
    OIDC_CLIENT_ID: str = ""
    OIDC_CLIENT_SECRET: SecretStr = SecretStr("")
    JWT_SECRET: SecretStr = SecretStr("dev-only-jwt-secret-not-for-production")
    JWT_TTL_S: Annotated[int, Field(ge=60, le=86_400)] = 3600
    CORS_ORIGINS: str = "http://localhost:3000"
    RATE_LIMIT_PER_MIN: Annotated[int, Field(ge=1, le=100_000)] = 120
    SQL_EXPLORER_MAX_ROWS: Annotated[int, Field(ge=1, le=100_000)] = 1000
    SQL_EXPLORER_TIMEOUT_S: Annotated[int, Field(ge=1, le=300)] = 15

    # --------------------------------------------------------- integrations
    SMS_GATEWAY_MODE: SmsMode = "mock"
    SOCIAL_SIGNALS_ENABLED: bool = False
    ADAPTER_MODE: AdapterMode = "mock"

    # -------------------------------------------------------- observability
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_SERVICE_NAME: str = "tazama-api"

    # ------------------------------------------------------- ai kill switches
    AI_CLASSIFICATION_ENABLED: bool = True
    AI_DIAGNOSIS_AGENT_ENABLED: bool = True
    AI_KNOWLEDGE_ASSISTANT_ENABLED: bool = True
    AI_REPORTER_ENABLED: bool = True
    AI_GNN_ENABLED: bool = False
    AI_BANDIT_ENABLED: bool = False

    # ------------------------------------------------------------------ web
    NEXT_PUBLIC_API_BASE_URL: str = "http://localhost:8000"
    NEXT_PUBLIC_MAP_TILE_URL: str = ""
    NEXT_PUBLIC_APP_MODE: str = "demo"

    # ------------------------------------------------------------ validators
    @field_validator("DISPLAY_TZ")
    @classmethod
    def _tz_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - env specific
            raise ValueError(f"DISPLAY_TZ={value!r} is not a known IANA timezone") from exc
        return value

    @field_validator("LLM_BASE_URL", "EMBEDDING_BASE_URL", "NEXT_PUBLIC_API_BASE_URL")
    @classmethod
    def _url_or_empty(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        # Parsing with pydantic keeps the rules in one place and rejects junk early.
        AnyHttpUrl(value)
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _origins_are_urls(cls, value: str) -> str:
        for origin in (o.strip() for o in value.split(",") if o.strip()):
            if origin == "*":
                raise ValueError("CORS_ORIGINS must be an explicit allowlist, never '*'")
            AnyHttpUrl(origin)
        return value

    @model_validator(mode="after")
    def _pool_bounds(self) -> Settings:
        if self.ORACLE_POOL_MAX < self.ORACLE_POOL_MIN:
            raise ValueError("ORACLE_POOL_MAX must be >= ORACLE_POOL_MIN")
        return self

    @model_validator(mode="after")
    def _oracle_needs_credentials(self) -> Settings:
        if self.DB_BACKEND == "oracle":
            missing = [
                name
                for name, val in (
                    ("ORACLE_USER", self.ORACLE_USER),
                    ("ORACLE_PASSWORD", self.ORACLE_PASSWORD.get_secret_value()),
                    ("ORACLE_DSN", self.ORACLE_DSN),
                )
                if not val
            ]
            if missing:
                raise ValueError(
                    "DB_BACKEND=oracle requires: " + ", ".join(missing),
                )
        return self

    @model_validator(mode="after")
    def _live_mode_is_locked_down(self) -> Settings:
        if self.APP_MODE != "live":
            return self
        problems: list[str] = []
        for name, val in (
            ("SOURCE_ORACLE_USER", self.SOURCE_ORACLE_USER),
            ("SOURCE_ORACLE_PASSWORD", self.SOURCE_ORACLE_PASSWORD.get_secret_value()),
            ("SOURCE_ORACLE_DSN", self.SOURCE_ORACLE_DSN),
        ):
            if not val:
                problems.append(f"{name} is required when APP_MODE=live")
        if self.ALLOW_LIVE_WRITES and self.APP_ENV == "prod":
            problems.append(
                "ALLOW_LIVE_WRITES=true with APP_ENV=prod is refused: "
                "live source writes are never allowed",
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _oidc_needs_issuer(self) -> Settings:
        if self.AUTH_MODE == "oidc":
            missing = [
                name
                for name, val in (
                    ("OIDC_ISSUER", self.OIDC_ISSUER),
                    ("OIDC_CLIENT_ID", self.OIDC_CLIENT_ID),
                    ("OIDC_CLIENT_SECRET", self.OIDC_CLIENT_SECRET.get_secret_value()),
                )
                if not val
            ]
            if missing:
                raise ValueError("AUTH_MODE=oidc requires: " + ", ".join(missing))
        return self

    @model_validator(mode="after")
    def _secrets_are_real_outside_local(self) -> Settings:
        if self.APP_ENV in ("local", "dev"):
            return self
        problems: list[str] = []
        for name, secret in (
            ("PSEUDONYM_SALT", self.PSEUDONYM_SALT),
            ("JWT_SECRET", self.JWT_SECRET),
        ):
            raw = secret.get_secret_value()
            if len(raw) < MIN_SECRET_LEN:
                problems.append(f"{name} must be at least {MIN_SECRET_LEN} characters")
            if "dev-only" in raw or "replace-with" in raw or "change-me" in raw:
                problems.append(f"{name} still holds a placeholder value")
        if self.AUTH_MODE == "dev":
            problems.append(f"AUTH_MODE=dev is not allowed when APP_ENV={self.APP_ENV}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _teams_real_send_needs_url(self) -> Settings:
        if not self.TEAMS_DRY_RUN and not self.TEAMS_WORKFLOW_WEBHOOK_URL.get_secret_value():
            raise ValueError(
                "TEAMS_DRY_RUN=false requires TEAMS_WORKFLOW_WEBHOOK_URL to be set",
            )
        return self

    @model_validator(mode="after")
    def _vector_oracle_needs_oracle(self) -> Settings:
        if self.VECTOR_BACKEND == "oracle" and self.DB_BACKEND != "oracle":
            raise ValueError("VECTOR_BACKEND=oracle requires DB_BACKEND=oracle")
        return self

    # --------------------------------------------------------- derived views
    @property
    def display_tz(self) -> ZoneInfo:
        return ZoneInfo(self.DISPLAY_TZ)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_demo(self) -> bool:
        return self.APP_MODE == "demo"

    @property
    def llm_configured(self) -> bool:
        """False means every AI feature must take its deterministic fallback path."""
        return bool(self.LLM_BASE_URL)

    @property
    def embeddings_configured(self) -> bool:
        return bool(self.EMBEDDING_BASE_URL)

    @property
    def source_writes_allowed(self) -> bool:
        """Never true in live mode unless a human deliberately flipped the flag."""
        return self.APP_MODE == "demo" or self.ALLOW_LIVE_WRITES

    @property
    def dev_users(self) -> dict[str, str]:
        """`user:role` pairs for AUTH_MODE=dev. Role names are validated here."""
        users: dict[str, str] = {}
        for pair in (p.strip() for p in self.DEV_USERS.split(",") if p.strip()):
            name, _, role = pair.partition(":")
            role = role or "care_agent"
            canonical = _ROLE_ALIASES.get(role, role)
            if canonical not in ALL_ROLES:
                raise ConfigurationError(
                    f"DEV_USERS entry {pair!r} has unknown role {role!r}; "
                    f"expected one of {', '.join(ALL_ROLES)}",
                )
            users[name] = canonical
        return users

    def redacted(self) -> dict[str, object]:
        """Config dump safe for logs, `/api/v1/meta` and bug reports."""
        out: dict[str, object] = {}
        for name, value in self.model_dump().items():
            out[name] = "***" if isinstance(getattr(self, name, None), SecretStr) else value
        return out


#: Short aliases accepted in DEV_USERS so the demo env file stays readable.
_ROLE_ALIASES: dict[str, str] = {
    "admin": "admin",
    "noc": "noc_engineer",
    "care": "care_agent",
    "analyst": "analyst",
    "manager": "manager",
}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process.

    Raises:
        ConfigurationError: with every offending variable listed, so a single run of
            `make doctor` tells an operator everything they need to fix.
    """
    # `TAZAMA_ENV_FILE=""` means "environment variables only" — what CI and the test
    # suite use so a developer's .env can never change a test result.
    env_file = os.environ.get("TAZAMA_ENV_FILE", ".env") or None
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        lines = [
            "TAZAMA cannot start: the environment contract is not satisfied.",
            "",
        ]
        for err in exc.errors():
            location = ".".join(str(p) for p in err["loc"]) or "(model)"
            lines.append(f"  * {location}: {err['msg']}")
        lines += [
            "",
            "Fix the variables above in your .env (copy .env.example), then run: make doctor",
        ]
        raise ConfigurationError("\n".join(lines)) from exc


def reset_settings_cache() -> None:
    """Test-only hook: drop the cached Settings instance."""
    get_settings.cache_clear()
