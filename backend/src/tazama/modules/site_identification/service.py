"""Site identification: which cell was the customer on, and how sure are we?

This is the hardest join in the product (PRD section 8), and it is solved with three cheap,
independent signals rather than one clever one:

  1. **Recent serving cell** — what the network itself last saw. Strongest signal, and the
     only one that decays: a cell from ten days ago says nothing about where someone is
     standing now, so it is ignored beyond a week.
  2. **Stated location** — what the customer wrote, matched against our own site and cell
     names by token overlap. Cheap, fuzzy, and weaker the vaguer the match: a bare region
     name locates a region, not a cell.
  3. **Topology and geography** — cells under the same site, siblings, and shared upstream
     elements. Used to *break ties* and to widen a region-only match, never to invent an
     answer on its own.

Every signal that contributed is returned in the evidence with its weight, and the
runner-ups come back as candidates, because a confidence without a reason is unauditable.
Below `policies.confidence.site_identification_min` the service refuses to guess: it
publishes `HumanReviewRequested` instead of `SiteIdentified` and writes no cell.

`stated_location` is **untrusted text**. It is tokenised and compared against our own
names. It never reaches a query as text, and nothing here branches on its content.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tazama.core.clock import Clock, ensure_utc
from tazama.core.ids import deterministic_id, get_correlation_id
from tazama.core.logging import get_logger
from tazama.core.policy import Policies, get_policies
from tazama.domain.events import AggregateType, HumanReviewRequested, SiteIdentified
from tazama.domain.value_objects import confidence_band
from tazama.modules.site_identification.models import (
    METHOD_REGION_FALLBACK,
    METHOD_SERVING_CELL,
    METHOD_SOURCE_PROVIDED,
    METHOD_STATED_LOCATION,
    METHOD_TOPOLOGY,
    METHOD_UNRESOLVED,
    REASON_AMBIGUOUS_LOCATION,
    REASON_LOW_CONFIDENCE,
    REASON_NO_CANDIDATE,
    REASON_NO_SERVING_CELL,
    REASON_REGION_ONLY,
    REASON_ROAMING,
    REASON_STALE_SERVING_CELL,
    REASON_UNKNOWN_CELL,
    Candidate,
    ComplaintLocation,
    SignalEvidence,
    SiteGuess,
    SiteIdConfig,
    serving_cell_decay,
    token_overlap,
    tokenise_location,
)
from tazama.modules.site_identification.ports import (
    DecisionLog,
    EventPublisher,
    SiteDirectory,
    SubscriberLookup,
    TopologyReader,
)

_log = get_logger(__name__)

ACTION_IDENTIFY_SITE = "identify_site"
OUTCOME_RESOLVED = "resolved"
OUTCOME_HUMAN_REVIEW = "human_review"

#: Queue name carried on `HumanReviewRequested` so the review UI can route it.
REVIEW_QUEUE = "site_identification"


@dataclass(slots=True)
class _Signals:
    """Mutable accumulator while the three signals are gathered."""

    evidence: list[SignalEvidence] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)

    def note(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Stated:
    """What the stated location produced."""

    best: Candidate | None = None
    site_id: str | None = None
    region: str | None = None
    distinct_sites: int = 0
    region_only: bool = False


class SiteIdentificationService:
    """Resolves a complaint to a cell and a site, with a confidence and an explanation."""

    def __init__(
        self,
        *,
        subscribers: SubscriberLookup,
        directory: SiteDirectory,
        topology: TopologyReader,
        publisher: EventPublisher,
        decisions: DecisionLog,
        clock: Clock,
        config: SiteIdConfig | None = None,
        policies: Policies | None = None,
    ) -> None:
        self._subscribers = subscribers
        self._directory = directory
        self._topology = topology
        self._publisher = publisher
        self._decisions = decisions
        self._clock = clock
        self._config = config or SiteIdConfig()
        self._policies = policies

    # ------------------------------------------------------------------ public

    def identify(self, complaint: ComplaintLocation) -> SiteGuess:
        """Resolve one complaint. Always returns a guess; never raises on bad data."""
        guess = (
            self._from_source(complaint)
            if (complaint.cell_id or "").strip()
            else self._combine(complaint)
        )
        return self.finalise(complaint, guess)

    @property
    def threshold(self) -> float:
        """Confidence below which a human decides (config/policies.yaml)."""
        return self._resolved_policies().confidence.site_identification_min

    # ----------------------------------------------------- signal 0: the source

    def _from_source(self, complaint: ComplaintLocation) -> SiteGuess:
        """The source system already told us the cell. Trust it: the network knows best."""
        cell_id = (complaint.cell_id or "").strip()
        cell = self._directory.get_cell(cell_id)
        confidence = self._config.source_provided_confidence
        reasons: list[str] = []
        evidence = [
            SignalEvidence(
                kind="complaint",
                ref=complaint.complaint_id,
                note=f"source system supplied cell {cell_id}",
                weight=confidence,
            ),
        ]
        if cell is None:
            # Schema drift or a cell decommissioned since: still trust the id, but say so.
            reasons.append(REASON_UNKNOWN_CELL)
            evidence.append(
                SignalEvidence(
                    kind="topology",
                    ref=cell_id,
                    note="cell id is not in the site directory",
                    weight=0.0,
                ),
            )
        return SiteGuess(
            complaint_id=complaint.complaint_id,
            cell_id=cell_id,
            site_id=cell.site_id if cell else None,
            region=cell.region if cell else complaint.region,
            confidence=confidence,
            method=METHOD_SOURCE_PROVIDED,
            evidence=tuple(evidence),
            candidates=(
                Candidate(
                    cell_id=cell_id,
                    site_id=cell.site_id if cell else None,
                    score=confidence,
                    source=METHOD_SOURCE_PROVIDED,
                ),
            ),
            reasons=tuple(reasons),
        )

    # ---------------------------------------------- signal 1: recent serving cell

    def _serving(self, complaint: ComplaintLocation, signals: _Signals) -> Candidate | None:
        """The subscriber's last serving cell, decayed by age.

        A fresh `last_cell_id` beats a stale `home_cell_id`, which is what makes "subscriber
        moved between cells" come out right. A roaming subscriber has no usable serving
        cell at all: their last seen cell is on somebody else's network.
        """
        if not complaint.subscriber_id:
            signals.note(REASON_NO_SERVING_CELL)
            return None
        position = self._subscribers.get(complaint.subscriber_id)
        if position is None:
            signals.note(REASON_NO_SERVING_CELL)
            return None
        if position.is_roaming:
            signals.note(REASON_ROAMING)
            signals.evidence.append(
                SignalEvidence(
                    kind="subscriber",
                    ref=position.id,
                    note="roaming: serving cell is not ours, signal discarded",
                    weight=0.0,
                ),
            )
            return self._home_cell_candidate(position.home_cell_id, signals)

        if position.last_cell_id and position.last_seen_at is not None:
            age = ensure_utc(complaint.received_at) - ensure_utc(position.last_seen_at)
            decay = serving_cell_decay(age, self._config)
            if decay <= 0.0:
                signals.note(REASON_STALE_SERVING_CELL)
                signals.evidence.append(
                    SignalEvidence(
                        kind="subscriber",
                        ref=position.last_cell_id,
                        note=f"serving cell {int(age.total_seconds() // 3600)}h old: ignored",
                        weight=0.0,
                    ),
                )
                return self._home_cell_candidate(position.home_cell_id, signals)
            score = self._config.serving_cell_weight * decay
            cell = self._directory.get_cell(position.last_cell_id)
            signals.evidence.append(
                SignalEvidence(
                    kind="subscriber",
                    ref=position.last_cell_id,
                    note=(
                        f"last serving cell, {int(age.total_seconds() // 60)} minutes old, "
                        f"decay {decay:.2f}"
                    ),
                    weight=score,
                ),
            )
            candidate = Candidate(
                cell_id=position.last_cell_id,
                site_id=cell.site_id if cell else None,
                score=score,
                source=METHOD_SERVING_CELL,
                note="recent serving cell",
            )
            signals.candidates.append(candidate)
            return candidate

        signals.note(REASON_NO_SERVING_CELL)
        return self._home_cell_candidate(position.home_cell_id, signals)

    def _home_cell_candidate(
        self,
        home_cell_id: str | None,
        signals: _Signals,
    ) -> Candidate | None:
        """Last resort: where this subscriber usually is. Never enough on its own."""
        if not home_cell_id:
            return None
        cell = self._directory.get_cell(home_cell_id)
        candidate = Candidate(
            cell_id=home_cell_id,
            site_id=cell.site_id if cell else None,
            score=self._config.home_cell_weight,
            source=METHOD_SERVING_CELL,
            note="home cell (no usable recent observation)",
        )
        signals.evidence.append(
            SignalEvidence(
                kind="subscriber",
                ref=home_cell_id,
                note="home cell used as fallback",
                weight=candidate.score,
            ),
        )
        signals.candidates.append(candidate)
        return candidate

    # ------------------------------------------------- signal 2: stated location

    def _stated(self, complaint: ComplaintLocation, signals: _Signals) -> _Stated:
        """Match the customer's words against our own names, by token overlap."""
        tokens = tokenise_location(complaint.stated_location)
        if not tokens:
            return _Stated()
        matches = self._directory.search_by_name(
            tokens,
            limit=self._config.max_name_matches,
        )
        scored: list[tuple[int, float, Any]] = []
        for match in matches:
            overlap = token_overlap(tokens, match.tokens)
            if overlap == 0:
                continue
            scored.append(
                (overlap, self._name_score(match.kind, overlap, len(match.tokens)), match),
            )
        if not scored:
            return _Stated()
        # Overlap first, score second: the match that accounts for more of what the
        # customer actually wrote wins, so "Westlands Mast Sector A" beats the site it
        # sits on, while a bare "Westlands Mast" still resolves to the site.
        scored.sort(key=lambda row: (-row[0], -row[1], row[2].id))

        place_matches = [(score, m) for _, score, m in scored if m.kind in ("site", "cell")]
        if not place_matches:
            return self._region_only(scored[0][2], scored[0][1], signals)

        distinct_sites = {m.site_id or m.id for _, m in place_matches}
        if len(distinct_sites) > 1:
            signals.note(REASON_AMBIGUOUS_LOCATION)

        best_score, best = place_matches[0]
        signals.evidence.append(
            SignalEvidence(
                kind="topology",
                ref=best.id,
                note=(
                    f"stated location matched {best.kind} {best.name!r} on "
                    f"{token_overlap(tokens, best.tokens)} token(s)"
                ),
                weight=best_score,
            ),
        )

        # Record the runner-ups so a reviewer sees what was rejected and why.
        for score, match in place_matches[1 : self._config.max_candidates]:
            signals.candidates.append(
                Candidate(
                    cell_id=match.id if match.kind == "cell" else "",
                    site_id=match.site_id or (match.id if match.kind == "site" else None),
                    score=score,
                    source=METHOD_STATED_LOCATION,
                    note=f"also matched {match.kind} {match.name!r}",
                ),
            )

        if best.kind == "cell":
            candidate = Candidate(
                cell_id=best.id,
                site_id=best.site_id,
                score=best_score,
                source=METHOD_STATED_LOCATION,
                note=f"name match on cell {best.name!r}",
            )
            signals.candidates.append(candidate)
            return _Stated(
                best=candidate,
                site_id=best.site_id,
                region=best.region,
                distinct_sites=len(distinct_sites),
            )

        # A site name was matched. One cell under it is an answer; several sectors are not,
        # so the site is returned and the cell is left to the tie-break or to a human.
        cells = self._topology.cells_under_site(best.id, limit=self._config.max_candidates)
        if len(cells) == 1:
            candidate = Candidate(
                cell_id=cells[0].id,
                site_id=best.id,
                score=best_score,
                source=METHOD_STATED_LOCATION,
                note=f"only cell under site {best.name!r}",
            )
            signals.candidates.append(candidate)
            return _Stated(
                best=candidate,
                site_id=best.id,
                region=best.region,
                distinct_sites=len(distinct_sites),
            )
        for cell in cells:
            signals.candidates.append(
                Candidate(
                    cell_id=cell.id,
                    site_id=best.id,
                    score=best_score * self._config.single_token_factor,
                    source=METHOD_STATED_LOCATION,
                    note=f"sector of matched site {best.name!r}",
                ),
            )
        return _Stated(
            best=None,
            site_id=best.id,
            region=best.region,
            distinct_sites=len(distinct_sites),
        )

    def _region_only(self, match: Any, score: float, signals: _Signals) -> _Stated:
        """The words named a region and nothing narrower. Widen, do not guess."""
        region = match.region or match.id
        signals.note(REASON_REGION_ONLY)
        signals.evidence.append(
            SignalEvidence(
                kind="topology",
                ref=str(region),
                note="stated location matched a region name only",
                weight=score,
            ),
        )
        cells = self._directory.cells_for_region(
            str(region),
            limit=self._config.max_region_cells,
        )
        for cell in cells[: self._config.max_candidates]:
            signals.candidates.append(
                Candidate(
                    cell_id=cell.id,
                    site_id=cell.site_id,
                    score=self._config.region_weight,
                    source=METHOD_REGION_FALLBACK,
                    note=f"cell in region {region}",
                ),
            )
        return _Stated(best=None, site_id=None, region=str(region), region_only=True)

    # ------------------------------------------------- signal 3: topology / ties

    def _agree(self, serving: Candidate, stated: _Stated, signals: _Signals) -> float:
        """Bonus when an independent signal points at the same place.

        Direct agreement (same site) is worth the full bonus; a shared upstream element is
        worth half, because it means "the same part of the network" rather than "the same
        mast".
        """
        if stated.site_id is None:
            return 0.0
        if serving.site_id and serving.site_id == stated.site_id:
            signals.evidence.append(
                SignalEvidence(
                    kind="topology",
                    ref=stated.site_id,
                    note="serving cell sits under the site named by the customer",
                    weight=self._config.topology_agreement_bonus,
                ),
            )
            return self._config.topology_agreement_bonus
        if serving.site_id and self._shares_upstream(serving.site_id, stated.site_id):
            bonus = self._config.topology_agreement_bonus / 2
            signals.evidence.append(
                SignalEvidence(
                    kind="topology",
                    ref=stated.site_id,
                    note="serving cell and named site share an upstream element",
                    weight=bonus,
                ),
            )
            return bonus
        return 0.0

    def _shares_upstream(self, left: str, right: str) -> bool:
        """Recursive topology walk, bounded. Used only to break a tie."""
        left_hops = {hop.node_id for hop in self._topology.upstream_of(left)}
        if not left_hops:
            return False
        return any(hop.node_id in left_hops for hop in self._topology.upstream_of(right))

    def _break_tie(self, serving: Candidate, stated: _Stated, signals: _Signals) -> Candidate:
        """Prefer the serving cell when it is a sector of the site the customer named."""
        if stated.site_id is None or serving.site_id == stated.site_id:
            return serving
        siblings = self._topology.cells_under_site(
            stated.site_id,
            limit=self._config.max_candidates,
        )
        if any(cell.id == serving.cell_id for cell in siblings):
            signals.evidence.append(
                SignalEvidence(
                    kind="topology",
                    ref=serving.cell_id,
                    note="serving cell is a sector of the named site",
                    weight=self._config.topology_agreement_bonus,
                ),
            )
            return Candidate(
                cell_id=serving.cell_id,
                site_id=stated.site_id,
                score=min(1.0, serving.score + self._config.topology_agreement_bonus),
                source=METHOD_TOPOLOGY,
                note="serving cell confirmed by the named site",
            )
        return serving

    # --------------------------------------------------------------- combination

    def _combine(self, complaint: ComplaintLocation) -> SiteGuess:
        signals = _Signals()
        serving = self._serving(complaint, signals)
        stated = self._stated(complaint, signals)

        ambiguity = self._ambiguity_factor(stated.distinct_sites)
        stated_score = stated.best.score * ambiguity if stated.best else 0.0
        serving_score = serving.score if serving else 0.0

        chosen: Candidate | None = None
        method = METHOD_UNRESOLVED
        confidence = 0.0
        site_id: str | None = None
        region = stated.region or complaint.region

        if serving is not None and serving_score >= stated_score:
            chosen = self._break_tie(serving, stated, signals)
            confidence = min(1.0, chosen.score + self._agree(chosen, stated, signals))
            method = chosen.source
            site_id = chosen.site_id
        elif stated.best is not None:
            chosen = stated.best
            confidence = min(1.0, stated_score)
            method = METHOD_STATED_LOCATION
            site_id = chosen.site_id
        elif stated.site_id is not None:
            # A site was named but its sectors are indistinguishable: a site-level answer.
            confidence = min(1.0, self._config.site_name_weight * ambiguity)
            method = METHOD_STATED_LOCATION
            site_id = stated.site_id
        elif stated.region_only:
            confidence = self._config.region_weight
            method = METHOD_REGION_FALLBACK

        if chosen is None and site_id is None and not stated.region_only:
            signals.note(REASON_NO_CANDIDATE)
            confidence = 0.0
            method = METHOD_UNRESOLVED

        if site_id is None and chosen is not None and chosen.site_id is None:
            resolved = self._directory.get_cell(chosen.cell_id)
            site_id = resolved.site_id if resolved else None
            region = resolved.region if resolved else region

        ranked = self._rank(signals.candidates, chosen)
        return SiteGuess(
            complaint_id=complaint.complaint_id,
            cell_id=chosen.cell_id if chosen and chosen.cell_id else None,
            site_id=site_id,
            region=region,
            confidence=round(max(0.0, min(1.0, confidence)), 4),
            method=method,
            evidence=tuple(signals.evidence),
            candidates=ranked,
            reasons=tuple(signals.reasons),
        )

    def _ambiguity_factor(self, distinct_sites: int) -> float:
        """Several equally-plausible places means less confidence, not a coin toss."""
        if distinct_sites <= 1:
            return 1.0
        penalty = self._config.ambiguity_penalty_per_extra * (distinct_sites - 1)
        return max(self._config.min_ambiguity_factor, 1.0 - penalty)

    def _rank(
        self,
        candidates: list[Candidate],
        chosen: Candidate | None,
    ) -> tuple[Candidate, ...]:
        """Deduplicate by cell, best score first, chosen cell first, bounded."""
        best: dict[str, Candidate] = {}
        for candidate in candidates:
            if not candidate.cell_id:
                continue
            current = best.get(candidate.cell_id)
            if current is None or candidate.score > current.score:
                best[candidate.cell_id] = candidate
        if chosen is not None and chosen.cell_id:
            best[chosen.cell_id] = chosen
        ordered = sorted(best.values(), key=lambda c: (-c.score, c.cell_id))
        if chosen is not None and chosen.cell_id:
            ordered.sort(key=lambda c: c.cell_id != chosen.cell_id)
        return tuple(ordered[: self._config.max_candidates])

    def _name_score(self, kind: str, overlap: int, name_tokens: int) -> float:
        """Score a name match.

        Three things make a match better: what kind of thing was named (a site beats a
        region), how many tokens matched (two beats one — "Westlands Mast" is a place,
        "Mast" is a word), and how much of the stored name was covered, so an exact match
        outranks a partial one.
        """
        base = {
            "site": self._config.site_name_weight,
            "cell": self._config.cell_name_weight,
        }.get(kind, self._config.region_weight)
        factor = 1.0 if overlap >= 2 else self._config.single_token_factor
        precision = min(1.0, overlap / max(name_tokens, 1))
        return base * factor * (0.6 + 0.4 * precision)

    # ------------------------------------------------------------------ finalise

    def finalise(self, complaint: ComplaintLocation, guess: SiteGuess) -> SiteGuess:
        """Apply the confidence policy, publish the right event, record the decision.

        Public because the policy gate is the part that must be provable on its own: a
        caller (or a test) can hand it any guess and see the same refusal.
        """
        needs_human = guess.cell_id is None or guess.confidence < self.threshold
        reasons = list(guess.reasons)
        if guess.confidence < self.threshold and REASON_LOW_CONFIDENCE not in reasons:
            reasons.append(REASON_LOW_CONFIDENCE)
        if guess.cell_id is None and REASON_NO_CANDIDATE not in reasons:
            reasons.append(REASON_NO_CANDIDATE)

        final = SiteGuess(
            complaint_id=guess.complaint_id,
            # A low-confidence cell is not written anywhere: a human decides instead. The
            # site is kept either way — it is context for the reviewer, not an answer the
            # pipeline may act on, and `needs_human` is what gates acting on it.
            cell_id=None if needs_human else guess.cell_id,
            site_id=guess.site_id,
            region=guess.region,
            confidence=guess.confidence,
            method=guess.method,
            needs_human=needs_human,
            evidence=guess.evidence,
            candidates=guess.candidates,
            reasons=tuple(reasons),
        )

        inputs: dict[str, Any] = {
            "complaint_id": complaint.complaint_id,
            "subscriber_id": complaint.subscriber_id,
            "has_stated_location": bool((complaint.stated_location or "").strip()),
            "stated_location_tokens": list(tokenise_location(complaint.stated_location)),
            "source_cell_id": complaint.cell_id,
            "region": complaint.region,
            "received_at": ensure_utc(complaint.received_at).isoformat(),
            "method": final.method,
            "confidence": final.confidence,
            "confidence_band": str(confidence_band(final.confidence)),
            "threshold": self.threshold,
            "candidate_count": len(final.candidates),
            "reasons": list(final.reasons),
        }

        if needs_human:
            self._publisher.publish(
                HumanReviewRequested(
                    aggregate_type=AggregateType.COMPLAINT,
                    aggregate_id=complaint.complaint_id,
                    occurred_at=self._clock.now(),
                    dedupe_key=deterministic_id("HRV", complaint.complaint_id, REVIEW_QUEUE),
                    correlation_id=get_correlation_id(),
                    payload={
                        "reason": final.reasons[0] if final.reasons else REASON_LOW_CONFIDENCE,
                        "confidence": final.confidence,
                        "queue": REVIEW_QUEUE,
                        **final.to_payload(),
                    },
                ),
            )
        else:
            self._publisher.publish(
                SiteIdentified(
                    aggregate_type=AggregateType.COMPLAINT,
                    aggregate_id=complaint.complaint_id,
                    occurred_at=self._clock.now(),
                    dedupe_key=deterministic_id("SID", complaint.complaint_id),
                    correlation_id=get_correlation_id(),
                    payload=final.to_payload(),
                ),
            )

        self._decisions.record(
            action=ACTION_IDENTIFY_SITE,
            entity_type="complaint",
            entity_id=complaint.complaint_id,
            outcome=OUTCOME_HUMAN_REVIEW if needs_human else OUTCOME_RESOLVED,
            inputs=inputs,
            evidence=final.evidence,
            confidence=final.confidence,
            rule_version=self._resolved_policies().rule_version,
        )
        _log.info(
            "site_identified",
            extra={
                "complaint_id": complaint.complaint_id,
                "method": final.method,
                "confidence": final.confidence,
                "needs_human": needs_human,
            },
        )
        return final

    def _resolved_policies(self) -> Policies:
        if self._policies is None:
            self._policies = get_policies()
        return self._policies
