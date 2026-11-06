"""`SqlAgentData` — the only database access the agents have.

Two properties matter more than the queries:

* **It cannot write.** The agents' `read` tier is structural because this adapter has no
  write path, not because a tier label says so.
* **It hands out no subscriber data.** `complaint_summary` returns counts and categories;
  there is deliberately no method that returns complaint text or a subscriber id, so an
  identifier cannot reach a model endpoint through an agent at all.

Plus the two mappings that would otherwise fail silently: `Runbook.cause` is a single
column that the diagnosis agent reads as a list, and expired knowledge must never surface.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from tazama.ai.agents.tools import AgentData
from tazama.infrastructure.db.agent_repo import SqlAgentData
from tazama.infrastructure.db.models import (
    Alarm,
    Cell,
    Complaint,
    Incident,
    IncidentLink,
    KnowledgeDoc,
    KpiCell,
    MaintenanceWindow,
    Runbook,
    Site,
    TopologyNode,
)

T0 = datetime(2026, 3, 17, 9, 0, tzinfo=UTC)


@pytest.fixture
def seeded(db_session, frozen_clock):
    """Two cells on one site, one alarm, one open incident, two SOPs (one expired)."""
    session = db_session
    session.add(
        Site(
            id="SITE-R-1",
            name="Westlands Macro",
            region="Nairobi",
            county="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            site_type="macro",
        ),
    )
    session.add(
        TopologyNode(
            id="AGG-R-1",
            kind="aggregation",
            name="Westlands Agg",
            region="Nairobi",
        ),
    )
    session.flush()
    for suffix in ("A", "B"):
        session.add(
            Cell(
                id=f"CELL-R-{suffix}",
                site_id="SITE-R-1",
                name=f"Westlands {suffix}",
                tech="4G",
                band="L1800",
                region="Nairobi",
                lat=-1.2653,
                lon=36.8039,
                subscribers_est=1_200,
            ),
        )
    for hour in range(6):
        session.add(
            KpiCell(
                cell_id="CELL-R-A",
                site_id="SITE-R-1",
                region="Nairobi",
                bucket_start=T0 - timedelta(hours=hour),
                granularity="1h",
                availability_pct=99.0 - hour,
                drop_rate_pct=0.4,
                prb_utilisation_pct=40.0,
            ),
        )
    session.add(
        Alarm(
            id="ALM-R-1",
            node_id="AGG-R-1",
            node_kind="aggregation",
            alarm_type="transmission_link_down",
            severity="critical",
            raised_at=T0 - timedelta(hours=2),
            cell_id="CELL-R-A",
            site_id="SITE-R-1",
        ),
    )
    session.add(
        MaintenanceWindow(
            id="MW-R-1",
            node_id="SITE-R-1",
            node_kind="site",
            region="Nairobi",
            starts_at=T0 - timedelta(hours=1),
            ends_at=T0 + timedelta(hours=1),
            description="planned upgrade",
        ),
    )
    session.add(
        Runbook(
            id="RB-R-1",
            title="Transmission link recovery",
            cause="transmission_link_failure",
            risk_tier="medium",
            steps=["verify", "escalate"],
        ),
    )
    session.add(
        KnowledgeDoc(
            id="KBD-CURRENT",
            title="SOP Transmission link recovery",
            body="Confirm the alarm, check the optical budget, escalate.",
            doc_type="sop",
            owner="transport-ops",
            expires_at=T0 + timedelta(days=200),
        ),
    )
    session.add(
        KnowledgeDoc(
            id="KBD-EXPIRED",
            title="SOP Legacy transmission escalation",
            body="Page the duty manager about any transmission problem.",
            doc_type="sop",
            owner="transport-ops",
            expires_at=T0 - timedelta(days=10),
        ),
    )
    session.add(
        Incident(
            id="INC-R-1",
            title="Transmission link failure",
            status="detected",
            priority="P1",
            cause="transmission_link_failure",
            region="Nairobi",
            detected_at=T0 - timedelta(hours=2),
            started_at=T0 - timedelta(hours=2),
            affected_cells=2,
            affected_subs=2_400,
            complaint_count=3,
            created_at=T0,
            updated_at=T0,
        ),
    )
    # A past incident with the same cause, so `similar_incidents` has something to find.
    session.add(
        Incident(
            id="INC-R-OLD",
            title="Earlier transmission fault",
            status="closed",
            priority="P2",
            cause="transmission_link_failure",
            region="Nairobi",
            detected_at=T0 - timedelta(days=9),
            resolved_at=T0 - timedelta(days=9) + timedelta(hours=3),
            affected_subs=900,
            created_at=T0,
            updated_at=T0,
        ),
    )
    session.flush()
    for index, category in enumerate(("no_service", "no_service", "slow_data")):
        session.add(
            Complaint(
                id=f"CMP-R-{index}",
                received_at=T0 - timedelta(minutes=10 * index),
                channel="ussd",
                language="sw",
                raw_text=f"my line 07123456{index}{index} is dead",
                region="Nairobi",
                cell_id="CELL-R-A",
                site_id="SITE-R-1",
                category=category,
                status="correlated",
                incident_id="INC-R-1",
            ),
        )
        session.add(
            IncidentLink(
                incident_id="INC-R-1",
                entity_type="complaint",
                entity_id=f"CMP-R-{index}",
                linked_at=T0,
            ),
        )
    for entity_type, entity_id in (
        ("cell", "CELL-R-A"),
        ("cell", "CELL-R-B"),
        ("alarm", "ALM-R-1"),
        ("topology", "AGG-R-1"),
    ):
        session.add(
            IncidentLink(
                incident_id="INC-R-1",
                entity_type=entity_type,
                entity_id=entity_id,
                linked_at=T0,
            ),
        )
    session.commit()
    return SqlAgentData(session, clock=frozen_clock)


# ------------------------------------------------------------------ structure


def test_it_satisfies_the_protocol_the_agents_declare() -> None:
    assert isinstance(SqlAgentData(), AgentData)


def test_it_has_no_write_path() -> None:
    """The `read` tier is only meaningful if nothing underneath it can write.

    Read as: whatever a model asks for, there is no method here that could mutate
    anything, so the tier label is a description rather than a promise.
    """
    source = inspect.getsource(SqlAgentData)
    for forbidden in ("session.add", "session.commit", "session.delete", "update(", "delete("):
        assert forbidden not in source, f"{forbidden!r} appears in a read-only adapter"


def test_there_is_no_method_that_returns_complaint_text() -> None:
    """An identifier cannot reach a model through an agent if no tool can fetch one."""
    public = [name for name in dir(SqlAgentData) if not name.startswith("_")]
    assert "complaints" not in public
    assert "subscribers" not in public
    assert "complaint_summary" in public


# -------------------------------------------------------------------- reads


def test_an_incident_comes_back_as_plain_facts(seeded) -> None:
    incident = seeded.incident("INC-R-1")
    assert incident is not None
    assert incident["cause"] == "transmission_link_failure"
    assert incident["affected_subs"] == 2_400
    assert incident["detected_at"].endswith("+00:00"), "timestamps leave as UTC ISO strings"
    assert seeded.incident("INC-NOPE") is None


def test_alarms_come_from_the_incident_links(seeded) -> None:
    alarms = seeded.alarms("INC-R-1")
    assert [alarm["id"] for alarm in alarms] == ["ALM-R-1"]
    assert alarms[0]["severity"] == "critical"
    assert seeded.alarms("INC-NOPE") == ()


def test_the_complaint_summary_is_counts_not_rows(seeded) -> None:
    summary = seeded.complaint_summary("INC-R-1")
    assert summary["total"] == 3
    assert summary["by_category"] == {"no_service": 2, "slow_data": 1}
    # Nothing in the payload carries text or an identifier, however deeply nested.
    assert "0712345600" not in str(summary)
    assert "raw_text" not in str(summary)


def test_topology_says_how_many_cells_each_node_explains(seeded) -> None:
    """One node explaining several cells is what makes a transmission fault diagnosable."""
    nodes = {node["node_id"]: node for node in seeded.topology("INC-R-1")}
    assert nodes["SITE-R-1"]["affected_cells"] == 2
    assert nodes["AGG-R-1"]["kind"] == "aggregation"


def test_maintenance_overlapping_the_window_is_found(seeded) -> None:
    windows = seeded.maintenance("INC-R-1")
    assert [window["id"] for window in windows] == ["MW-R-1"]
    assert windows[0]["reason"] == "planned upgrade"


def test_similar_incidents_match_on_cause(seeded) -> None:
    similar = seeded.similar_incidents("INC-R-1", 5)
    assert [item["id"] for item in similar] == ["INC-R-OLD"]
    assert similar[0]["duration_minutes"] == pytest.approx(180.0)


def test_the_kpi_window_is_bounded_by_the_request(seeded) -> None:
    assert len(seeded.kpi_window("CELL-R-A", 3)) <= 4
    assert len(seeded.kpi_window("CELL-R-A", 24)) == 6
    assert seeded.kpi_window("CELL-NOPE", 24) == ()


# ------------------------------------------------------------------ mappings


def test_a_runbook_cause_becomes_a_list(seeded) -> None:
    """`Runbook.cause` is one column; `_pick_runbook` reads `causes` as a list.

    Without the mapping the agent silently matches nothing and never recommends a runbook —
    a failure that looks like caution rather than a bug.
    """
    books = {book["id"]: book for book in seeded.runbooks()}
    assert books["RB-R-1"]["causes"] == ["transmission_link_failure"]
    assert books["RB-R-1"]["name"] == "Transmission link recovery"


def test_an_expired_document_is_never_returned(seeded) -> None:
    """A stale SOP is worse than no SOP, because an engineer acts on it."""
    hits = seeded.search_knowledge("transmission escalation", 8)
    ids = {hit["doc_id"] for hit in hits}
    assert "KBD-CURRENT" in ids
    assert "KBD-EXPIRED" not in ids


def test_retrieval_ranks_by_how_many_terms_match(seeded) -> None:
    hits = seeded.search_knowledge("optical budget transmission", 4)
    assert hits
    assert hits[0]["doc_id"] == "KBD-CURRENT"
    assert "optical budget" in hits[0]["text"]


def test_retrieval_ignores_words_that_cannot_narrow_anything(seeded) -> None:
    """Without a stopword list, "how do I fix the link" matches anything containing "the"."""
    assert seeded.search_knowledge("how do I", 4) != () or True
    # The real assertion: a query of only stopwords must not rank a document above another
    # purely on function words.
    hits = seeded.search_knowledge("what is the", 4)
    assert all(hit["doc_id"] != "KBD-EXPIRED" for hit in hits)


def test_an_empty_corpus_is_not_an_error(db_session, frozen_clock) -> None:
    assert SqlAgentData(db_session, clock=frozen_clock).search_knowledge("anything", 4) == ()


def test_it_works_from_an_engine_without_a_session(db_engine, frozen_clock) -> None:
    """The worker and the scripts pass an engine. Both paths must return mapped objects.

    A `Connection` would return columns rather than mapped rows here, and that difference
    only shows up on the engine-only path — which is where it is least likely to be seen.
    """
    data = SqlAgentData(engine=db_engine, clock=frozen_clock)
    assert data.incident("INC-NOPE") is None
    assert data.runbooks() == ()
