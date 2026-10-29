"""The insights and reports HTTP surfaces.

Two properties these tests exist to pin down:

* **The window is anchored on the data, not on `now`.** A seeded demo database sits still,
  so a wall-clock window would silently return empty series and make the panels look
  broken. The regression is easy to reintroduce and invisible until a demo.
* **Reports cannot send.** There is no send route, and the preview says so. A test that
  asserts the absence of a route is worth more than one that asserts a 200.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.infrastructure.db.models import Cell, Complaint, Incident, KpiCell, Site

#: Deliberately far in the past relative to the frozen test clock, so a wall-clock window
#: would miss it entirely.
DATA_END = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
HOURS = 14 * 24


@pytest.fixture
def series(db_session):
    """One cell with a fortnight of hourly KPIs and complaints that track them."""
    session = db_session
    session.add(
        Site(
            id="SITE-NRB-001",
            name="Westlands Macro",
            region="Nairobi",
            county="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            site_type="macro",
        ),
    )
    session.flush()
    session.add(
        Cell(
            id="CELL-NRB-001",
            site_id="SITE-NRB-001",
            name="Westlands A",
            tech="4G",
            band="L1800",
            region="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            subscribers_est=2_000,
        ),
    )

    for hour in range(HOURS):
        at = DATA_END - timedelta(hours=HOURS - hour)
        # A daily shape, plus a dip every third day, so there is something to find.
        busy = 1.0 if 16 <= at.hour <= 21 else 0.0
        dip = 1.0 if (at.day % 3 == 0 and 10 <= at.hour <= 14) else 0.0
        session.add(
            KpiCell(
                cell_id="CELL-NRB-001",
                site_id="SITE-NRB-001",
                region="Nairobi",
                bucket_start=at,
                granularity="1h",
                availability_pct=99.9 - 8.0 * dip,
                rrc_setup_success_pct=99.0 - 5.0 * dip,
                erab_setup_success_pct=99.2 - 5.0 * dip,
                drop_rate_pct=0.4 + 2.0 * dip,
                prb_utilisation_pct=35.0 + 40.0 * busy + 15.0 * dip,
                dl_throughput_mbps=22.0 - 10.0 * busy - 6.0 * dip,
                ul_throughput_mbps=8.0 - 3.0 * busy,
                latency_ms=28.0 + 20.0 * busy,
                active_users=180 + int(220 * busy),
                sinr_db=12.0 - 3.0 * dip,
            ),
        )
        # Complaints follow the dip an hour later: a real lead for lead-lag to find.
        prior = at - timedelta(hours=1)
        lagged = 1.0 if (prior.day % 3 == 0 and 10 <= prior.hour <= 14) else 0.0
        for index in range(int(1 + 6 * lagged + 2 * busy)):
            session.add(
                Complaint(
                    id=f"CMP-{hour:04d}-{index}",
                    received_at=at + timedelta(minutes=5 * index),
                    channel="ussd",
                    language="en",
                    raw_text="no service",
                    region="Nairobi",
                    cell_id="CELL-NRB-001",
                    category="no_service",
                    status="received",
                ),
            )
    session.commit()
    return "CELL-NRB-001"


def test_the_window_is_anchored_on_the_latest_data(client, auth_headers, series) -> None:
    body = client.get("/api/v1/insights/window?days=14", headers=auth_headers("analyst")).json()
    assert body["hours"] == 14 * 24
    # Ends at the last whole hour of data, not at the frozen clock's "now".
    assert body["end"].startswith("2026-02-28") or body["end"].startswith("2026-03-01")


def test_insights_with_no_kpi_data_says_so_rather_than_returning_zeros(
    client,
    auth_headers,
    db_session,
) -> None:
    response = client.get("/api/v1/insights/window", headers=auth_headers("analyst"))
    assert response.status_code == 404
    assert "seed" in response.json()["detail"]


def test_the_complaint_rate_is_normalised_and_carries_its_caveats(
    client,
    auth_headers,
    series,
) -> None:
    body = client.get(
        "/api/v1/insights/complaint-rate?days=7",
        headers=auth_headers("analyst"),
    ).json()
    assert body["n_points"] == 7 * 24
    assert body["subscribers_est"] == 2_000
    assert len(body["rate_per_1k"]) == body["n_points"]
    assert len(body["deseasonalised"]) == body["n_points"]
    # The denominator is an estimate, and the response has to say so.
    assert any("subscribers_est" in caveat for caveat in body["caveats"])


def test_lead_lag_reports_the_direction_in_words(client, auth_headers, series) -> None:
    body = client.get(
        f"/api/v1/insights/lead-lag?cell_id={series}&kpi=availability_pct&days=14",
        headers=auth_headers("analyst"),
    ).json()
    assert body["lags"][0] < 0 < body["lags"][-1]
    assert body["reading"]
    # Whatever the number, the prose and the flag must agree.
    if body["best_lag_hours"] is not None and body["best_lag_hours"] > 0:
        assert body["kpi_leads"] is True
        assert "before the complaint rate" in body["reading"]


def test_an_unknown_kpi_is_a_422_not_a_500(client, auth_headers, series) -> None:
    response = client.get(
        f"/api/v1/insights/lead-lag?cell_id={series}&kpi=made_up_kpi",
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 422


def test_correlations_return_a_square_matrix_and_ranked_pairs(
    client,
    auth_headers,
    series,
) -> None:
    body = client.get(
        "/api/v1/insights/correlations?days=14",
        headers=auth_headers("analyst"),
    ).json()
    size = len(body["labels"])
    assert size > 1
    assert len(body["pearson"]) == size
    assert all(len(row) == size for row in body["pearson"])
    assert body["top"]
    # A pair is never correlated with itself.
    assert all(pair["left"] != pair["right"] for pair in body["top"])


def test_drivers_are_ranked_from_one(client, auth_headers, series) -> None:
    body = client.get("/api/v1/insights/drivers?days=14", headers=auth_headers("analyst")).json()
    ranks = [driver["rank"] for driver in body["drivers"]]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1
    scores = [driver["score"] for driver in body["drivers"]]
    assert scores == sorted(scores, reverse=True)


def test_pca_variance_ratios_are_ordered_and_cumulative(client, auth_headers, series) -> None:
    body = client.get("/api/v1/insights/pca?days=14", headers=auth_headers("analyst")).json()
    ratios = body["explained_variance_ratio"]
    assert ratios == sorted(ratios, reverse=True)
    cumulative = body["cumulative_variance"]
    assert cumulative == sorted(cumulative)
    assert cumulative[-1] <= 1.0 + 1e-6


def test_busiest_cells_shortlists_by_complaint_count(client, auth_headers, series) -> None:
    body = client.get(
        "/api/v1/insights/busiest-cells?limit=5",
        headers=auth_headers("analyst"),
    ).json()
    assert body and body[0]["cell_id"] == series
    assert body[0]["complaints"] > 0


@pytest.mark.parametrize(
    ("role", "status"),
    [("analyst", 200), ("manager", 200), ("noc_engineer", 200), ("care_agent", 403)],
)
def test_insights_authorisation(client, auth_headers, series, role, status) -> None:
    response = client.get("/api/v1/insights/window", headers=auth_headers(role))
    assert response.status_code == status


# ----------------------------------------------------------------- reports


@pytest.fixture
def incidents(db_session):
    session = db_session
    for index in range(3):
        session.add(
            Incident(
                id=f"INC-RPT-{index}",
                title=f"Congestion {index}",
                status="detected",
                priority="P2",
                cause="congestion",
                region="Nairobi",
                detected_at=DATA_END - timedelta(hours=index + 1),
                started_at=DATA_END - timedelta(hours=index + 2),
                affected_cells=2,
                affected_sites=1,
                affected_subs=1_000 * (index + 1),
                complaint_count=5,
                created_at=DATA_END,
                updated_at=DATA_END,
                suppressed_reason="planned_maintenance" if index == 2 else None,
            ),
        )
    session.commit()


def test_the_digest_is_built_and_validated_but_not_sent(client, auth_headers, incidents) -> None:
    body = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers("manager")).json()
    assert body["kind"] == "daily"
    assert len(body["incidents"]) == 3
    assert body["card"]["type"] == "AdaptiveCard"
    assert body["card_bytes"] == len(str(body["card"]).replace("'", '"')) or body["card_bytes"] > 0
    assert body["dry_run"] is True
    assert "Nothing has been posted" in body["delivery_note"]


def test_there_is_no_way_to_send_a_report_over_http(client, auth_headers) -> None:
    """The absence of this route is the control. Phase 6's scheduler owns sending."""
    for method, path in (
        ("post", "/api/v1/reports/digest"),
        ("post", "/api/v1/reports/send"),
        ("post", "/api/v1/reports/digest/send"),
    ):
        response = getattr(client, method)(path, headers=auth_headers("admin"), json={})
        assert response.status_code in (404, 405), (path, response.status_code)


