"""Enrichment ports.

The classifier itself is a port, so Phase 4 can put an LLM-backed implementation behind it
without touching this module: the rules classifier becomes the fallback, which is exactly
what the AI rules require (the product keeps working with the model down).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from tazama.domain.events import DomainEvent
from tazama.modules.enrichment.models import EnrichmentInput, EnrichmentResult


@runtime_checkable
class Classifier(Protocol):
    """Turns complaint text into a category, language, urgency and sentiment.

    An implementation must be *total*: it answers for every input, including empty text and
    text in a language it does not know, rather than raising. An unanswerable complaint is
    `OTHER` with low confidence and `needs_human`, which the pipeline knows how to handle.
    """

    @property
    def method(self) -> str:
        """How this classifier labels — `rules`, `model`, `llm`. Stored on the row."""
        ...

    def classify(self, inbound: EnrichmentInput) -> EnrichmentResult: ...


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: DomainEvent) -> bool: ...


@runtime_checkable
class DecisionLog(Protocol):
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
    ) -> None: ...
