"""OpenTelemetry wiring. Tracing is always on in-process; exporting is optional.

With `OTEL_EXPORTER_OTLP_ENDPOINT` empty the SDK still produces spans (so correlation ids
and timings are available to tests and to the trace viewer in the compose `observability`
profile) but nothing leaves the process.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from tazama.core.logging import get_logger, scrub

_log = get_logger(__name__)
_configured = False


def configure_tracing(
    service_name: str,
    *,
    endpoint: str = "",
    environment: str = "local",
) -> None:
    """Install a tracer provider once per process."""
    global _configured
    if _configured:
        return
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
        },
    )
    provider = TracerProvider(resource=resource)
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")),
            )
        except Exception as exc:  # pragma: no cover - exporter is environment specific
            _log.warning("otel_exporter_unavailable", extra={"error": str(exc)})
    trace.set_tracer_provider(provider)
    _configured = True


def get_tracer(name: str = "tazama") -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start a span with scrubbed attributes.

    Span attributes are treated like log fields: secrets and identifiers are redacted
    before they are recorded, because traces are exported off-box.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, _attr(scrub(value)))
        try:
            yield current
        except Exception as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            current.record_exception(exc)
            raise


def _attr(value: Any) -> str | int | float | bool:
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)


def annotate(**attributes: Any) -> None:
    """Add attributes to the current span, if there is one."""
    current = trace.get_current_span()
    if current is None or not current.is_recording():
        return
    for key, value in attributes.items():
        current.set_attribute(key, _attr(scrub(value)))


def current_trace_id() -> str:
    current = trace.get_current_span()
    ctx = current.get_span_context() if current else None
    if ctx is None or not ctx.trace_id:
        return ""
    return format(ctx.trace_id, "032x")


def extract_headers(headers: Mapping[str, str]) -> str:
    """Pull the W3C traceparent trace-id out of inbound headers, if present."""
    traceparent = headers.get("traceparent") or headers.get("Traceparent") or ""
    parts = traceparent.split("-")
    return parts[1] if len(parts) >= 3 and len(parts[1]) == 32 else ""
