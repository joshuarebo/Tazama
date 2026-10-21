"""Correlation: turn observations into incident candidates, and explain every decision.

One engine, two entry points:

* `detect_degradations(window)` is **network-first**. It scans KPI health, collapses alarm
  storms, walks the topology and raises incidents whether or not anybody complained.
  Complaints are confirmation, not a precondition — otherwise "silent sufferers" and
  "the outage that silenced the channels" are invisible by construction.
* `correlate_complaint(observation)` is customer-first: a located complaint either joins
  the incident covering its **event time** or, with enough corroborated company, starts
  one.

The rules, and why each exists:

1. **Spatio-temporal clustering** — complaints close in space and time are one event.
   Normalised by `subscribers_est`, so a busy cell must shout louder than a rural one.
2. **Topology-aware joining** — degradation on cells that share an upstream element is
   one incident rooted at that element. This is the difference between "nine sites are
   broken" and "one transmission link is down".
3. **Maintenance suppression** — an approved window on the element (or any ancestor)
   means this was expected. Suppression is recorded, never silent.
4. **Alarm-storm collapsing** — alarms on related elements inside the collapse window are
   links on one incident, not N incidents.
5. **Flapping** — enough raise/clear transitions in an hour with no KPI impact is a
   hardware nuisance, not a customer incident.
6. **False-alarm rejection** — an alarm with healthy KPIs and nobody complaining is
   rejected. Silence plus health means nothing is wrong.
7. **Non-network rejection** — a billing surge is never a network incident, however big.
8. **Silent sufferers** — KPI degradation past the policy threshold raises an incident
   with zero complaints.
9. **Silence as signal** — a complaint rate that collapses against the cell's *own*
   baseline while KPIs collapse is evidence of an outage, not of health.
10. **Duplicate storms** — complaints sharing a `dedupe_key` count once.
11. **Late events** — event time decides the window, and a complaint may attach to an
    already-closed incident, which returns a reopen signal rather than a second incident.

Every outcome records exactly one decision through `DecisionLog`, stamped with
`policies.rule_version`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies
from tazama.domain.events import (
    AggregateType,
    Correlated,
    DegradationDetected,
)
from tazama.domain.value_objects import (
    KPI_POLARITY,
    IncidentCause,
    KpiName,
    NodeKind,
    TimeWindow,
)
from tazama.modules.correlation.models import (
    AlarmRecord,
    CellInfo,
    ComplaintCluster,
    ComplaintCount,
    ComplaintObservation,
    CorrelationDecision,
    CorrelationLink,
    CorrelationOutcome,
    Evidence,
    IncidentCandidate,
    IncidentRef,
    KpiBreach,
    SuppressionReason,
    TopologyNodeInfo,
    cluster_radius_km,
    union_window,
)
from tazama.modules.correlation.ports import (
    AlarmReader,
    CellDirectory,
    ComplaintReader,
    DecisionLog,
    EventPublisher,
    IncidentLinkRepository,
    IncidentRepository,
    KpiReader,
    MaintenanceReader,
    TopologyReader,
)

_log = get_logger(__name__)

#: Share of a cell's KPI samples that must breach before the cell is considered degraded
#: at all. Three of the ten modelled KPIs: one noisy counter (PRB utilisation touches its
#: threshold in a healthy evening busy hour) can never look like a fault, while a real
#: fault drags accessibility, retainability and integrity down together.
KPI_BREACH_MIN_SEVERITY = 0.30

#: Share of an upstream element's cells that must be degraded before clusters are merged
#: upward without an alarm on that element. Below this, "they share a link" is a
#: coincidence of the topology rather than evidence about the link.
UPSTREAM_DEGRADED_SHARE = 0.60

#: Availability below this is an outage whatever the other counters say.
OUTAGE_AVAILABILITY_PCT = 50.0

#: Minimum complaints-per-hour a cell must normally see before its silence means
#: anything. Below it, "no complaints" is the normal state and proves nothing.
SILENCE_MIN_BASELINE_PER_HOUR = 0.25

#: Observed rate below this fraction of the cell's own baseline counts as silence.
SILENCE_DROP_RATIO = 0.40

#: The per-cell complaint baseline is measured over a window the **same length** as the
#: candidate's, immediately before it. Comparing a 4-hour outage against a 24-hour average
#: is dimensionally unsound: it divides a short burst of normal activity across a whole
#: day and makes a real collapse look like business as usual. Clamped so a 15-minute
#: candidate still gets a usable sample and a 12-hour one does not reach back into
#: yesterday's different traffic shape.
SILENCE_BASELINE_MIN_HOURS = 1.0
SILENCE_BASELINE_MAX_HOURS = 6.0

#: Flapping is judged over an hour, per the NOC's own SOP.
FLAPPING_WINDOW = timedelta(hours=1)

#: How long after closure a late complaint may still attach to an incident instead of
#: starting a new one. Beyond this, a recurrence is genuinely a new incident.
LATE_ATTACH_GRACE = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class _Group:
    """Degradation collapsed onto one element: the unit a candidate is built from."""

    primary_node_id: str
    primary_node_kind: NodeKind
    region: str
    cell_ids: tuple[str, ...]
    site_ids: tuple[str, ...]
    breaches: tuple[KpiBreach, ...] = ()
    alarms: tuple[AlarmRecord, ...] = ()
    merged_from: tuple[str, ...] = ()

    @property
    def severity(self) -> float:
        if not self.breaches:
            return 0.0
        return max(b.severity for b in self.breaches)

    @property
    def worst(self) -> dict[str, float]:
        """Worst value seen for each KPI across the group's cells."""
        merged: dict[str, float] = {}
        for breach in self.breaches:
            for name, value in breach.worst.items():
                current = merged.get(name)
                beats_current = current is not None and (
                    value < current if _lower_is_worse(name) else value > current
                )
                if current is None or beats_current:
                    merged[name] = value
        return merged


