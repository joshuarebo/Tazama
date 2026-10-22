"""FastAPI application factory.

Startup order: settings (fail fast) -> logging -> tracing -> middleware -> routers.
A configuration problem stops the process with one readable error, by design.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from tazama import __version__
from tazama.api.middleware import (
    BodyLimitMiddleware,
    CorrelationIdMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from tazama.api.v1 import router as v1_router
from tazama.core.errors import PROBLEM_CONTENT_TYPE, TazamaError, ValidationFailed
from tazama.core.ids import get_correlation_id
from tazama.core.logging import configure_logging, get_logger
from tazama.core.settings import Settings, get_settings
from tazama.core.tracing import configure_tracing

_log = get_logger(__name__)

DESCRIPTION = """
TAZAMA correlates customer complaints and care calls with network KPIs and alarms,
detects and explains incidents, and proposes evidence-backed, human-approved actions.

**Synthetic data only.** AI recommends, rules decide, humans approve.
"""


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _log.info(
        "api_start",
        extra={
            "app_mode": settings.APP_MODE,
            "app_env": settings.APP_ENV,
            "db_backend": settings.DB_BACKEND,
            "vector_backend": settings.VECTOR_BACKEND,
            "llm_configured": settings.llm_configured,
            "auth_mode": settings.AUTH_MODE,
            "version": __version__,
        },
    )

    # Tail the outbox so connected browsers see live updates. Read-only and best-effort:
    # if it fails, the UI is merely not live, and a refresh still shows the truth.
    tail: asyncio.Task[object] | None = None
    if app.state.stream_tail_enabled:
        from tazama.api.stream_feed import run_tail

        tail = asyncio.create_task(run_tail(settings), name="sse-outbox-tail")

    try:
        yield
    finally:
        if tail is not None:
            tail.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tail
        _log.info("api_stop")


def create_app(settings: Settings | None = None, *, stream_tail: bool = True) -> FastAPI:
    """Build the application.

    `stream_tail=False` skips the background outbox tail — the test client would otherwise
    start a poller for every app it constructs.
    """
    settings = settings or get_settings()
    configure_logging(
        level=settings.LOG_LEVEL,
        service=settings.OTEL_SERVICE_NAME,
        mode=settings.APP_MODE,
    )
    configure_tracing(
        settings.OTEL_SERVICE_NAME,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        environment=settings.APP_ENV,
    )

    app = FastAPI(
        title="TAZAMA API",
        version=__version__,
        description=DESCRIPTION,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.stream_tail_enabled = stream_tail

    # Innermost listed last: correlation id wraps everything.
    app.add_middleware(RateLimitMiddleware, limit=settings.RATE_LIMIT_PER_MIN)
    app.add_middleware(BodyLimitMiddleware, max_bytes=1_000_000)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Correlation-ID", "Idempotency-Key"],
        expose_headers=["X-Correlation-ID", "X-RateLimit-Remaining"],
        max_age=600,
    )

    _install_error_handlers(app)
    app.include_router(v1_router)
    return app


def _problem_response(
    request: Request,
    error: TazamaError,
) -> JSONResponse:
    problem = error.to_problem(
        instance=str(request.url.path),
        correlation_id=get_correlation_id(),
    )
    return JSONResponse(
        status_code=error.status,
        content=problem,
        media_type=PROBLEM_CONTENT_TYPE,
        headers={"X-Correlation-ID": get_correlation_id()},
    )


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(TazamaError)
    async def _tazama_error(request: Request, exc: Exception) -> JSONResponse:
        error = exc if isinstance(exc, TazamaError) else TazamaError(str(exc))
        if error.status >= 500:
            _log.error("api_error", extra={"code": error.code, "detail": error.detail})
        else:
            _log.info("api_refused", extra={"code": error.code, "detail": error.detail})
        return _problem_response(request, error)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
        errors = exc.errors() if isinstance(exc, RequestValidationError) else []
        fields = [
            {
                "field": ".".join(str(p) for p in err.get("loc", ())[1:]) or "body",
                "message": str(err.get("msg", "invalid")),
            }
            for err in errors[:25]
        ]
        return _problem_response(
            request,
            ValidationFailed("One or more fields are invalid.", errors=fields),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: Exception) -> JSONResponse:
        status = getattr(exc, "status_code", 500)
        detail = str(getattr(exc, "detail", "Request failed."))
        error = TazamaError(detail)
        error.status = status
        error.code = f"http-{status}"
        error.title = detail if status < 500 else "Request failed"
        return _problem_response(request, error)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        # Never leak internals: log the detail, return a generic problem document.
        _log.exception("unhandled_error", extra={"path": request.url.path})
        return _problem_response(
            request,
            TazamaError("An unexpected error occurred. The correlation id is in the response."),
        )


app = None  # populated by the ASGI entrypoint below


def asgi() -> FastAPI:
    """Uvicorn entrypoint: `uvicorn tazama.api.app:asgi --factory`."""
    return create_app()
