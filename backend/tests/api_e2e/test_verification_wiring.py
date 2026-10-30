"""Verification against the real adapters.

This file exists because of a bug the type checker found and no test did: the composition
root wired `SqlKpiReader` straight into verification's `KpiReader` port, and the port is
keyword-only with different parameter names, so the first real call raised `TypeError`. The
module had unit tests with fakes and an HTTP surface, and still had never once run against
the database.

So these tests go through `build_services` — the real composition root, the real
repositories — and assert the thing the unit tests structurally could not: that the ports
and the adapters fit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from tazama.infrastructure.db.models import (
    Alarm,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    KpiCell,
    Site,
)

#: The frozen test clock. Anchoring here matters: verification reads the *last two hours*
#: as "current", so a fixture whose recovery happens after `now` is a fixture that has not
#: recovered — and the service is right to say so.
NOW = datetime(2026, 3, 17, 11, 30, tzinfo=UTC)

#: When the fault started and when it cleared, both comfortably before `now` so the
#: current window sees healthy data.
BROKE_AT = NOW - timedelta(hours=9)
CLEARED_AT = NOW - timedelta(hours=4)


@pytest.fixture
def recovered_incident(db_session):
    """A cell whose availability collapsed and then came back, with the alarm cleared."""
    session = db_session
    session.add(
        Site(
            id="SITE-VER-1",
            name="Verification Site",
            region="Nairobi",
            county="Nairobi",
            lat=-1.26,
            lon=36.80,
            site_type="macro",
        ),
    )
    session.flush()
    session.add(
        Cell(
            id="CELL-VER-1",
            site_id="SITE-VER-1",
            name="Verification A",
            tech="4G",
            band="L1800",
            region="Nairobi",
            lat=-1.26,
            lon=36.80,
            subscribers_est=2_000,
        ),
    )

    # Healthy for a day, broken for five hours, healthy again for the last four.
    for hour in range(-36, 1):
        at = NOW + timedelta(hours=hour)
        broken = BROKE_AT <= at < CLEARED_AT
        session.add(
            KpiCell(
                cell_id="CELL-VER-1",
                site_id="SITE-VER-1",
                region="Nairobi",
                bucket_start=at,
                granularity="1h",
                availability_pct=5.0 if broken else 99.9,
                rrc_setup_success_pct=40.0 if broken else 99.4,
                erab_setup_success_pct=99.3,
                drop_rate_pct=18.0 if broken else 0.4,
                prb_utilisation_pct=35.0,
                dl_throughput_mbps=22.0,
                ul_throughput_mbps=8.0,
                latency_ms=30.0,
                active_users=180.0,
                sinr_db=12.0,
            ),
        )

    session.add(
        Alarm(
            id="ALM-VER-1",
            node_id="CELL-VER-1",
            node_kind="cell",
            alarm_type="cell_unavailable",
            severity="critical",
            raised_at=BROKE_AT,
            cleared_at=CLEARED_AT,
            cell_id="CELL-VER-1",
            site_id="SITE-VER-1",
        ),
    )
    session.add(
        Incident(
            id="INC-VER-1",
            title="Availability collapse on Verification A",
            status="verifying",
            priority="P2",
            cause="cell_outage",
            region="Nairobi",
            detected_at=BROKE_AT,
            started_at=BROKE_AT,
            affected_cells=1,
            affected_sites=1,
            affected_subs=2_000,
            complaint_count=4,
            created_at=BROKE_AT,
            updated_at=NOW,
        ),
    )
    session.flush()
    session.add(
        IncidentLink(
            incident_id="INC-VER-1",
            entity_type="cell",
            entity_id="CELL-VER-1",
            linked_at=BROKE_AT,
        ),
    )
    session.commit()
    return "INC-VER-1"


@pytest.fixture
def services(db_session, settings, frozen_clock):
    from tazama.composition import build_services

    return build_services(db_session, settings=settings, clock=frozen_clock)


def test_the_verification_ports_fit_the_real_adapters(services, recovered_incident) -> None:
    """The regression. A keyword call into a positional adapter raised `TypeError`."""
    from tazama.domain.value_objects import KpiName, TimeWindow

    kpis = services.verification._kpis
    window = TimeWindow(start=BROKE_AT - timedelta(hours=24), end=BROKE_AT)
    value = kpis.baseline(
        element_id="CELL-VER-1",
        kpi=str(KpiName.AVAILABILITY),
        window=window,
    )
    assert value is not None
    assert value > 95.0, "the pre-incident baseline should be healthy"

    recent = kpis.current(
        element_id="CELL-VER-1",
        kpi=str(KpiName.AVAILABILITY),
        window=TimeWindow(start=BROKE_AT, end=CLEARED_AT),
    )
    assert recent is not None
    assert recent < 50.0, "the incident window should show the collapse"


def test_a_cleared_alarm_is_not_reported_as_open(services, recovered_incident) -> None:
    alarms = services.verification._alarms
    assert alarms.open_alarms(element_ids=["CELL-VER-1"], at=NOW) == ()
    # And while it was raised, it is reported.
    still_open = alarms.open_alarms(
        element_ids=["CELL-VER-1"],
        at=CLEARED_AT - timedelta(hours=1),
    )
    assert "ALM-VER-1" in still_open


def test_complaints_are_counted_since_a_point(services, recovered_incident, db_session) -> None:
    counter = services.verification._complaints
    assert counter.count_since(element_ids=["CELL-VER-1"], since=NOW) == 0

    db_session.add(
        Complaint(
            id="CMP-VER-1",
            received_at=NOW + timedelta(minutes=30),
            channel="ussd",
            language="en",
            raw_text="still no service",
            region="Nairobi",
            cell_id="CELL-VER-1",
            site_id="SITE-VER-1",
            category="no_service",
            status="received",
        ),
    )
    db_session.commit()
    assert counter.count_since(element_ids=["CELL-VER-1"], since=NOW) == 1
    assert counter.count_since(element_ids=[], since=NOW) == 0


def test_an_empty_element_list_is_not_an_error(services, recovered_incident) -> None:
    """Verification runs on incidents that may have lost their links. It must not crash."""
    alarms = services.verification._alarms
    assert alarms.open_alarms(element_ids=[], at=NOW) == ()


def test_verification_runs_end_to_end_through_the_real_services(
    services,
    recovered_incident,
    db_session,
) -> None:
    """The whole point: this call used to raise before it reached any logic."""
    result = services.verification.verify(recovered_incident)

    assert result.incident_id == recovered_incident
    assert result.checks, "a verdict must name the checks it ran"
    # The fixture recovered: availability back to 99.9, drop rate back to 0.4, alarm
    # cleared four hours ago, no complaints since. Verification should agree.
    assert result.passed is True, result.reason
    assert result.reopened is False
    assert result.open_alarms == ()
    assert result.complaints_after == 0
    assert result.kpi_details, "the KPI comparison should be on the record"

    rows = db_session.execute(select(Incident)).scalars().all()
    assert rows, "the incident is still there"


def test_a_fault_that_has_not_cleared_is_not_verified_as_recovered(
    services,
    recovered_incident,
    db_session,
) -> None:
    """Fail closed. A still-raised alarm means the fault is still there."""
    db_session.execute(
        Alarm.__table__.update().where(Alarm.id == "ALM-VER-1").values(cleared_at=None),
    )
    db_session.commit()

    result = services.verification.verify(recovered_incident)
    assert result.passed is False
    assert "ALM-VER-1" in result.open_alarms
