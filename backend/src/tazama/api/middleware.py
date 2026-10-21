"""ASGI middleware: correlation ids, security headers, body limits, rate limiting.

Order matters and is set in `api/app.py`: correlation id first (so everything downstream
can log it), then security headers, then limits, then the application.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from tazama.core.errors import PROBLEM_CONTENT_TYPE, PayloadTooLarge, TazamaError
from tazama.core.ids import get_correlation_id, new_correlation_id, set_correlation_id
from tazama.core.logging import get_logger
from tazama.core.security import RateLimiter

_log = get_logger(__name__)

Next = Callable[[Request], Awaitable[Response]]


def problem_response(request: Request, error: TazamaError) -> JSONResponse:
    """Render a problem+json document from middleware.

    Starlette does not route exceptions raised inside `BaseHTTPMiddleware` through the
    application's exception handlers, so middleware renders its own refusals rather than
    raising and turning a 413 into a 500.
    """
    return JSONResponse(
        status_code=error.status,
        content=error.to_problem(instance=request.url.path, correlation_id=get_correlation_id()),
        media_type=PROBLEM_CONTENT_TYPE,
        headers={"X-Correlation-ID": get_correlation_id()},
    )


#: Content-Security-Policy for API responses. The web app ships its own, stricter policy;
#: this one locks down anything served directly by the API (docs, problem+json, SSE).
API_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
    "img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=()",
    "Cache-Control": "no-store",
    "Content-Security-Policy": API_CSP,
}


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Accept or mint a correlation id, expose it on the response, log the request."""

    header = "X-Correlation-ID"

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        inbound = request.headers.get(self.header, "").strip()
        # Never trust an inbound id verbatim: cap it and keep it to safe characters.
        candidate = "".join(c for c in inbound if c.isalnum() or c in "-_")[:64]
        cid = candidate or new_correlation_id()
        set_correlation_id(cid)
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers[self.header] = cid
        _log.info(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Static security headers, plus HSTS when the request arrived over TLS."""

    def __init__(self, app: object, *, enable_hsts: bool = True) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.enable_hsts = enable_hsts

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if self.enable_hsts and (request.url.scheme == "https" or forwarded_proto == "https"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized requests before they are parsed."""

    def __init__(self, app: object, *, max_bytes: int = 1_000_000) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return problem_response(
                request,
                PayloadTooLarge(f"Request body exceeds the {self.max_bytes} byte limit."),
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-caller request limiting, keyed by authenticated subject or client address."""

    exempt_paths = frozenset({"/healthz", "/readyz", "/metrics"})

    def __init__(self, app: object, *, limit: int, window_s: float = 60.0) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = RateLimiter(limit=limit, window_s=window_s)

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        if request.url.path in self.exempt_paths:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        key = auth[-24:] if auth else (request.client.host if request.client else "anonymous")
        try:
            self._limiter.check(key)
        except TazamaError as refusal:
            refused = problem_response(request, refusal)
            refused.headers["Retry-After"] = str(refusal.extra.get("retryAfter", 60))
            return refused
        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Remaining", str(self._limiter.remaining(key)))
        return response
