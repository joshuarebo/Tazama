"""The incident aggregate and its lifecycle.

Three invariants this service exists to hold:

* **Idempotency.** `open_or_update` is keyed on the candidate's deterministic id, so a
  replay, a retried worker tick or the complaint path arriving after the network path all
  converge on one incident. Running the whole engine twice must change nothing.
* **Optimistic locking.** Every write carries the version it read. A stale write raises
  `Conflict` instead of silently overwriting somebody else's update.
* **No ad-hoc status writes.** Every transition goes through `domain.state_machine`. The
  service never assigns a status directly; it asks the table for a legal path and walks
  it, which is why an illegal request fails loudly rather than corrupting the lifecycle.

Closure specifically requires a verification signal. The verification module publishes
`VerificationCompleted`; without a passing signal, `mark_resolved` refuses.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from tazama.core.clock import Clock
from tazama.core.errors import Conflict, NotFound, PolicyViolation
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import (
    AggregateType,
    IncidentDetected,
    IncidentUpdated,
)
from tazama.domain.state_machine import (
    TRANSITION_TABLE,
    CaseState,
    IllegalTransition,
    Trigger,
    advance,
)
from tazama.domain.value_objects import IncidentCause, IncidentStatus, Priority, TimeWindow
from tazama.modules.incidents.models import (
    Evidence,
    Incident,
    IncidentCandidate,
    IncidentLink,
    MergeOutcome,
    SimilarityQuery,
    VerificationSignal,
)
from tazama.modules.incidents.ports import (
    DecisionLog,
    EventPublisher,
    IncidentStore,
    LinkStore,
)

_log = get_logger(__name__)

#: Incident status <-> case lifecycle state. The lifecycle also contains stages that live
#: on `case_record` (prioritised, routed, notifying) rather than on the incident, so the
#: service walks *through* them without ever persisting them here.
_STATUS_TO_STATE: dict[IncidentStatus, CaseState] = {
    IncidentStatus.DETECTED: CaseState.CORRELATED,
    IncidentStatus.DIAGNOSING: CaseState.DIAGNOSING,
    IncidentStatus.MITIGATING: CaseState.MITIGATING,
    IncidentStatus.VERIFYING: CaseState.VERIFYING,
    IncidentStatus.RESOLVED: CaseState.RESOLVED,
    IncidentStatus.CLOSED: CaseState.CLOSED,
    IncidentStatus.REOPENED: CaseState.REOPENED,
    IncidentStatus.SUPPRESSED: CaseState.SUPPRESSED,
}

_STATE_TO_STATUS: dict[CaseState, IncidentStatus] = {
    CaseState.CORRELATED: IncidentStatus.DETECTED,
    CaseState.DIAGNOSING: IncidentStatus.DIAGNOSING,
    CaseState.MITIGATING: IncidentStatus.MITIGATING,
    CaseState.VERIFYING: IncidentStatus.VERIFYING,
    CaseState.RESOLVED: IncidentStatus.RESOLVED,
    CaseState.CLOSED: IncidentStatus.CLOSED,
    CaseState.REOPENED: IncidentStatus.REOPENED,
    CaseState.SUPPRESSED: IncidentStatus.SUPPRESSED,
}

#: Triggers never taken automatically while searching for a path: they are decisions, not
#: steps. Without this a search for "resolve" could cheerfully suppress the incident.
_NON_AUTOMATIC: frozenset[Trigger] = frozenset(
    {
        Trigger.SUPPRESS,
        Trigger.ESCALATE_TO_HUMAN,
        Trigger.RESUME_FROM_HUMAN,
        Trigger.REOPEN,
        Trigger.CLOSE,
    },
)

#: Comparable size for similarity: half to double the number of affected cells.
SIMILAR_SIZE_FACTOR = 2


class IncidentService:
    """Owns the incident aggregate. Dependencies by keyword, like every Phase 2 service."""

    def __init__(
        self,
        *,
        store: IncidentStore,
        links: LinkStore,
        publisher: EventPublisher,
        decisions: DecisionLog,
        clock: Clock,
        policies: Policies,
    ) -> None:
        self._store = store
        self._links = links
        self._publisher = publisher
        self._decisions = decisions
        self._clock = clock
        self._policies = policies

    # ===================================================================== reads

    def get(self, incident_id: str) -> Incident:
        incident = self._store.get(incident_id)
        if incident is None:
            raise NotFound(f"Unknown incident {incident_id!r}.")
        return incident

    def links_for(self, incident_id: str) -> tuple[IncidentLink, ...]:
        return self._links.for_incident(incident_id)

    def find_by_nodes(
        self,
        node_ids: Sequence[str],
        window: TimeWindow,
    ) -> tuple[Incident, ...]:
        """Incidents rooted on any of these elements, overlapping the window.

        Correlation asks this when a complaint arrives: the complaint's cell and every
        element above it are candidate roots, because the outage may have been collapsed
        onto a site or a transmission link.
        """
        return self._store.find_by_nodes(node_ids, window)

    def similar_incidents(self, incident: Incident, limit: int = 5) -> tuple[Incident, ...]:
        """Same cause, same region and element kind, comparable size, newest first.

        Structured filters only: this is what the Command Center shows a NOC engineer and
        what the Phase 4 DiagnosisAgent reads as prior art, so it has to be defensible.
        """
        size = max(incident.affected_cells, 1)
        query = SimilarityQuery(
            cause=incident.cause,
            region=incident.region,
            primary_node_kind=incident.primary_node_kind,
            min_affected_cells=max(size // SIMILAR_SIZE_FACTOR, 0),
            max_affected_cells=size * SIMILAR_SIZE_FACTOR,
            exclude_id=incident.id,
            limit=max(1, limit),
        )
        return self._store.find_similar(query)

    # ==================================================================== writes

    def open_or_update(self, candidate: IncidentCandidate) -> Incident:
        """Create the incident, or fold new evidence into the one that already exists.

        Idempotent by construction: the id comes from the candidate, and an update that
        would change nothing writes nothing and publishes nothing.
        """
        now = self._clock.now()
        existing = self._store.get(candidate.key)
        if existing is None:
            incident = self._from_candidate(candidate, now)
            stored = self._store.insert(incident)
            self._publisher.publish(
                IncidentDetected(
                    aggregate_type=AggregateType.INCIDENT,
                    aggregate_id=stored.id,
                    occurred_at=now,
                    dedupe_key=stored.id,
                    payload=self._event_payload(stored),
                ),
            )
            self._record(
                action="open_incident",
                entity_id=stored.id,
                outcome="created",
                inputs=self._candidate_inputs(candidate),
                confidence=candidate.confidence,
            )
            return stored

        merged = self._fold(existing, candidate, now)
        if _same_facts(existing, merged):
            # A replay. Nothing changed, so nothing is written and nothing is published.
            self._record(
                action="open_incident",
                entity_id=existing.id,
                outcome="unchanged",
                inputs=self._candidate_inputs(candidate),
                confidence=candidate.confidence,
            )
            return existing

        stored = self._store.update(merged, expected_version=existing.version)
        self._publisher.publish(
            IncidentUpdated(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=stored.id,
                occurred_at=now,
                dedupe_key=f"{stored.id}|v{stored.version}",
                payload=self._event_payload(stored),
            ),
        )
        self._record(
            action="open_incident",
            entity_id=stored.id,
            outcome="updated",
            inputs=self._candidate_inputs(candidate),
            confidence=candidate.confidence,
        )
        return stored

    def apply_impact(
        self,
        incident_id: str,
        *,
        affected_cells: int,
        affected_sites: int,
        affected_subs: int,
        enterprise_accounts: int,
        sla_risk: float,
        expected_version: int | None = None,
    ) -> Incident:
        """Record the impact module's numbers on the aggregate (they drive priority)."""
        incident = self.get(incident_id)
        if expected_version is not None and expected_version != incident.version:
            raise Conflict(
                f"Incident {incident_id} is at version {incident.version}, not {expected_version}.",
            )
        updated = incident.with_changes(
            affected_cells=affected_cells,
            affected_sites=affected_sites,
            affected_subs=affected_subs,
            enterprise_accounts=enterprise_accounts,
            sla_risk=sla_risk,
            updated_at=self._clock.now(),
            version=incident.version + 1,
        )
        if _same_facts(incident, updated):
            return incident
        stored = self._store.update(updated, expected_version=incident.version)
        self._publisher.publish(
            IncidentUpdated(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=stored.id,
                occurred_at=stored.updated_at,
                dedupe_key=f"{stored.id}|impact|v{stored.version}",
                payload=self._event_payload(stored),
            ),
        )
        return stored

    def link(
        self,
        incident_id: str,
        entity_type: str,
        entity_id: str,
        *,
        weight: float = 1.0,
        rationale: str = "",
    ) -> bool:
        """Attach evidence. Idempotent on the (incident, type, entity) unique constraint."""
        created = self._links.link(
            incident_id,
            entity_type,
            entity_id,
            weight=weight,
            rationale=rationale,
        )
        if created:
            _log.debug(
                "incident_linked",
                extra={
                    "incident_id": incident_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                },
            )
        return created

    # =============================================================== transitions

    def transition(
        self,
        incident_id: str,
        trigger: Trigger,
        *,
        expected_version: int | None = None,
        reason: str = "",
    ) -> Incident:
        """Apply one declared trigger. The only mutator of `status` in the system."""
        incident = self.get(incident_id)
        self._check_version(incident, expected_version)
        state = _state_of(incident)
        transition = advance(state, trigger)
        target = _STATE_TO_STATUS.get(transition.target)
        if target is None:
            raise PolicyViolation(
                f"Case state {transition.target} is tracked on the case record, "
                "not on the incident aggregate.",
            )
        return self._write_status(incident, target, reason=reason, trigger=trigger)

    def mark_resolved(
        self,
        incident_id: str,
        *,
        verification: VerificationSignal,
        expected_version: int | None = None,
    ) -> Incident:
        """Resolve, but only on a passing verification signal.

        The verification module owns the check (KPI recovery, alarms cleared, quiet
        period). This service refuses to resolve on anything else, because "it looks
        better now" is how an incident gets closed while customers are still offline.
        """
        incident = self.get(incident_id)
        self._check_version(incident, expected_version)
        if not verification.passed:
            raise PolicyViolation(
                f"Incident {incident_id} cannot be resolved: verification did not pass.",
            )
        if verification.incident_id != incident_id:
            raise PolicyViolation(
                f"Verification signal is for {verification.incident_id}, not {incident_id}.",
            )
        return self._walk(
            incident,
            CaseState.RESOLVED,
            reason=verification.detail or "verification passed",
            resolved_at=verification.checked_at,
        )

    def close(
        self,
        incident_id: str,
        *,
        expected_version: int | None = None,
        reason: str = "",
    ) -> Incident:
        """Close a resolved incident. Only reachable from RESOLVED, per the lifecycle."""
        incident = self.get(incident_id)
        self._check_version(incident, expected_version)
        state = _state_of(incident)
        advance(state, Trigger.CLOSE)  # raises IllegalTransition unless resolved
        return self._write_status(
            incident,
            IncidentStatus.CLOSED,
            reason=reason or "closed after verification",
            trigger=Trigger.CLOSE,
            closed_at=self._clock.now(),
        )

    def reopen(
        self,
        incident_id: str,
        *,
        reason: str,
        expected_version: int | None = None,
    ) -> Incident:
        """Bring an incident back: a failed fix, a recurrence, or a late complaint."""
        incident = self.get(incident_id)
        self._check_version(incident, expected_version)
        state = _state_of(incident)
        advance(state, Trigger.REOPEN)
        return self._write_status(
            incident,
            IncidentStatus.REOPENED,
            reason=reason,
            trigger=Trigger.REOPEN,
            resolved_at=None,
            closed_at=None,
        )

    def suppress(
        self,
        incident_id: str,
        *,
        reason: str,
        expected_version: int | None = None,
    ) -> Incident:
        """Mark something as deliberately not an incident, with the reason recorded."""
        incident = self.get(incident_id)
        self._check_version(incident, expected_version)
        state = _state_of(incident)
        advance(state, Trigger.SUPPRESS)
        return self._write_status(
            incident,
            IncidentStatus.SUPPRESSED,
            reason=reason,
            trigger=Trigger.SUPPRESS,
            suppressed_reason=reason,
        )

    # ===================================================================== merge

    def merge(self, into_id: str, other_id: str, *, reason: str) -> MergeOutcome:
        """Fold `other` into `into` when correlation finds they share an upstream element.

        Links move, the absorbed incident is marked and kept, and nothing is deleted: an
        auditor must still be able to see that two incidents were once separate.
        """
        if into_id == other_id:
            raise PolicyViolation("An incident cannot be merged into itself.")
        target = self.get(into_id)
        absorbed = self.get(other_id)
        if (
            absorbed.status is IncidentStatus.SUPPRESSED
            and absorbed.suppressed_reason
            and (absorbed.suppressed_reason.startswith("merged_into:"))
        ):
            raise Conflict(f"Incident {other_id} has already been merged.")

        moved = self._links.move(from_incident_id=other_id, to_incident_id=into_id)
        self._links.link(
            into_id,
            "incident",
            other_id,
            weight=1.0,
            rationale=f"absorbed: {reason}",
        )
        now = self._clock.now()
        self._write_status(
            absorbed,
            IncidentStatus.SUPPRESSED,
            reason=reason,
            trigger=Trigger.SUPPRESS,
            suppressed_reason=f"merged_into:{into_id}",
            summary=f"merged into {into_id}: {reason}",
        )
        grown = target.with_changes(
            affected_cells=max(target.affected_cells, absorbed.affected_cells),
            affected_sites=max(target.affected_sites, absorbed.affected_sites),
            complaint_count=target.complaint_count + absorbed.complaint_count,
            window_end=max(
                target.window.end,
                absorbed.window.end,
            ),
            updated_at=now,
            version=target.version + 1,
        )
        stored = self._store.update(grown, expected_version=target.version)
        self._publisher.publish(
            IncidentUpdated(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=stored.id,
                occurred_at=now,
                dedupe_key=f"{stored.id}|merge|{other_id}",
                payload={**self._event_payload(stored), "absorbed": other_id},
            ),
        )
        outcome = MergeOutcome(
            into_id=into_id,
            absorbed_id=other_id,
            links_moved=moved,
            reason=reason,
            evidence=(Evidence(kind="incident", ref=other_id, note=f"{moved} links moved"),),
        )
        self._record(
            action="merge_incident",
            entity_id=into_id,
            outcome="merged",
            inputs={"absorbed": other_id, "links_moved": moved, "reason": reason},
            evidence=outcome.evidence,
        )
        return outcome

    # ================================================================== internals

    def _walk(
        self,
        incident: Incident,
        target_state: CaseState,
        *,
        reason: str,
        **changes: Any,
    ) -> Incident:
        """Walk the declared transition table from here to `target_state`.

        Searching the table rather than hard-coding a path means the lifecycle stays the
        single source of truth: if a transition is removed, this stops working loudly.
        """
        path = _shortest_path(_state_of(incident), target_state)
        if path is None:
            raise IllegalTransition(_state_of(incident), Trigger.RESOLVE)
        current = incident
        for trigger, state in path:
            status = _STATE_TO_STATUS.get(state)
            if status is None:
                # An intermediate stage owned by the case record: traversed, not stored.
                continue
            current = self._write_status(
                current,
                status,
                reason=reason,
                trigger=trigger,
                **(changes if state is target_state else {}),
            )
        return current

    def _write_status(
        self,
        incident: Incident,
        status: IncidentStatus,
        *,
        reason: str,
        trigger: Trigger,
        **changes: Any,
    ) -> Incident:
        now = self._clock.now()
        if status is IncidentStatus.RESOLVED:
            changes.setdefault("resolved_at", now)
        updated = incident.with_changes(
            status=status,
            updated_at=now,
            version=incident.version + 1,
            **changes,
        )
        stored = self._store.update(updated, expected_version=incident.version)
        self._publisher.publish(
            IncidentUpdated(
                aggregate_type=AggregateType.INCIDENT,
                aggregate_id=stored.id,
                occurred_at=now,
                dedupe_key=f"{stored.id}|{status}|v{stored.version}",
                payload={
                    **self._event_payload(stored),
                    "trigger": str(trigger),
                    "reason": reason,
                },
            ),
        )
        self._record(
            action="transition_incident",
            entity_id=stored.id,
            outcome=str(status),
            inputs={
                "from": str(incident.status),
                "to": str(status),
                "trigger": str(trigger),
                "reason": reason,
            },
        )
        return stored

    def _fold(self, existing: Incident, candidate: IncidentCandidate, now: datetime) -> Incident:
        """Widen the window and raise the counts; never shrink what was already seen."""
        confidence = max(existing.cause_confidence or 0.0, candidate.confidence)
        window_end = max(
            existing.window_end or existing.detected_at,
            candidate.window.end,
        )
        suppressed = existing.suppressed_reason or candidate.suppressed_reason
        return existing.with_changes(
            cause=candidate.cause if existing.cause is IncidentCause.UNKNOWN else existing.cause,
            cause_confidence=round(confidence, 4),
            window_end=window_end,
            affected_cells=max(existing.affected_cells, len(candidate.cell_ids)),
            affected_sites=max(existing.affected_sites, len(candidate.site_ids)),
            complaint_count=max(existing.complaint_count, candidate.complaint_count),
            summary=candidate.summary or existing.summary,
            suppressed_reason=suppressed,
            updated_at=now,
            version=existing.version + 1,
        )

    def _from_candidate(self, candidate: IncidentCandidate, now: datetime) -> Incident:
        status = (
            IncidentStatus.SUPPRESSED if candidate.suppressed_reason else IncidentStatus.DETECTED
        )
        return Incident(
            id=candidate.key,
            title=candidate.title or f"{candidate.cause} on {candidate.primary_node_id}",
            status=status,
            # Priority belongs to the prioritisation module; a new incident starts at the
            # bottom rather than pretending to know.
            priority=Priority.P3,
            cause=candidate.cause,
            region=candidate.region,
            detected_at=now,
            created_at=now,
            updated_at=now,
            started_at=candidate.window.start,
            window_end=candidate.window.end,
            cause_confidence=round(candidate.confidence, 4),
            primary_node_id=candidate.primary_node_id,
            primary_node_kind=str(candidate.primary_node_kind),
            affected_cells=len(candidate.cell_ids),
            affected_sites=len(candidate.site_ids),
            complaint_count=candidate.complaint_count,
            summary=candidate.summary or None,
            suppressed_reason=candidate.suppressed_reason,
            detector=candidate.detector,
            rule_version=candidate.rule_version or self._policies.rule_version,
            version=1,
        )

    def _check_version(self, incident: Incident, expected_version: int | None) -> None:
        if expected_version is not None and expected_version != incident.version:
            raise Conflict(
                f"Incident {incident.id} is at version {incident.version}, "
                f"not {expected_version}: refusing a stale update.",
            )

    def _event_payload(self, incident: Incident) -> dict[str, Any]:
        return {
            "incident_id": incident.id,
            "status": str(incident.status),
            "cause": str(incident.cause),
            "region": incident.region,
            "primary_node_id": incident.primary_node_id,
            "primary_node_kind": incident.primary_node_kind,
            "window_start": incident.window.start.isoformat(),
            "window_end": incident.window.end.isoformat(),
            "affected_cells": incident.affected_cells,
            "affected_sites": incident.affected_sites,
            "affected_subs": incident.affected_subs,
            "enterprise_accounts": incident.enterprise_accounts,
            "complaint_count": incident.complaint_count,
            "sla_risk": incident.sla_risk,
            "suppressed_reason": incident.suppressed_reason,
            "version": incident.version,
        }

    def _candidate_inputs(self, candidate: IncidentCandidate) -> dict[str, Any]:
        return {
            "primary_node_id": candidate.primary_node_id,
            "primary_node_kind": str(candidate.primary_node_kind),
            "cause": str(candidate.cause),
            "window_start": candidate.window.start.isoformat(),
            "window_end": candidate.window.end.isoformat(),
            "cells": len(candidate.cell_ids),
            "sites": len(candidate.site_ids),
            "severity": candidate.severity,
            "complaint_count": candidate.complaint_count,
            "suppressed_reason": candidate.suppressed_reason,
        }

    def _record(
        self,
        *,
        action: str,
        entity_id: str,
        outcome: str,
        inputs: dict[str, Any],
        evidence: Sequence[Any] = (),
        confidence: float | None = None,
    ) -> None:
        self._decisions.record(
            action=action,
            entity_type="incident",
            entity_id=entity_id,
            outcome=outcome,
            inputs=inputs,
            evidence=evidence,
            confidence=confidence,
            rule_version=self._policies.rule_version,
        )