def test_the_report_id_is_deterministic_for_a_window(client, auth_headers, incidents) -> None:
    first = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers("manager")).json()
    second = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers("manager")).json()
    assert first["report_id"] == second["report_id"]
    weekly = client.get(
        "/api/v1/reports/digest?kind=weekly",
        headers=auth_headers("manager"),
    ).json()
    assert weekly["report_id"] != first["report_id"]


def test_suppressed_incidents_are_excluded_from_the_live_count(
    client,
    auth_headers,
    incidents,
) -> None:
    body = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers("manager")).json()
    headline = dict(tuple(pair) for pair in body["headline"])
    assert headline["Incidents"] == "2 live, 1 suppressed"


def test_an_unknown_digest_kind_is_refused(client, auth_headers) -> None:
    response = client.get("/api/v1/reports/digest?kind=hourly", headers=auth_headers("manager"))
    assert response.status_code == 422


def test_the_webhook_value_is_never_returned(client, auth_headers, incidents) -> None:
    response = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers("manager"))
    assert "webhook_configured" in response.json()
    assert "http" not in response.text.replace("http://adaptivecards.io", "")


@pytest.mark.parametrize(
    ("role", "status"),
    [("manager", 200), ("analyst", 200), ("noc_engineer", 200), ("care_agent", 403)],
)
def test_reports_authorisation(client, auth_headers, incidents, role, status) -> None:
    response = client.get("/api/v1/reports/digest?kind=daily", headers=auth_headers(role))
    assert response.status_code == status
