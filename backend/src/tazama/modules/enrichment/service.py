"""Enrichment: give a complaint a meaning before anything acts on it.

The service is thin on purpose. It owns the policy decision — is this classification
confident enough to act on, or does a human need to see it — and leaves the classification
itself to a port, so Phase 4 can swap an LLM in front of the rules without touching this
file or the pipeline.
"""

from __future__ import annotations

from tazama.core.clock import Clock
from tazama.core.logging import get_logger
from tazama.core.policy import Policies, get_policies
from tazama.domain.events import AggregateType, ComplaintEnriched, HumanReviewRequested
from tazama.modules.enrichment.models import EnrichmentInput, EnrichmentResult
from tazama.modules.enrichment.ports import Classifier, DecisionLog, EventPublisher
from tazama.modules.enrichment.rules import RulesClassifier

_log = get_logger(__name__)

ENTITY_TYPE = "complaint"


class EnrichmentService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        decisions: DecisionLog,
        clock: Clock,
        classifier: Classifier | None = None,
        fallback: Classifier | None = None,
        policies: Policies | None = None,
    ) -> None:
        self._publisher = publisher
        self._decisions = decisions
        self._clock = clock
        # Default to the rules classifier so the service works with no model configured.
        self._classifier = classifier or RulesClassifier()
        self._fallback = fallback or RulesClassifier()
        self._policies = policies

    @property
    def policies(self) -> Policies:
        return self._policies or get_policies()

    @property
    def threshold(self) -> float:
        """Below this the classification is a suggestion, not a fact."""
        return self.policies.confidence.classification_min

    def enrich(self, inbound: EnrichmentInput) -> EnrichmentResult:
        """Classify, decide whether it is actionable, record, publish."""
        result = self._classify(inbound)
        result = self._gate(result)

        self._decisions.record(
            action="enrich",
            entity_type=ENTITY_TYPE,
            entity_id=inbound.complaint_id,
            outcome=str(result.category),
            inputs={
                "channel": inbound.channel,
                "method": result.method,
                "language": str(result.language),
                "urgency": result.urgency,
                "sentiment": result.sentiment,
                "is_network": result.is_network,
                "needs_human": result.needs_human,
                "threshold": self.threshold,
                # The scores, not the text: an audit row never carries customer wording.
                "scores": [score.explain() for score in result.scores],
                "reasons": list(result.reasons),
            },
            confidence=result.confidence,
            rule_version=self.policies.rule_version,
        )

        now = self._clock.now()
        self._publisher.publish(
            ComplaintEnriched(
                aggregate_type=AggregateType.COMPLAINT,
                aggregate_id=inbound.complaint_id,
                occurred_at=now,
                dedupe_key=f"{inbound.complaint_id}:enriched:{result.category}",
                payload=result.to_payload(),
            ),
        )
        if result.needs_human:
            self._publisher.publish(
                HumanReviewRequested(
                    aggregate_type=AggregateType.COMPLAINT,
                    aggregate_id=inbound.complaint_id,
                    occurred_at=now,
                    dedupe_key=f"{inbound.complaint_id}:review:classification",
                    payload={
                        "reason": "classification confidence below threshold",
                        "confidence": result.confidence,
                        "queue": "care_back_office",
                    },
                ),
            )

        _log.info(
            "complaint_enriched",
            extra={
                "complaint_id": inbound.complaint_id,
                "category": str(result.category),
                "language": str(result.language),
                "confidence": result.confidence,
                "method": result.method,
                "needs_human": result.needs_human,
            },
        )
        return result

    # ----------------------------------------------------------- internals

    def _classify(self, inbound: EnrichmentInput) -> EnrichmentResult:
        """Primary classifier, falling back on failure.

        A model that times out or returns nonsense must not stop a complaint from being
        processed — the fallback is deterministic and always answers.
        """
        try:
            return self._classifier.classify(inbound)
        except Exception as exc:
            _log.warning(
                "classifier_failed_using_fallback",
                extra={
                    "complaint_id": inbound.complaint_id,
                    "method": getattr(self._classifier, "method", "unknown"),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            degraded = self._fallback.classify(inbound)
            return EnrichmentResult(
                complaint_id=degraded.complaint_id,
                category=degraded.category,
                language=degraded.language,
                is_network=degraded.is_network,
                urgency=degraded.urgency,
                sentiment=degraded.sentiment,
                confidence=degraded.confidence,
                method=f"{degraded.method}:fallback",
                scores=degraded.scores,
                reasons=(*degraded.reasons, "primary classifier unavailable"),
                needs_human=degraded.needs_human,
            )

    def _gate(self, result: EnrichmentResult) -> EnrichmentResult:
        """Apply the policy threshold. Low confidence is a routing decision, not a label.

        The threshold is stated even when the classifier already asked for a human: the
        reviewer needs to know *which* rule routed the complaint to them, and "the
        classifier was unsure" and "it scored below the policy floor" are different facts.
        """
        if result.confidence >= self.threshold:
            return result
        return EnrichmentResult(
            complaint_id=result.complaint_id,
            category=result.category,
            language=result.language,
            is_network=result.is_network,
            urgency=result.urgency,
            sentiment=result.sentiment,
            confidence=result.confidence,
            method=result.method,
            scores=result.scores,
            reasons=(
                *result.reasons,
                f"confidence {result.confidence:.2f} below the {self.threshold:.2f} floor",
            ),
            needs_human=True,
        )