# --------------------------------------------------------------------- helpers


def _state_of(incident: Incident) -> CaseState:
    state = _STATUS_TO_STATE.get(incident.status)
    if state is None:  # pragma: no cover - every IncidentStatus is mapped, asserted in tests
        raise PolicyViolation(f"Incident status {incident.status} has no lifecycle state.")
    return state


def _shortest_path(
    source: CaseState,
    target: CaseState,
) -> list[tuple[Trigger, CaseState]] | None:
    """Breadth-first search over the declared transition table."""
    if source is target:
        return []
    queue: deque[tuple[CaseState, list[tuple[Trigger, CaseState]]]] = deque([(source, [])])
    seen = {source}
    while queue:
        state, path = queue.popleft()
        for (from_state, trigger), transition in sorted(
            TRANSITION_TABLE.items(),
            key=lambda item: (str(item[0][0]), str(item[0][1])),
        ):
            if from_state is not state or trigger in _NON_AUTOMATIC:
                continue
            if transition.target in seen:
                continue
            step = [*path, (trigger, transition.target)]
            if transition.target is target:
                return step
            seen.add(transition.target)
            queue.append((transition.target, step))
    return None


def _same_facts(left: Incident, right: Incident) -> bool:
    """Equality ignoring the bookkeeping fields, so a replay is recognisably a replay."""
    ignored = {"updated_at", "version"}
    return all(
        getattr(left, name) == getattr(right, name)
        for name in left.__slots__
        if name not in ignored
    )


__all__ = ["IncidentService"]
