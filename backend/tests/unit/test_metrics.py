"""The /metrics exposition.

The failure modes worth testing are not "does it count" but the ones that take down a
metrics backend or make a scrape unparseable: unbounded label cardinality, an unescaped
quote in a label, and a leaked identifier.
"""

from __future__ import annotations

from tazama.api.metrics import BUCKETS, MAX_SERIES, Metrics


def _render(metrics: Metrics) -> str:
    return metrics.render()


def test_a_request_appears_as_a_counter_and_a_histogram() -> None:
    m = Metrics()
    m.request_started()
    m.request_finished(method="GET", route="/api/v1/incidents", status=200, seconds=0.03)

    text = _render(m)
    expected = 'tazama_http_requests_total{method="GET",route="/api/v1/incidents",status="200"} 1'
    assert expected in text
    assert "tazama_http_request_duration_seconds_count" in text
    assert "tazama_http_request_duration_seconds_sum" in text


def test_histogram_buckets_are_cumulative() -> None:
    """A Prometheus histogram is cumulative. Non-cumulative buckets silently produce
    nonsense quantiles rather than an error, so this is worth pinning."""
    m = Metrics()
    for seconds in (0.001, 0.03, 0.4, 20.0):
        m.request_started()
        m.request_finished(method="GET", route="/x", status=200, seconds=seconds)

    lines = [ln for ln in _render(m).splitlines() if "_bucket{" in ln]
    values = [int(ln.rsplit(" ", 1)[1]) for ln in lines]
    assert values == sorted(values), "bucket counts must never decrease"
    assert values[-1] == 4, "the +Inf bucket must hold every observation"
    assert len(lines) == len(BUCKETS) + 1


def test_in_flight_returns_to_zero() -> None:
    m = Metrics()
    m.request_started()
    m.request_finished(method="GET", route="/x", status=200, seconds=0.01)
    assert "tazama_http_requests_in_flight 0" in _render(m)


def test_label_cardinality_is_capped() -> None:
    """The scenario: a scanner walks thousands of distinct URLs.

    Without a ceiling every one mints a new series and the metrics backend, not the API,
    is what falls over.
    """
    m = Metrics()
    for i in range(MAX_SERIES + 500):
        m.request_started()
        m.request_finished(method="GET", route=f"/scan/{i}", status=404, seconds=0.001)

    text = _render(m)
    series = [ln for ln in text.splitlines() if ln.startswith("tazama_http_requests_total{")]
    assert len(series) <= MAX_SERIES + 1
    assert "__overflow__" in text


def test_a_quote_in_a_label_cannot_break_the_scrape() -> None:
    """An unescaped quote makes the *entire* scrape unparseable, not just one line."""
    m = Metrics()
    m.request_started()
    m.request_finished(method='GET"evil', route='/a"b', status=200, seconds=0.01)

    for line in _render(m).splitlines():
        if line.startswith("#") or "{" not in line:
            continue
        inside = line[line.index("{") + 1 : line.rindex("}")]
        # Every quote inside the label block must be escaped or a delimiter.
        assert '\\"' in inside or inside.count('"') % 2 == 0


def test_counters_and_gauges_render_with_their_type() -> None:
    m = Metrics()
    m.incr("tazama_llm_calls_total", task="diagnose")
    m.incr("tazama_llm_calls_total", task="diagnose")
    m.gauge("tazama_outbox_pending", 7.0)

    text = _render(m)
    assert "# TYPE tazama_llm_calls_total counter" in text
    assert 'tazama_llm_calls_total{task="diagnose"} 2' in text
    assert "# TYPE tazama_outbox_pending gauge" in text
    assert "tazama_outbox_pending 7.0" in text


def test_the_type_header_is_emitted_once_per_metric() -> None:
    """A repeated TYPE line for the same metric is a parse error in some scrapers."""
    m = Metrics()
    m.incr("tazama_thing_total", a="1")
    m.incr("tazama_thing_total", a="2")
    assert _render(m).count("# TYPE tazama_thing_total counter") == 1