@dataclass(slots=True)
class _Assessment:
    """Working state while a group is being judged. Mutable on purpose: it accumulates."""

    evidence: list[Evidence] = field(default_factory=list)
    links: list[CorrelationLink] = field(default_factory=list)
    confidence: float = 0.4
    notes: list[str] = field(default_factory=list)


class CorrelationService:
    """Groups observations into incident candidates. Stateless between calls."""

    def __init__(
        self,
        *,
        kpis: KpiReader,
        alarms: AlarmReader,
        maintenance: MaintenanceReader,
        complaints: ComplaintReader,
        cells: CellDirectory,
        topology: TopologyReader,
        incidents: IncidentRepository,
        links: IncidentLinkRepository,
        publisher: EventPublisher,
        decisions: DecisionLog,
        clock: Clock,
        policies: Policies,
    ) -> None:
        self._kpis = kpis
        self._alarms = alarms
        self._maintenance = maintenance
        self._complaints = complaints
        self._cells = cells
        self._topology = topology
        self._incidents = incidents
        self._links = links
        self._publisher = publisher
        self._decisions = decisions
        self._clock = clock
        self._policies = policies

    # ================================================================= network first

    def detect_degradations(
        self,
        window: TimeWindow,
        *,
        granularity: str = "1h",
    ) -> tuple[CorrelationOutcome, ...]:
        """Scan the network for degradation and raise, suppress or reject each finding.

        Runs with zero complaints in the database and still produces incidents: that is
        what makes the product network-first rather than complaint-driven.
        """
        breaches = self._kpis.breaches(
            window,
            granularity,
            min_severity=KPI_BREACH_MIN_SEVERITY,
        )
        alarms = self._alarms.active(window)
        counts = {row.cell_id: row for row in self._complaints.counts_by_cell(window)}

        for breach in breaches:
            self._publish_degradation(breach)

        outcomes: list[CorrelationOutcome] = []
        groups = self._group_degradations(breaches, alarms, window, granularity)
        for group in groups:
            outcomes.append(self._evaluate_group(group, counts))

        covered = {cell for group in groups for cell in group.cell_ids}
        outcomes.extend(self._evaluate_alarm_only(alarms, covered, window, counts))
        return tuple(outcomes)

    # ================================================================ complaint first

    def correlate_complaint(self, observation: ComplaintObservation) -> CorrelationOutcome:
        """Attach a located complaint to the incident covering its event time, or judge it.

        The window is centred on `event_time`, so a complaint that arrived six hours late
        is evaluated against the network as it was when the customer suffered.
        """
        policy = self._policies.correlation
        if not observation.is_network:
            return self._record(
                CorrelationOutcome(
                    decision=CorrelationDecision.REJECTED,
                    suppressed_reason=SuppressionReason.NON_NETWORK.value,
                    note=f"category {observation.category} is not a network category",
                ),
                action="correlate_complaint",
                entity_type="complaint",
                entity_id=observation.complaint_id,
                inputs={"category": str(observation.category), "cell_id": observation.cell_id},
            )

        span = timedelta(minutes=policy.time_window_minutes)
        window = TimeWindow(
            start=observation.event_time - span,
            end=observation.event_time + span,
        )
        ancestors = self._topology.upstream_of(observation.cell_id)
        node_ids = [observation.cell_id, *(node.node_id for node in ancestors)]
        existing = self._match_incident(
            self._incidents.find_candidates(node_ids, window),
            observation,
        )
        if existing is not None:
            return self._attach(observation, existing)

        corroboration = self._corroborate(observation, window)
        if corroboration is None:
            return self._record(
                CorrelationOutcome(
                    decision=CorrelationDecision.NO_MATCH,
                    suppressed_reason=SuppressionReason.UNCORROBORATED.value,
                    note="no KPI breach and no alarm on the element at the event time",
                ),
                action="correlate_complaint",
                entity_type="complaint",
                entity_id=observation.complaint_id,
                inputs={
                    "cell_id": observation.cell_id,
                    "event_time": observation.event_time.isoformat(),
                    "received_at": observation.received_at.isoformat(),
                },
            )

        breach, alarm_rows = corroboration
        cluster = self._cluster_around(observation, window)
        if not cluster.qualifies(
            min_complaints=policy.min_complaints_for_incident,
            min_per_1k_subs=policy.min_complaints_per_1k_subs,
        ):
            return self._record(
                CorrelationOutcome(
                    decision=CorrelationDecision.NO_MATCH,
                    suppressed_reason=SuppressionReason.BELOW_THRESHOLD.value,
                    note=(
                        f"{cluster.distinct_count} distinct complaints "
                        f"({cluster.per_1k_subs:.2f} per 1k subscribers) below "
                        f"{policy.min_complaints_for_incident} / "
                        f"{policy.min_complaints_per_1k_subs}"
                    ),
                ),
                action="correlate_complaint",
                entity_type="complaint",
                entity_id=observation.complaint_id,
                inputs={
                    "cell_id": observation.cell_id,
                    "distinct_complaints": cluster.distinct_count,
                    "raw_complaints": cluster.raw_count,
                    "per_1k_subs": round(cluster.per_1k_subs, 3),
                },
            )

        group = self._group_for_cluster(cluster, breach, alarm_rows, observation)
        counts = (
            ComplaintCount(
                cell_id=observation.cell_id,
                total=cluster.raw_count,
                distinct=cluster.distinct_count,
            ),
        )
        return self._evaluate_group(group, {row.cell_id: row for row in counts})

    # =============================================================== grouping

    def _group_degradations(
        self,
        breaches: Sequence[KpiBreach],
        alarms: Sequence[AlarmRecord],
        window: TimeWindow,
        granularity: str,
    ) -> tuple[_Group, ...]:
        """Collapse breached cells onto the deepest upstream element the evidence allows.

        Preferring the *highest* qualifying ancestor is what turns a transmission-link
        failure into one incident instead of nine site incidents or twenty-seven cell
        incidents.
        """
        if not breaches:
            return ()
        by_cell = {breach.cell_id: breach for breach in breaches}
        alarmed_nodes = {
            alarm.node_id for alarm in alarms if alarm.is_significant and _overlaps(alarm, window)
        }
        chains = {cell_id: self._topology.upstream_of(cell_id) for cell_id in by_cell}
        max_depth = max((len(chain) for chain in chains.values()), default=0)

        #: cell -> the ancestor it was merged into, deepest qualifying wins.
        assigned: dict[str, TopologyNodeInfo] = {}
        if self._policies.correlation.topology_shared_upstream:
            for depth in range(max_depth):
                buckets: dict[str, list[str]] = {}
                nodes: dict[str, TopologyNodeInfo] = {}
                for cell_id, chain in chains.items():
                    if depth >= len(chain):
                        continue
                    node = chain[depth]
                    if node.kind in (NodeKind.CORE, NodeKind.AGGREGATION):
                        # A whole region is not one incident; the core is never a root here.
                        continue
                    buckets.setdefault(node.node_id, []).append(cell_id)
                    nodes[node.node_id] = node
                for node_id, members in buckets.items():
                    if len(members) < 2:
                        continue
                    if self._upstream_qualifies(node_id, members, node_id in alarmed_nodes):
                        for cell_id in members:
                            assigned[cell_id] = nodes[node_id]

        groups: list[_Group] = []
        merged: dict[str, list[str]] = {}
        for cell_id in sorted(by_cell):
            owner = assigned.get(cell_id)
            if owner is not None:
                merged.setdefault(owner.node_id, []).append(cell_id)
        for members in [pair[1] for pair in sorted(merged.items())]:
            root = assigned[members[0]]
            groups.append(
                self._build_group(
                    root.node_id,
                    root.kind,
                    members,
                    by_cell,
                    alarms,
                    window,
                ),
            )
        for cell_id in sorted(by_cell):
            if cell_id in assigned:
                continue
            groups.append(
                self._build_group(cell_id, NodeKind.CELL, [cell_id], by_cell, alarms, window),
            )
        _ = granularity  # window boundaries already come from the reader
        return tuple(groups)

    def _upstream_qualifies(self, node_id: str, members: Sequence[str], alarmed: bool) -> bool:
        """Merge upward only when the element itself is implicated."""
        if alarmed:
            return True
        descendants = self._topology.descendants_of(node_id, NodeKind.CELL)
        if not descendants:
            return False
        return len(members) / len(descendants) >= UPSTREAM_DEGRADED_SHARE

    def _build_group(
        self,
        node_id: str,
        kind: NodeKind,
        members: Sequence[str],
        by_cell: Mapping[str, KpiBreach],
        alarms: Sequence[AlarmRecord],
        window: TimeWindow,
    ) -> _Group:
        breaches = tuple(by_cell[cell_id] for cell_id in sorted(members))
        site_ids = tuple(sorted({breach.site_id for breach in breaches}))
        related = {node_id, *members, *site_ids}
        collapse = timedelta(minutes=self._policies.correlation.alarm_storm_collapse_minutes)
        group_window = union_window(breach.window for breach in breaches)
        storm_window = group_window.expand(collapse)
        group_alarms = tuple(
            alarm
            for alarm in sorted(alarms, key=lambda a: (a.raised_at, a.alarm_id))
            if alarm.node_id in related and _overlaps(alarm, storm_window)
        )
        _ = window
        return _Group(
            primary_node_id=node_id,
            primary_node_kind=kind,
            region=breaches[0].region,
            cell_ids=tuple(sorted(members)),
            site_ids=site_ids,
            breaches=breaches,
            alarms=group_alarms,
            merged_from=tuple(sorted(members)) if kind is not NodeKind.CELL else (),
        )

    def _group_for_cluster(
        self,
        cluster: ComplaintCluster,
        breach: KpiBreach | None,
        alarms: Sequence[AlarmRecord],
        observation: ComplaintObservation,
    ) -> _Group:
        """A complaint-led group: the cell the complaints point at."""
        return _Group(
            primary_node_id=observation.cell_id,
            primary_node_kind=NodeKind.CELL,
            region=observation.region,
            cell_ids=cluster.cell_ids,
            site_ids=(observation.site_id,),
            breaches=(breach,) if breach is not None else (),
            alarms=tuple(alarms),
        )

    # ============================================================== evaluation

    def _evaluate_group(
        self,
        group: _Group,
        counts: Mapping[str, ComplaintCount],
    ) -> CorrelationOutcome:
        policy = self._policies.correlation
        window = (
            union_window(breach.window for breach in group.breaches)
            if group.breaches
            else _alarm_window(group.alarms)
        )
        assessment = _Assessment()
        self._add_kpi_evidence(group, assessment)
        self._add_alarm_evidence(group, assessment)
        self._add_topology_evidence(group, assessment)

        suppression = self._suppression_for(group, window)
        cluster = self._cluster_from_counts(group, counts, window)
        confirmed = cluster.qualifies(
            min_complaints=policy.min_complaints_for_incident,
            min_per_1k_subs=policy.min_complaints_per_1k_subs,
        )
        if cluster.raw_count:
            assessment.evidence.append(
                Evidence(
                    kind="complaint",
                    ref=f"cluster:{group.primary_node_id}",
                    note=(
                        f"{cluster.distinct_count} distinct complaints from "
                        f"{cluster.raw_count} submissions, "
                        f"{cluster.per_1k_subs:.2f} per 1k subscribers"
                    ),
                    weight=1.0 if confirmed else 0.5,
                ),
            )
        if confirmed:
            assessment.confidence += 0.25
        silence = self._silence_signal(group, window)
        if silence is not None:
            assessment.evidence.append(silence)
            assessment.confidence += 0.1

        severity = group.severity
        silent_sufferer = bool(group.breaches) and severity >= policy.silent_sufferer_kpi_threshold
        if silent_sufferer:
            assessment.confidence += 0.15
        if any(alarm.is_significant for alarm in group.alarms):
            assessment.confidence += 0.2
        if len(group.site_ids) > 1:
            assessment.confidence += 0.05

        cause = self._classify(group)
        if suppression is not None:
            return self._suppress(group, window, cause, suppression, assessment)

        if not (confirmed or silent_sufferer or self._alarm_corroborated(group)):
            return self._record(
                CorrelationOutcome(
                    decision=CorrelationDecision.REJECTED,
                    suppressed_reason=SuppressionReason.BELOW_THRESHOLD.value,
                    evidence=tuple(assessment.evidence),
                    note=(
                        f"severity {severity:.2f} below "
                        f"{policy.silent_sufferer_kpi_threshold:.2f} with "
                        f"{cluster.distinct_count} distinct complaints and no major alarm"
                    ),
                ),
                action="detect_degradation",
                entity_type=str(group.primary_node_kind),
                entity_id=group.primary_node_id,
                inputs=self._inputs(group, window, cluster, severity),
            )

        candidate = self._candidate(
            group,
            window,
            cause,
            assessment,
            cluster,
            severity,
            silent_sufferer=silent_sufferer,
        )
        incident = self._incidents.open_or_update(candidate)
        created = incident.incident_id == candidate.key
        linked = self._apply_links(incident.incident_id, candidate.links)
        return self._record(
            CorrelationOutcome(
                decision=(CorrelationDecision.CREATED if created else CorrelationDecision.UPDATED),
                candidate=candidate,
                incident_id=incident.incident_id,
                links=linked,
                evidence=candidate.evidence,
                confidence=candidate.confidence,
                note=candidate.summary,
            ),
            action="detect_degradation",
            entity_type="incident",
            entity_id=incident.incident_id,
            inputs=self._inputs(group, window, cluster, severity),
        )

    def _evaluate_alarm_only(
        self,
        alarms: Sequence[AlarmRecord],
        covered: Iterable[str],
        window: TimeWindow,
        counts: Mapping[str, ComplaintCount],
    ) -> tuple[CorrelationOutcome, ...]:
        """Judge alarms with no KPI degradation behind them: flapping or false alarm."""
        covered_cells = set(covered)
        by_node: dict[str, list[AlarmRecord]] = {}
        for alarm in sorted(alarms, key=lambda a: (a.raised_at, a.alarm_id)):
            if alarm.cell_id and alarm.cell_id in covered_cells:
                continue
            by_node.setdefault(alarm.node_id, []).append(alarm)

        outcomes: list[CorrelationOutcome] = []
        for node_id, node_alarms in sorted(by_node.items()):
            transitions = self._alarms.transitions(node_id, window)
            flaps = _max_transitions_per_hour(tuple(t.at for t in transitions))
            count = counts.get(node_id)
            distinct = count.distinct if count else 0
            evidence = [
                Evidence(
                    kind="alarm",
                    ref=alarm.alarm_id,
                    note=f"{alarm.alarm_type} ({alarm.severity}) with no KPI degradation",
                )
                for alarm in node_alarms[:5]
            ]
            if flaps >= self._policies.correlation.flapping_min_transitions:
                outcomes.append(
                    self._flapping(node_id, node_alarms, window, flaps, evidence),
                )
                continue
            outcomes.append(
                self._record(
                    CorrelationOutcome(
                        decision=CorrelationDecision.REJECTED,
                        suppressed_reason=SuppressionReason.UNCORROBORATED.value,
                        evidence=tuple(evidence),
                        note=(
                            f"{len(node_alarms)} alarm(s) with healthy KPIs and "
                            f"{distinct} complaints: nothing is wrong"
                        ),
                    ),
                    action="detect_degradation",
                    entity_type="alarm_source",
                    entity_id=node_id,
                    inputs={
                        "alarms": len(node_alarms),
                        "alarm_types": sorted({a.alarm_type for a in node_alarms}),
                        "distinct_complaints": distinct,
                        "kpi_breach": False,
                        "transitions_per_hour": flaps,
                    },
                ),
            )
        return tuple(outcomes)

    def _flapping(
        self,
        node_id: str,
        alarms: Sequence[AlarmRecord],
        window: TimeWindow,
        flaps: int,
        evidence: list[Evidence],
    ) -> CorrelationOutcome:
        """One suppressed record for the whole flap, never one incident per transition."""
        first = min(alarm.raised_at for alarm in alarms)
        last = max((alarm.cleared_at or alarm.raised_at) for alarm in alarms)
        flap_window = TimeWindow(start=first, end=max(last, first + timedelta(minutes=1)))
        cell = self._cells.get(node_id)
        evidence.append(
            Evidence(
                kind="alarm",
                ref=node_id,
                note=f"{flaps} raise/clear transitions within an hour, KPIs stable",
                weight=1.0,
            ),
        )
        candidate = IncidentCandidate(
            primary_node_id=node_id,
            primary_node_kind=NodeKind.CELL if cell else NodeKind.SITE,
            cause=IncidentCause.FLAPPING,
            window=flap_window,
            region=cell.region if cell else "",
            cell_ids=(node_id,) if cell else (),
            site_ids=(cell.site_id,) if cell else (),
            alarm_ids=tuple(alarm.alarm_id for alarm in alarms),
            severity=0.0,
            confidence=0.7,
            title=f"Flapping alarm source {node_id}",
            summary=(
                f"{flaps} raise/clear transitions inside an hour with no KPI impact; "
                "classified as flapping and suppressed"
            ),
            suppressed_reason=SuppressionReason.FLAPPING.value,
            evidence=tuple(evidence),
            links=tuple(
                CorrelationLink(
                    entity_type="alarm",
                    entity_id=alarm.alarm_id,
                    weight=0.2,
                    rationale="flapping transition",
                )
                for alarm in alarms
            ),
            rule_version=self._policies.rule_version,
        )
        incident = self._incidents.open_or_update(candidate)
        linked = self._apply_links(incident.incident_id, candidate.links)
        _ = window
        return self._record(
            CorrelationOutcome(
                decision=CorrelationDecision.SUPPRESSED,
                candidate=candidate,
                incident_id=incident.incident_id,
                links=linked,
                suppressed_reason=SuppressionReason.FLAPPING.value,
                evidence=candidate.evidence,
                confidence=candidate.confidence,
                note=candidate.summary,
            ),
            action="detect_degradation",
            entity_type="incident",
            entity_id=incident.incident_id,
            inputs={
                "transitions_per_hour": flaps,
                "alarms": len(alarms),
                "kpi_breach": False,
                "threshold": self._policies.correlation.flapping_min_transitions,
            },
        )

    def _suppress(
        self,
        group: _Group,
        window: TimeWindow,
        cause: IncidentCause,
        cover: Any,
        assessment: _Assessment,
    ) -> CorrelationOutcome:
        assessment.evidence.append(
            Evidence(
                kind="maintenance",
                ref=cover.maintenance_id,
                note=(
                    f"approved window {cover.starts_at.isoformat()} -> "
                    f"{cover.ends_at.isoformat()} on {cover.node_kind} {cover.node_id}"
                ),
            ),
        )
        candidate = IncidentCandidate(
            primary_node_id=group.primary_node_id,
            primary_node_kind=group.primary_node_kind,
            cause=IncidentCause.PLANNED_MAINTENANCE,
            window=window,
            region=group.region,
            cell_ids=group.cell_ids,
            site_ids=group.site_ids,
            alarm_ids=tuple(alarm.alarm_id for alarm in group.alarms),
            severity=group.severity,
            confidence=0.9,
            title=f"Planned maintenance on {group.primary_node_kind} {group.primary_node_id}",
            summary=(
                f"degradation {window.start.isoformat()} -> {window.end.isoformat()} falls "
                f"inside approved maintenance {cover.maintenance_id}; suppressed "
                f"(would otherwise have been {cause})"
            ),
            suppressed_reason=SuppressionReason.PLANNED_MAINTENANCE.value,
            evidence=tuple(assessment.evidence),
            links=tuple(assessment.links),
            rule_version=self._policies.rule_version,
        )
        incident = self._incidents.open_or_update(candidate)
        linked = self._apply_links(incident.incident_id, candidate.links)
        return self._record(
            CorrelationOutcome(
                decision=CorrelationDecision.SUPPRESSED,
                candidate=candidate,
                incident_id=incident.incident_id,
                links=linked,
                suppressed_reason=SuppressionReason.PLANNED_MAINTENANCE.value,
                evidence=candidate.evidence,
                confidence=candidate.confidence,
                note=candidate.summary,
            ),
            action="detect_degradation",
            entity_type="incident",
            entity_id=incident.incident_id,
            inputs={
                "maintenance_id": cover.maintenance_id,
                "maintenance_node": cover.node_id,
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
                "severity": round(group.severity, 3),
            },
        )

    # ============================================================ complaint attach

    def _attach(
        self,
        observation: ComplaintObservation,
        incident: IncidentRef,
    ) -> CorrelationOutcome:
        """Link a complaint to an existing incident, counting its dedupe key once."""
        first_of_key = self._links.link(
            incident.incident_id,
            "complaint_dedupe",
            observation.dedupe_token,
            weight=1.0,
            rationale="distinct complaint signature",
        )
        links: list[CorrelationLink] = []
        if first_of_key:
            self._links.link(
                incident.incident_id,
                "complaint",
                observation.complaint_id,
                weight=1.0,
                rationale=f"event time inside incident window, lag {observation.lag}",
            )
            links.append(
                CorrelationLink(
                    entity_type="complaint",
                    entity_id=observation.complaint_id,
                    weight=1.0,
                    rationale="first complaint with this dedupe key",
                ),
            )
        reopen = incident.is_closed
        if reopen:
            # A late complaint for a closed window is evidence the fix did not hold. The
            # engine asks for a reopen rather than creating a second incident.
            incident = self._incidents.reopen(
                incident.incident_id,
                reason=f"late complaint {observation.complaint_id} inside a closed window",
            )
        self._publisher.publish(
            Correlated(
                aggregate_type=AggregateType.COMPLAINT,
                aggregate_id=observation.complaint_id,
                occurred_at=self._clock.now(),
                dedupe_key=f"{observation.complaint_id}|{incident.incident_id}",
                payload={
                    "incident_id": incident.incident_id,
                    "reason": "duplicate" if not first_of_key else "attached",
                    "weight": 1.0 if first_of_key else 0.0,
                    "event_time": observation.event_time.isoformat(),
                },
            ),
        )
        decision = CorrelationDecision.REOPENED if reopen else CorrelationDecision.ATTACHED
        return self._record(
            CorrelationOutcome(
                decision=decision,
                incident_id=incident.incident_id,
                links=tuple(links),
                suppressed_reason=None if first_of_key else SuppressionReason.DUPLICATE.value,
                evidence=(
                    Evidence(
                        kind="complaint",
                        ref=observation.complaint_id,
                        note=(
                            f"event time {observation.event_time.isoformat()} inside "
                            f"{incident.window.start.isoformat()} -> "
                            f"{incident.window.end.isoformat()}"
                        ),
                    ),
                ),
                confidence=0.8 if first_of_key else 0.5,
                reopen_requested=reopen,
                note="duplicate submission" if not first_of_key else "attached by event time",
            ),
            action="correlate_complaint",
            entity_type="complaint",
            entity_id=observation.complaint_id,
            inputs={
                "incident_id": incident.incident_id,
                "event_time": observation.event_time.isoformat(),
                "received_at": observation.received_at.isoformat(),
                "lag_seconds": observation.lag.total_seconds(),
                "dedupe_key": observation.dedupe_token,
                "first_of_dedupe_key": first_of_key,
                "incident_status": incident.status,
            },
        )

    def _match_incident(
        self,
        refs: Sequence[IncidentRef],
        observation: ComplaintObservation,
    ) -> IncidentRef | None:
        """Pick the incident whose *event-time* window covers this complaint."""
        moment = observation.event_time
        live = [
            ref
            for ref in refs
            if not ref.is_suppressed and ref.window.start <= moment < ref.window.end
        ]
        if not live:
            # A closed incident still accepts a late complaint for a short grace period.
            live = [
                ref
                for ref in refs
                if not ref.is_suppressed
                and ref.window.start <= moment < ref.window.end + LATE_ATTACH_GRACE
            ]
        if not live:
            return None
        # Prefer the most specific root: a cell incident beats the site incident above it.
        order = {
            NodeKind.CELL.value: 0,
            NodeKind.SITE.value: 1,
            NodeKind.TRANSMISSION_LINK.value: 2,
            NodeKind.AGGREGATION.value: 3,
            NodeKind.CORE.value: 4,
        }
        return min(live, key=lambda r: (order.get(r.primary_node_kind, 9), r.incident_id))

    def _corroborate(
        self,
        observation: ComplaintObservation,
        window: TimeWindow,
    ) -> tuple[KpiBreach | None, tuple[AlarmRecord, ...]] | None:
        """Complaints alone never raise a network incident; the network must agree."""
        breaches = self._kpis.breaches(
            window,
            "1h",
            cell_ids=[observation.cell_id],
            min_severity=KPI_BREACH_MIN_SEVERITY,
        )
        alarms = tuple(
            alarm
            for alarm in self._alarms.for_element(observation.cell_id, window)
            if alarm.is_significant
        )
        if not breaches and not alarms:
            return None
        return (breaches[0] if breaches else None), alarms

    def _cluster_around(
        self,
        observation: ComplaintObservation,
        window: TimeWindow,
    ) -> ComplaintCluster:
        """Complaints within the policy radius and window of this one."""
        policy = self._policies.correlation
        origin = self._cells.get(observation.cell_id)
        neighbours: tuple[CellInfo, ...]
        if origin is None:
            neighbours = ()
        else:
            neighbours = tuple(
                cell
                for cell in self._cells.by_region(origin.region)
                if cell.point.distance_km(origin.point) <= policy.spatial_radius_km
            )
        cell_ids = tuple(sorted({observation.cell_id, *(cell.cell_id for cell in neighbours)}))
        observations = self._complaints.in_window(window, cell_ids)
        network = [row for row in observations if row.is_network]
        if observation.complaint_id not in {row.complaint_id for row in network}:
            network.append(observation)
        subscribers = sum(cell.subscribers_est for cell in neighbours) or (
            origin.subscribers_est if origin else 0
        )
        return ComplaintCluster(
            cell_ids=cell_ids,
            complaint_ids=tuple(sorted(row.complaint_id for row in network)),
            dedupe_keys=tuple(sorted(row.dedupe_token for row in network)),
            window=window,
            subscribers_est=subscribers,
            radius_km=cluster_radius_km(list(neighbours)),
        )

    def _cluster_from_counts(
        self,
        group: _Group,
        counts: Mapping[str, ComplaintCount],
        window: TimeWindow,
    ) -> ComplaintCluster:
        """Cluster built from the SQL-side counts, so the engine never pages complaints."""
        cells = self._cells.with_geo(group.cell_ids)
        total = 0
        distinct = 0
        for cell_id in group.cell_ids:
            row = counts.get(cell_id)
            if row is None:
                continue
            total += row.total
            distinct += row.distinct
        # The counts come from SQL, so the cluster carries synthetic key placeholders: the
        # engine needs *how many* distinct signatures there were, never their text.
        return ComplaintCluster(
            cell_ids=group.cell_ids,
            complaint_ids=tuple(f"{group.primary_node_id}#{i}" for i in range(total)),
            dedupe_keys=tuple(f"{group.primary_node_id}#{i}" for i in range(distinct)),
            window=window,
            subscribers_est=sum(cell.subscribers_est for cell in cells),
            radius_km=cluster_radius_km(list(cells)),
        )

    # ============================================================== assessment

    def _suppression_for(self, group: _Group, window: TimeWindow) -> Any:
        """An approved window on the element or any ancestor suppresses the candidate."""
        if not self._policies.correlation.maintenance_suppression:
            return None
        chain = [group.primary_node_id, *group.site_ids]
        chain += [node.node_id for node in self._topology.upstream_of(group.primary_node_id)]
        seen: set[str] = set()
        for node_id in chain:
            if node_id in seen:
                continue
            seen.add(node_id)
            for cover in self._maintenance.covering(node_id, window):
                if cover.is_approved and cover.window.overlaps(window):
                    return cover
        return None

    def _silence_signal(self, group: _Group, window: TimeWindow) -> Evidence | None:
        """Silence against a cell's own baseline is evidence, not reassurance."""
        hours = max(window.duration.total_seconds() / 3600.0, 0.25)
        lookback = min(max(hours, SILENCE_BASELINE_MIN_HOURS), SILENCE_BASELINE_MAX_HOURS)
        reference = TimeWindow(
            start=window.start - timedelta(hours=lookback),
            end=window.start,
        )
        baseline = sum(
            self._complaints.baseline_rate(cell_id, reference) for cell_id in group.cell_ids
        )
        if baseline < SILENCE_MIN_BASELINE_PER_HOUR:
            return None
        observed = sum(
            self._complaints.baseline_rate(cell_id, window) for cell_id in group.cell_ids
        )
        if observed >= baseline * SILENCE_DROP_RATIO:
            return None
        return Evidence(
            kind="baseline",
            ref=f"complaint_rate:{group.primary_node_id}",
            note=(
                f"complaint rate fell from {baseline:.2f}/h (previous {lookback:.1f}h) to "
                f"{observed:.2f}/h over {hours:.1f}h while KPIs collapsed: "
                "the channel went quiet with the cells"
            ),
            weight=1.0,
        )

    def _alarm_corroborated(self, group: _Group) -> bool:
        return any(alarm.is_significant for alarm in group.alarms) and bool(group.breaches)

    def _classify(self, group: _Group) -> IncidentCause:
        """Deterministic cause from the signals. The LLM may refine it; it never sets it."""
        if group.primary_node_kind is NodeKind.TRANSMISSION_LINK:
            return IncidentCause.TRANSMISSION_LINK_FAILURE
        worst = group.worst
        availability = worst.get(KpiName.AVAILABILITY.value)
        if any(alarm.alarm_type == "EXT_POWER_LOSS" for alarm in group.alarms):
            return IncidentCause.POWER_FAILURE
        if availability is not None and availability < OUTAGE_AVAILABILITY_PCT:
            return IncidentCause.CELL_OUTAGE
        prb = worst.get(KpiName.PRB_UTILISATION.value)
        downlink = worst.get(KpiName.DL_THROUGHPUT.value)
        congested = prb is not None and prb > _threshold(KpiName.PRB_UTILISATION)
        starved = downlink is not None and downlink < _threshold(KpiName.DL_THROUGHPUT)
        if congested and starved:
            return IncidentCause.CONGESTION
        if group.breaches:
            return IncidentCause.CELL_OUTAGE if group.severity >= 0.8 else IncidentCause.UNKNOWN
        return IncidentCause.UNKNOWN

    def _candidate(
        self,
        group: _Group,
        window: TimeWindow,
        cause: IncidentCause,
        assessment: _Assessment,
        cluster: ComplaintCluster,
        severity: float,
        *,
        silent_sufferer: bool,
    ) -> IncidentCandidate:
        kind = group.primary_node_kind
        title = f"{_cause_title(cause)} on {kind} {group.primary_node_id}"
        summary_parts = [
            f"{len(group.cell_ids)} cell(s) across {len(group.site_ids)} site(s)",
            f"KPI severity {severity:.2f}",
            f"{cluster.distinct_count} distinct complaints",
        ]
        if silent_sufferer and not cluster.distinct_count:
            summary_parts.append("no complaints: silent sufferers")
        if len(group.site_ids) > 1:
            summary_parts.append(f"collapsed onto shared upstream {group.primary_node_id}")
        links = list(assessment.links)
        links += [
            CorrelationLink(
                entity_type="cell",
                entity_id=cell_id,
                weight=1.0,
                rationale="degraded KPIs in the incident window",
            )
            for cell_id in group.cell_ids
        ]
        links += [
            CorrelationLink(
                entity_type="alarm",
                entity_id=alarm.alarm_id,
                weight=0.8 if alarm.is_significant else 0.3,
                rationale=f"{alarm.alarm_type} collapsed into this incident",
            )
            for alarm in group.alarms
        ]
        return IncidentCandidate(
            primary_node_id=group.primary_node_id,
            primary_node_kind=kind,
            cause=cause,
            window=window,
            region=group.region,
            cell_ids=group.cell_ids,
            site_ids=group.site_ids,
            alarm_ids=tuple(alarm.alarm_id for alarm in group.alarms),
            severity=round(severity, 4),
            confidence=round(min(assessment.confidence, 0.99), 4),
            complaint_count=cluster.distinct_count,
            title=title[:120],
            summary="; ".join(summary_parts),
            evidence=tuple(assessment.evidence),
            links=tuple(links),
            rule_version=self._policies.rule_version,
        )

    def _add_kpi_evidence(self, group: _Group, assessment: _Assessment) -> None:
        for breach in group.breaches[:10]:
            assessment.evidence.append(
                Evidence(
                    kind="kpi",
                    ref=f"kpi_cell:{breach.cell_id}:{breach.window.start.isoformat()}",
                    note=(
                        f"{breach.breached_buckets}/{breach.buckets} buckets degraded, "
                        f"severity {breach.severity:.2f}, "
                        f"breached {', '.join(k.value for k in breach.breached_kpis[:6])}"
                    ),
                    weight=breach.severity,
                ),
            )

    def _add_alarm_evidence(self, group: _Group, assessment: _Assessment) -> None:
        collapse = self._policies.correlation.alarm_storm_collapse_minutes
        if len(group.alarms) > 1:
            assessment.notes.append(
                f"{len(group.alarms)} alarms collapsed within {collapse} minutes",
            )
        for alarm in group.alarms[:10]:
            assessment.evidence.append(
                Evidence(
                    kind="alarm",
                    ref=alarm.alarm_id,
                    note=(
                        f"{alarm.alarm_type} ({alarm.severity}) on {alarm.node_kind} "
                        f"{alarm.node_id} raised {alarm.raised_at.isoformat()}"
                    ),
                    weight=0.8 if alarm.is_significant else 0.3,
                ),
            )

    def _add_topology_evidence(self, group: _Group, assessment: _Assessment) -> None:
        if group.primary_node_kind is NodeKind.CELL:
            return
        assessment.evidence.append(
            Evidence(
                kind="topology",
                ref=group.primary_node_id,
                note=(
                    f"{len(group.cell_ids)} degraded cells on {len(group.site_ids)} sites "
                    f"share upstream {group.primary_node_kind} {group.primary_node_id}"
                ),
                weight=1.0,
            ),
        )
        assessment.links.append(
            CorrelationLink(
                entity_type="topology",
                entity_id=group.primary_node_id,
                weight=1.0,
                rationale="shared upstream element",
            ),
        )

    # ================================================================= plumbing

    def _apply_links(
        self,
        incident_id: str,
        links: Sequence[CorrelationLink],
    ) -> tuple[CorrelationLink, ...]:
        applied: list[CorrelationLink] = []
        for link in links:
            if self._links.link(
                incident_id,
                link.entity_type,
                link.entity_id,
                weight=link.weight,
                rationale=link.rationale,
            ):
                applied.append(link)
        return tuple(applied)

    def _publish_degradation(self, breach: KpiBreach) -> None:
        self._publisher.publish(
            DegradationDetected(
                aggregate_type=AggregateType.CELL,
                aggregate_id=breach.cell_id,
                occurred_at=self._clock.now(),
                dedupe_key=f"{breach.cell_id}|{breach.window.start.isoformat()}",
                payload={
                    "cell_id": breach.cell_id,
                    "kpi": [kpi.value for kpi in breach.breached_kpis],
                    "value": breach.worst,
                    "threshold": {kpi.value: _threshold(kpi) for kpi in breach.breached_kpis},
                    "severity": breach.severity,
                    "window_start": breach.window.start.isoformat(),
                    "window_end": breach.window.end.isoformat(),
                },
            ),
        )

    def _inputs(
        self,
        group: _Group,
        window: TimeWindow,
        cluster: ComplaintCluster,
        severity: float,
    ) -> dict[str, Any]:
        policy = self._policies.correlation
        return {
            "primary_node_id": group.primary_node_id,
            "primary_node_kind": str(group.primary_node_kind),
            "cells": len(group.cell_ids),
            "sites": len(group.site_ids),
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "kpi_severity": round(severity, 3),
            "distinct_complaints": cluster.distinct_count,
            "complaints_per_1k_subs": round(cluster.per_1k_subs, 3),
            "subscribers_est": cluster.subscribers_est,
            "alarms": len(group.alarms),
            "policy": {
                "spatial_radius_km": policy.spatial_radius_km,
                "time_window_minutes": policy.time_window_minutes,
                "min_complaints_for_incident": policy.min_complaints_for_incident,
                "min_complaints_per_1k_subs": policy.min_complaints_per_1k_subs,
                "silent_sufferer_kpi_threshold": policy.silent_sufferer_kpi_threshold,
            },
        }

    def _record(
        self,
        outcome: CorrelationOutcome,
        *,
        action: str,
        entity_type: str,
        entity_id: str,
        inputs: dict[str, Any],
    ) -> CorrelationOutcome:
        """Exactly one decision row per outcome, stamped with the policy version."""
        self._decisions.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            outcome=str(outcome.decision),
            inputs={**inputs, "suppressed_reason": outcome.suppressed_reason},
            evidence=outcome.evidence,
            confidence=outcome.confidence or None,
            rule_version=self._policies.rule_version,
        )
        _log.debug(
            "correlation_decision",
            extra={"action": action, "outcome": str(outcome.decision), "entity_id": entity_id},
        )
        return outcome


