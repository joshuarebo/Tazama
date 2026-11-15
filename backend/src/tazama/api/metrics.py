"""Prometheus metrics, served at `/metrics`.

Standard 4.4 of the AI standards checklist asks for operational metrics with p50, p95 and
p99, and 4.5 asks for Grafana dashboards from day one. A dashboard needs a scrape target,
so this is that target.

Three decisions worth stating:

* **No client library.** The exposition format is a handful of lines of text and the
  dependency would be one more thing to audit, pin and scan. The format is stable and
  documented, and rendering it here keeps the behaviour visible.
* **Bounded label cardinality.** The route *template* is the label, never the resolved
  path. `/api/v1/incidents/{incident_id}` is one series; the resolved path would be one
  series per incident, which is how a metrics backend is accidentally taken down
  (standard: metrics cardinality). Unmatched paths collapse to `__unmatched__`.
* **Histograms, not averages.** An average latency hides exactly the tail that matters
  during an incident. The buckets below are fixed so p95 and p99 are comparable across
  deploys.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

#: Latency buckets in seconds. Chosen around the shapes this API actually has: sub-50ms
#: reads, a few hundred ms for correlated queries, and seconds only for an agent run.
BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

#: HTTP methods are attacker-controlled: a client may send any token as the method and the
#: framework passes it through. Capping the *route* label alone therefore does not cap the
#: series count, because `(method, "__overflow__", status)` stays unbounded through the
#: method. Anything outside this list collapses to "OTHER".
ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)

#: Every label on an overflowed series is constant, so overflow can add at most one series
#: per status code rather than one per distinct input.
OVERFLOW = "__overflow__"

#: A hard ceiling on distinct label combinations. Past this, new series are folded into
#: `__overflow__` rather than growing without bound: losing detail is recoverable, and a
#: metrics backend falling over during an incident is not.
MAX_SERIES = 2_000


@dataclass(slots=True)
class _Histogram:
    counts: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS) + 1))
    total: float = 0.0
    n: int = 0

    def observe(self, seconds: float) -> None:
        self.total += seconds
        self.n += 1
        for index, edge in enumerate(BUCKETS):
            if seconds <= edge:
                self.counts[index] += 1
                return
        self.counts[-1] += 1


class Metrics:
    """Process-local metric state.

    Each replica exposes its own numbers and Prometheus aggregates across them, which is
    the normal arrangement and the reason no shared store is needed here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, str, int], int] = defaultdict(int)
        self._latency: dict[tuple[str, str], _Histogram] = defaultdict(_Histogram)
        self._in_flight = 0
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._started = time.time()

    # ------------------------------------------------------------ requests

    def request_started(self) -> None:
        with self._lock:
            self._in_flight += 1

    def request_finished(
        self,
        *,
        method: str,
        route: str,
        status: int,
        seconds: float,
    ) -> None:
        safe_method = method.upper() if method.upper() in ALLOWED_METHODS else "OTHER"
        with self._lock:
            self._in_flight -= 1

            key = (safe_method, route, status)
            if key in self._requests or len(self._requests) < MAX_SERIES:
                self._requests[key] += 1
            else:
                # Every label constant, so overflow adds one series per status at most.
                self._requests[(OVERFLOW, OVERFLOW, status)] += 1

            latency_key = (safe_method, route)
            if latency_key not in self._latency and len(self._latency) >= MAX_SERIES:
                latency_key = (OVERFLOW, OVERFLOW)
            self._latency[latency_key].observe(seconds)

    # -------------------------------------------------------- app counters

    def incr(self, name: str, /, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            if len(self._counters) < MAX_SERIES or key in self._counters:
                self._counters[key] += 1

    def gauge(self, name: str, value: float, /, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            if len(self._gauges) < MAX_SERIES or key in self._gauges:
                self._gauges[key] = value

    # ---------------------------------------------------------- exposition

    def render(self) -> str:
        with self._lock:
            requests = dict(self._requests)
            latency = {k: (v.counts[:], v.total, v.n) for k, v in self._latency.items()}
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            in_flight = self._in_flight
            uptime = time.time() - self._started

        out: list[str] = []

        out += [
            "# HELP tazama_build_info Static build information.",
            "# TYPE tazama_build_info gauge",
            f'tazama_build_info{{version="{_esc(_version())}"}} 1',
            "# HELP tazama_uptime_seconds Seconds since this process started.",
            "# TYPE tazama_uptime_seconds gauge",
            f"tazama_uptime_seconds {uptime:.1f}",
            "# HELP tazama_http_requests_in_flight Requests currently being served.",
            "# TYPE tazama_http_requests_in_flight gauge",
            f"tazama_http_requests_in_flight {in_flight}",
        ]

        out += [
            "# HELP tazama_http_requests_total HTTP requests by method, route and status.",
            "# TYPE tazama_http_requests_total counter",
        ]
        for (method, route, status), count in sorted(requests.items()):
            out.append(
                f'tazama_http_requests_total{{method="{_esc(method)}",'
                f'route="{_esc(route)}",status="{status}"}} {count}'
            )

        out += [
            "# HELP tazama_http_request_duration_seconds Request latency.",
            "# TYPE tazama_http_request_duration_seconds histogram",
        ]
        for (method, route), (counts, total, n) in sorted(latency.items()):
            labels = f'method="{_esc(method)}",route="{_esc(route)}"'
            cumulative = 0
            for index, edge in enumerate(BUCKETS):
                cumulative += counts[index]
                out.append(
                    f"tazama_http_request_duration_seconds_bucket{{{labels},"
                    f'le="{edge}"}} {cumulative}'
                )
            cumulative += counts[-1]
            out.append(
                f'tazama_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {cumulative}'
            )
            out.append(f"tazama_http_request_duration_seconds_sum{{{labels}}} {total:.6f}")
            out.append(f"tazama_http_request_duration_seconds_count{{{labels}}} {n}")

        seen: set[str] = set()
        for (counter_name, counter_labels), count in sorted(counters.items()):
            if counter_name not in seen:
                out.append(f"# TYPE {counter_name} counter")
                seen.add(counter_name)
            out.append(f"{counter_name}{_labels(counter_labels)} {count}")

        seen = set()
        for (gauge_name, gauge_labels), reading in sorted(gauges.items()):
            if gauge_name not in seen:
                out.append(f"# TYPE {gauge_name} gauge")
                seen.add(gauge_name)
            out.append(f"{gauge_name}{_labels(gauge_labels)} {reading}")

        return "\n".join(out) + "\n"


def _labels(pairs: tuple[tuple[str, str], ...]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in pairs)
    return "{" + inner + "}"


def _esc(value: str) -> str:
    """Escape a label value. An unescaped quote makes the whole scrape unparseable."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _version() -> str:
    from tazama import __version__

    return __version__


#: The process-wide instance. The middleware writes to it; `/metrics` reads it.
METRICS = Metrics()
