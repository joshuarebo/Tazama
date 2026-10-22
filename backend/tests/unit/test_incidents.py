"""Incident aggregate unit tests: identity, locking, lifecycle, similarity, merge.

The store is an in-memory fake that enforces the same optimistic-locking contract as the
SQL adapter (a write with a stale version is refused), so the behaviour proved here is
the behaviour that runs in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tazama.core.clock import FixedClock
from tazama.core.errors import Conflict, NotFound, PolicyViolation
from tazama.core.policy import Policies, load_policies
from tazama.domain.events import EventType
from tazama.domain.state_machine import IllegalTransition, Trigger
from tazama.domain.value_objects import (
    IncidentCause,
    IncidentStatus,
    NodeKind,
    TimeWindow,
)
from tazama.infrastructure.eventbus.memory import InMemoryEventBus
from tazama.modules.incidents.models import (
    Incident,
    IncidentCandidate,
    IncidentLink,
    SimilarityQuery,
    VerificationSignal,
)
from tazama.modules.incidents.service import _STATUS_TO_STATE, IncidentService

NOW = datetime(2026, 3, 17, 0, 0, tzinfo=UTC)
DAY = datetime(2026, 3, 16, 0, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute)


# ===================================================================== fakes


@dataclass
class FakeStore:
    rows: dict[str, Incident] = field(default_factory=dict)
    updates: int = 0

    def get(self, incident_id: str) -> Incident | None:
        return self.rows.get(incident_id)

    def insert(self, incident: Incident) -> Incident:
        existing = self.rows.get(incident.id)
        if existing is not None:
            return existing
        self.rows[incident.id] = incident
        return incident

    def update(self, incident: Incident, *, expected_version: int) -> Incident:
        current = self.rows.get(incident.id)
        if current is None or current.version != expected_version:
            raise Conflict(
                f"Incident {incident.id} is at version "
                f"{current.version if current else 'none'}, not {expected_version}.",
            )
        self.rows[incident.id] = incident
        self.updates += 1
        return incident

    def find_by_nodes(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[Incident, ...]:
        wanted = set(node_ids)
        return tuple(
            row
            for row in self.rows.values()
            if row.primary_node_id in wanted and row.window.overlaps(window)
        )

    def find_similar(self, query: SimilarityQuery) -> tuple[Incident, ...]:
        found = [
            row
            for row in self.rows.values()
            if row.cause is query.cause
            and row.status is not IncidentStatus.SUPPRESSED
            and (query.region is None or row.region == query.region)
            and (
                query.primary_node_kind is None or row.primary_node_kind == query.primary_node_kind
            )
            and row.affected_cells >= query.min_affected_cells
            and (query.max_affected_cells is None or row.affected_cells <= query.max_affected_cells)
            and row.id != query.exclude_id
        ]
        found.sort(key=lambda row: (row.detected_at, row.id), reverse=True)
        return tuple(found[: query.limit])


@dataclass
class FakeLinks:
    rows: list[IncidentLink] = field(default_factory=list)

    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        if any(
            row.incident_id == incident_id
            and row.entity_type == entity_type
            and row.entity_id == entity_id
            for row in self.rows
        ):
            return False
        self.rows.append(
            IncidentLink(
                incident_id=incident_id,
                entity_type=entity_type,
                entity_id=entity_id,
                weight=weight,
                rationale=rationale,
            ),
        )
        return True

    def for_incident(self, incident_id: str) -> tuple[IncidentLink, ...]:
        return tuple(row for row in self.rows if row.incident_id == incident_id)

    def move(self, *, from_incident_id: str, to_incident_id: str) -> int:
        moved = 0
        for row in self.for_incident(from_incident_id):
            if self.link(
                to_incident_id,
                row.entity_type,
                row.entity_id,
                weight=row.weight,
                rationale=row.rationale,
            ):
                moved += 1
        self.rows = [row for row in self.rows if row.incident_id != from_incident_id]
        return moved


@dataclass
class FakeDecisionLog:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Sequence[Any] = (),
        confidence: float | None = None,
        rule_version: str | None = None,
    ) -> None:
        self.calls.append(
            {"action": action, "outcome": outcome, "rule_version": rule_version},
        )


@pytest.fixture
def policies(repo_root) -> Policies:
    return load_policies(repo_root / "config" / "policies.yaml")


@pytest.fixture
def parts(policies) -> dict[str, Any]:
    return {
        "store": FakeStore(),
        "links": FakeLinks(),
        "publisher": InMemoryEventBus(),
        "decisions": FakeDecisionLog(),
    }


@pytest.fixture
def service(parts, policies) -> IncidentService:
    return IncidentService(clock=FixedClock(NOW), policies=policies, **parts)


def candidate(
    *,
    node_id: str = "CELL-A",
    kind: NodeKind = NodeKind.CELL,
    cause: IncidentCause = IncidentCause.CELL_OUTAGE,
    start_hour: int = 6,
    end_hour: int = 9,
    cells: tuple[str, ...] = ("CELL-A",),
    sites: tuple[str, ...] = ("SITE-1",),
    complaints: int = 4,
    suppressed: str | None = None,
    confidence: float = 0.8,
) -> IncidentCandidate:
    return IncidentCandidate(
        primary_node_id=node_id,
        primary_node_kind=kind,
        cause=cause,
        window=TimeWindow(start=at(start_hour), end=at(end_hour)),
        region="Nairobi",
        cell_ids=cells,
        site_ids=sites,
        severity=0.9,
        confidence=confidence,
        complaint_count=complaints,
        title="Cell outage on CELL-A",
        summary="one cell down",
        suppressed_reason=suppressed,
    )


# ================================================================== identity


def test_the_same_candidate_never_creates_a_second_incident(service, parts) -> None:
    first = service.open_or_update(candidate())
    second = service.open_or_update(candidate())

    assert first.id == second.id
    assert len(parts["store"].rows) == 1
    assert parts["store"].updates == 0, "a replay must not write"


def test_identity_is_derived_from_element_cause_and_window_bucket(service) -> None:
    base = service.open_or_update(candidate())
    same_hour = service.open_or_update(candidate(start_hour=6, end_hour=8))
    other_element = service.open_or_update(candidate(node_id="CELL-B"))
    other_cause = service.open_or_update(candidate(cause=IncidentCause.CONGESTION))
    other_hour = service.open_or_update(candidate(start_hour=11, end_hour=12))

    assert same_hour.id == base.id
    assert len({base.id, other_element.id, other_cause.id, other_hour.id}) == 4


def test_a_replay_publishes_no_second_event(service, parts) -> None:
    service.open_or_update(candidate())
    bus: InMemoryEventBus = parts["publisher"]
    assert len(bus.of_type(EventType.INCIDENT_DETECTED)) == 1

    service.open_or_update(candidate())

    assert len(bus.of_type(EventType.INCIDENT_DETECTED)) == 1
    assert len(bus.of_type(EventType.INCIDENT_UPDATED)) == 0


def test_new_evidence_widens_the_incident_and_publishes_an_update(service, parts) -> None:
    service.open_or_update(candidate())
    updated = service.open_or_update(
        candidate(cells=("CELL-A", "CELL-A2"), complaints=11, end_hour=11),
    )

    assert updated.affected_cells == 2
    assert updated.complaint_count == 11
    assert updated.version == 2
    assert len(parts["publisher"].of_type(EventType.INCIDENT_UPDATED)) == 1


def test_a_suppressed_candidate_lands_suppressed_and_records_the_reason(service) -> None:
    incident = service.open_or_update(candidate(suppressed="planned_maintenance"))

    assert incident.status is IncidentStatus.SUPPRESSED
    assert incident.suppressed_reason == "planned_maintenance"
    assert not incident.is_open


def test_opening_records_a_decision_with_the_rule_version(service, parts, policies) -> None:
    service.open_or_update(candidate())

    calls = parts["decisions"].calls
    assert calls[0]["action"] == "open_incident"
    assert calls[0]["outcome"] == "created"
    assert calls[0]["rule_version"] == policies.rule_version


# =========================================================== optimistic locking


def test_a_stale_update_raises_conflict(service, parts) -> None:
    incident = service.open_or_update(candidate())
    service.apply_impact(
        incident.id,
        affected_cells=1,
        affected_sites=1,
        affected_subs=1200,
        enterprise_accounts=1,
        sla_risk=0.4,
    )

    with pytest.raises(Conflict):
        service.apply_impact(
            incident.id,
            affected_cells=1,
            affected_sites=1,
            affected_subs=9999,
            enterprise_accounts=1,
            sla_risk=0.9,
            expected_version=incident.version,
        )


def test_a_stale_transition_raises_conflict(service) -> None:
    incident = service.open_or_update(candidate())
    service.transition(incident.id, Trigger.DIAGNOSE)

    with pytest.raises(Conflict):
        service.transition(
            incident.id,
            Trigger.SUPPRESS,
            expected_version=incident.version,
        )


def test_an_unknown_incident_raises_not_found(service) -> None:
    with pytest.raises(NotFound):
        service.get("INC-NOPE")


# ================================================================== lifecycle


def test_every_incident_status_maps_to_a_lifecycle_state() -> None:
    assert set(_STATUS_TO_STATE) == set(IncidentStatus)


def test_status_changes_go_through_the_state_machine(service) -> None:
    incident = service.open_or_update(candidate())
    assert incident.status is IncidentStatus.DETECTED

    diagnosing = service.transition(incident.id, Trigger.DIAGNOSE)

    assert diagnosing.status is IncidentStatus.DIAGNOSING
    assert diagnosing.version == 2


def test_an_illegal_transition_is_refused(service) -> None:
    incident = service.open_or_update(candidate())

    with pytest.raises(IllegalTransition):
        service.transition(incident.id, Trigger.CLOSE)


def test_closure_requires_a_passing_verification_signal(service) -> None:
    incident = service.open_or_update(candidate())

    with pytest.raises(PolicyViolation):
        service.mark_resolved(
            incident.id,
            verification=VerificationSignal(
                incident_id=incident.id,
                passed=False,
                checked_at=NOW,
                detail="KPIs still degraded",
            ),
        )
    assert service.get(incident.id).status is IncidentStatus.DETECTED


def test_a_verification_signal_for_another_incident_is_refused(service) -> None:
    incident = service.open_or_update(candidate())

    with pytest.raises(PolicyViolation):
        service.mark_resolved(
            incident.id,
            verification=VerificationSignal(
                incident_id="INC-SOMEONE-ELSE",
                passed=True,
                checked_at=NOW,
            ),
        )


def test_resolve_then_close_then_reopen_walks_the_declared_lifecycle(service) -> None:
    incident = service.open_or_update(candidate())

    resolved = service.mark_resolved(
        incident.id,
        verification=VerificationSignal(
            incident_id=incident.id,
            passed=True,
            checked_at=at(10),
            detail="KPIs recovered, alarms cleared, quiet period passed",
        ),
    )
    assert resolved.status is IncidentStatus.RESOLVED
    assert resolved.resolved_at == at(10)

    closed = service.close(resolved.id, expected_version=resolved.version)
    assert closed.status is IncidentStatus.CLOSED
    assert closed.closed_at == NOW

    reopened = service.reopen(closed.id, reason="recurrence the next morning")
    assert reopened.status is IncidentStatus.REOPENED
    assert reopened.closed_at is None
    assert reopened.resolved_at is None


def test_closing_an_unresolved_incident_is_refused(service) -> None:
    incident = service.open_or_update(candidate())

    with pytest.raises(IllegalTransition):
        service.close(incident.id)


def test_suppressing_records_the_reason(service) -> None:
    incident = service.open_or_update(candidate())

    suppressed = service.suppress(incident.id, reason="duplicate of INC-OTHER")

    assert suppressed.status is IncidentStatus.SUPPRESSED
    assert suppressed.suppressed_reason == "duplicate of INC-OTHER"


# ===================================================================== links


def test_linking_is_idempotent(service) -> None:
    incident = service.open_or_update(candidate())

    assert service.link(incident.id, "complaint", "CMP-1", rationale="first") is True
    assert service.link(incident.id, "complaint", "CMP-1", rationale="again") is False
    assert len(service.links_for(incident.id)) == 1


# ================================================================ similarity


def test_similar_incidents_filters_on_cause_region_kind_and_size(service, parts) -> None:
    target = service.open_or_update(candidate(cells=("CELL-A", "CELL-B")))
    store: FakeStore = parts["store"]

    def seed(incident_id: str, **changes: Any) -> None:
        store.rows[incident_id] = store.rows[target.id].with_changes(
            id=incident_id,
            **changes,
        )

    seed("INC-SAME", detected_at=NOW - timedelta(days=1))
    seed("INC-OTHER-CAUSE", cause=IncidentCause.CONGESTION)
    seed("INC-OTHER-REGION", region="Mombasa")
    seed("INC-OTHER-KIND", primary_node_kind=str(NodeKind.SITE))
    seed("INC-TOO-BIG", affected_cells=400)
    seed("INC-SUPPRESSED", status=IncidentStatus.SUPPRESSED)

    found = {row.id for row in service.similar_incidents(store.rows[target.id], limit=10)}

    assert "INC-SAME" in found
    assert target.id not in found
    assert found.isdisjoint(
        {
            "INC-OTHER-CAUSE",
            "INC-OTHER-REGION",
            "INC-OTHER-KIND",
            "INC-TOO-BIG",
            "INC-SUPPRESSED",
        },
    )


def test_similar_incidents_returns_newest_first(service, parts) -> None:
    target = service.open_or_update(candidate())
    store: FakeStore = parts["store"]
    for days, name in ((3, "INC-OLD"), (1, "INC-NEW"), (2, "INC-MID")):
        store.rows[name] = store.rows[target.id].with_changes(
            id=name,
            detected_at=NOW - timedelta(days=days),
        )

    found = service.similar_incidents(store.rows[target.id], limit=3)

    assert [row.id for row in found] == ["INC-NEW", "INC-MID", "INC-OLD"]


# ===================================================================== merge


def test_merge_moves_links_marks_the_absorbed_incident_and_deletes_nothing(
    service,
    parts,
) -> None:
    into = service.open_or_update(candidate(node_id="LINK-1", kind=NodeKind.TRANSMISSION_LINK))
    other = service.open_or_update(candidate(node_id="CELL-B", cells=("CELL-B",)))
    service.link(other.id, "complaint", "CMP-9")
    service.link(other.id, "cell", "CELL-B")

    outcome = service.merge(into.id, other.id, reason="both hang off LINK-1")

    assert outcome.links_moved == 2
    moved = {(row.entity_type, row.entity_id) for row in service.links_for(into.id)}
    assert ("complaint", "CMP-9") in moved
    assert ("cell", "CELL-B") in moved
    assert ("incident", other.id) in moved, "the absorbed incident stays visible"

    absorbed = service.get(other.id)
    assert absorbed.status is IncidentStatus.SUPPRESSED
    assert absorbed.suppressed_reason == f"merged_into:{into.id}"
    assert absorbed.summary is not None
    assert into.id in absorbed.summary
    assert service.get(other.id) is not None, "nothing is deleted"


def test_merging_an_incident_into_itself_is_refused(service) -> None:
    incident = service.open_or_update(candidate())

    with pytest.raises(PolicyViolation):
        service.merge(incident.id, incident.id, reason="nonsense")


def test_merging_an_already_merged_incident_is_refused(service) -> None:
    into = service.open_or_update(candidate(node_id="LINK-1", kind=NodeKind.TRANSMISSION_LINK))
    other = service.open_or_update(candidate(node_id="CELL-B"))
    service.merge(into.id, other.id, reason="shared upstream")

    with pytest.raises(Conflict):
        service.merge(into.id, other.id, reason="again")


def test_merge_grows_the_target_and_publishes_an_update(service, parts) -> None:
    into = service.open_or_update(
        candidate(node_id="LINK-1", kind=NodeKind.TRANSMISSION_LINK, complaints=5),
    )
    other = service.open_or_update(
        candidate(node_id="CELL-B", cells=("CELL-B", "CELL-C"), complaints=7),
    )

    service.merge(into.id, other.id, reason="shared upstream")
    grown = service.get(into.id)

    assert grown.complaint_count == 12
    assert grown.affected_cells == 2
    assert any(
        event.payload.get("absorbed") == other.id
        for event in parts["publisher"].of_type(EventType.INCIDENT_UPDATED)
    )


# =================================================================== lookups


def test_find_by_nodes_returns_incidents_rooted_on_any_of_them(service) -> None:
    cell = service.open_or_update(candidate())
    service.open_or_update(candidate(node_id="CELL-ELSEWHERE"))

    found = service.find_by_nodes(
        ["CELL-A", "SITE-1", "LINK-1"],
        TimeWindow(start=at(7), end=at(8)),
    )

    assert [row.id for row in found] == [cell.id]
