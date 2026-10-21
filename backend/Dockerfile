# TAZAMA backend image — multi-stage, non-root, no secrets baked in.
# Stage 1 resolves dependencies with uv; stage 2 carries only the runtime.

FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app
# Dependency layer first so application edits do not re-resolve the world.
COPY pyproject.toml uv.lock* /app/
RUN uv sync --no-install-project --extra ml || uv sync --no-install-project
COPY src /app/src
RUN uv sync --extra ml || uv sync


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=UTC

# libaio is what python-oracledb needs if it is ever switched out of thin mode.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libaio1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tazama \
    && useradd --uid 10001 --gid tazama --create-home --shell /usr/sbin/nologin tazama

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=tazama:tazama src /app/src
COPY --chown=tazama:tazama alembic.ini /app/alembic.ini
COPY --chown=tazama:tazama alembic /app/alembic

USER tazama
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "tazama.api.app:asgi", "--factory", "--host", "0.0.0.0", "--port", "8000"]