# --------------------------------------------------------------------- helpers


def _threshold(kpi: KpiName) -> float:

    return KPI_POLARITY[kpi][1]


def _lower_is_worse(kpi_value: str) -> bool:

    try:
        return KPI_POLARITY[KpiName(kpi_value)][0]
    except ValueError:  # pragma: no cover - defensive: unknown column name
        return True


def _overlaps(alarm: AlarmRecord, window: TimeWindow) -> bool:
    """An alarm still open at the window start counts, even if it was raised before it."""
    end = alarm.cleared_at or window.end
    return alarm.raised_at < window.end and end > window.start


def _alarm_window(alarms: Sequence[AlarmRecord]) -> TimeWindow:
    if not alarms:
        raise ValueError("a group needs either KPI breaches or alarms")
    start = min(alarm.raised_at for alarm in alarms)
    end = max((alarm.cleared_at or alarm.raised_at) for alarm in alarms)
    return TimeWindow(start=start, end=max(end, start + timedelta(minutes=1)))


def _max_transitions_per_hour(moments: Sequence[Any]) -> int:
    """Largest number of raise/clear transitions inside any one-hour sliding window."""
    ordered = sorted(moments)
    best = 0
    left = 0
    for right, moment in enumerate(ordered):
        while moment - ordered[left] > FLAPPING_WINDOW:
            left += 1
        best = max(best, right - left + 1)
    return best


def _cause_title(cause: IncidentCause) -> str:
    return cause.value.replace("_", " ").capitalize()


__all__ = [
    "KPI_BREACH_MIN_SEVERITY",
    "UPSTREAM_DEGRADED_SHARE",
    "CorrelationService",
]
