"""Care lookup: identity handling, authorisation, and what the agent is told.

The point of these tests is not that the endpoint returns 200. It is that the raw MSISDN
never appears in the response, that the "known issue" claim is only made when the cell
actually matches, and that a care agent cannot reach anything else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.infrastructure.db.models import (
    CareCall,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    Site,
    Subscriber,
)

T0 = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)
#: A seeded number, in the shape a USSD channel produces.
MSISDN = "0712345678"


@pytest.fixture
def seeded(db_session, pseudonymiser):
    """One subscriber on a cell with a live incident, and a neighbour with none."""
    from tazama.domain.value_objects import Msisdn

    session = db_session
    token = pseudonymiser.token(Msisdn.parse(MSISDN).value)

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
    for cell_id in ("CELL-NRB-001", "CELL-NRB-002"):
        session.add(
            Cell(
                id=cell_id,
                site_id="SITE-NRB-001",
                name=f"Westlands {cell_id[-1]}",
                tech="4G",
                band="L1800",
                region="Nairobi",
                lat=-1.2653,
                lon=36.8039,
                subscribers_est=1200,
            ),
        )
    session.add(
        Subscriber(
            id=token,
            region="Nairobi",
            home_cell_id="CELL-NRB-001",
            last_cell_id="CELL-NRB-001",
            last_seen_at=T0,
            segment="consumer",
            msisdn_format="07XXXXXXXX",
        ),
    )
    session.add(
        Subscriber(
            id="SUB-QUIETQUIETQUIETQ",
            region="Nairobi",
            home_cell_id="CELL-NRB-002",
            last_cell_id="CELL-NRB-002",
            last_seen_at=T0,
            segment="consumer",
        ),
    )
    session.add(
        Complaint(
            id="CMP-CARE-1",
            received_at=T0 - timedelta(hours=2),
            channel="ussd",
            language="sw",
            raw_text="Hakuna network kabisa tangu asubuhi",
            subscriber_id=token,
            region="Nairobi",
            cell_id="CELL-NRB-001",
            category="no_service",
            status="received",
        ),
    )
    session.add(
        CareCall(
            id="CALL-CARE-1",
            started_at=T0 - timedelta(hours=1),
            duration_s=185,
            subscriber_id=token,
            reason_code="no_service",
            reason_text="Customer reports no service",
            region="Nairobi",
            cell_id="CELL-NRB-001",
        ),
    )
    session.add(
        Incident(
            id="INC-CARE-1",
            title="No service on Westlands 1",
            status="detected",
            priority="P2",
            cause="cell_outage",
            region="Nairobi",
            detected_at=T0 - timedelta(hours=3),
            started_at=T0 - timedelta(hours=3),
            affected_cells=1,
            affected_sites=1,
            affected_subs=1200,
            complaint_count=1,
            created_at=T0,
            updated_at=T0,
        ),
    )
    session.flush()
    session.add(
        IncidentLink(
            incident_id="INC-CARE-1",
            entity_type="cell",
            entity_id="CELL-NRB-001",
            linked_at=T0,
        ),
    )
    session.commit()
    return token


def test_an_msisdn_is_pseudonymised_and_never_echoed(client, auth_headers, seeded) -> None:
    response = client.post(
        "/api/v1/care/lookup",
        json={"identifier": MSISDN},
        headers=auth_headers("care_agent"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["subscriber"]["token"] == seeded

    # The number must not appear anywhere in the payload, in any shape.
    raw = response.text
    for shape in (MSISDN, "254712345678", "+254712345678", "712345678"):
        assert shape not in raw


def test_the_token_path_finds_the_same_subscriber(client, auth_headers, seeded) -> None:
    by_number = client.post(
        "/api/v1/care/lookup",
        json={"identifier": MSISDN},
        headers=auth_headers("care_agent"),
    ).json()
    by_token = client.post(
        "/api/v1/care/lookup",
        json={"identifier": seeded},
        headers=auth_headers("care_agent"),
    ).json()
    assert by_number["subscriber"] == by_token["subscriber"]


def test_a_live_incident_on_the_serving_cell_is_reported_with_approved_wording(
    client,
    auth_headers,
    seeded,
) -> None:
    body = client.post(
        "/api/v1/care/lookup",
        json={"identifier": seeded},
        headers=auth_headers("care_agent"),
    ).json()
    issue = body["known_issue"]
    assert issue is not None
    assert issue["incident_id"] == "INC-CARE-1"
    assert "cell outage" in issue["say_to_customer"]
    # The agent is given a line to read, and it promises nothing about timing.
    assert "minutes" not in issue["say_to_customer"]
    assert issue["notification_sent"] is False
    assert body["complaints"] and body["care_calls"]


def test_a_region_match_is_flagged_as_weaker_than_a_cell_match(
    client,
    auth_headers,
    seeded,
) -> None:
    """The quiet neighbour must not be told the outage is theirs."""
    body = client.post(
        "/api/v1/care/lookup",
        json={"identifier": "SUB-QUIETQUIETQUIETQ"},
        headers=auth_headers("care_agent"),
    ).json()
    assert body["found"] is True
    notes = " ".join(body["notes"])
    assert "No live incident on this subscriber's serving cell" in notes
    assert "may not affect this customer" in notes


def test_an_unknown_subscriber_says_so_without_leaking_existence(
    client,
    auth_headers,
    seeded,
) -> None:
    body = client.post(
        "/api/v1/care/lookup",
        json={"identifier": "0799999999"},
        headers=auth_headers("care_agent"),
    ).json()
    assert body["found"] is False
    assert body["subscriber"] is None
    assert body["known_issue"] is None


def test_a_malformed_identifier_is_refused_with_a_useful_message(
    client,
    auth_headers,
) -> None:
    response = client.post(
        "/api/v1/care/lookup",
        json={"identifier": "not-a-number"},
        headers=auth_headers("care_agent"),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "MSISDN" in detail
    # The rejected input is not echoed back into the error.
    assert "not-a-number" not in detail


@pytest.mark.parametrize(
    ("role", "status"),
    [("care_agent", 200), ("admin", 200), ("noc_engineer", 403), ("analyst", 403)],
)
def test_lookup_is_restricted_to_care_roles(client, auth_headers, seeded, role, status) -> None:
    response = client.post(
        "/api/v1/care/lookup",
        json={"identifier": seeded},
        headers=auth_headers(role),
    )
    assert response.status_code == status


def test_lookup_requires_authentication(client, seeded) -> None:
    assert client.post("/api/v1/care/lookup", json={"identifier": seeded}).status_code == 401


def test_samples_only_returns_subscribers_with_complaints(client, auth_headers, seeded) -> None:
    response = client.get("/api/v1/care/samples?limit=5", headers=auth_headers("care_agent"))
    assert response.status_code == 200
    assert response.json() == [seeded]
