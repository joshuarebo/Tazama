"""Enrichment DTOs: what a complaint means, before anything acts on it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tazama.domain.value_objects import ComplaintCategory, Language


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrichmentInput:
    """One complaint to classify. `text` is **untrusted**: it is matched against our own
    vocabulary and never interpreted as an instruction."""

    complaint_id: str
    text: str
    channel: str
    declared_language: Language | None = None
    stated_location: str | None = None


@dataclass(frozen=True, slots=True)
class CategoryScore:
    category: ComplaintCategory
    score: float
    matched: tuple[str, ...]
    #: True when nothing but suggestive words matched. The label still names the right
    #: family, but it is a lexical guess, so it never counts as a confident classification
    #: however many of those words appear.
    weak_only: bool = False

    def explain(self) -> str:
        qualifier = " [suggestive terms only]" if self.weak_only else ""
        return (
            f"{self.category}: {self.score:.2f} "
            f"({', '.join(self.matched) or 'no terms'}){qualifier}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EnrichmentResult:
    """The classification, with the evidence that produced it.

    `method` says which classifier answered — `rules` here, `llm` or `model` in Phase 4 —
    so a stored row always discloses how it was labelled.
    """

    complaint_id: str
    category: ComplaintCategory
    language: Language
    is_network: bool
    urgency: int
    sentiment: str
    confidence: float
    method: str = "rules"
    scores: tuple[CategoryScore, ...] = ()
    reasons: tuple[str, ...] = ()
    needs_human: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        if not 1 <= self.urgency <= 5:
            raise ValueError(f"urgency must be 1..5, got {self.urgency}")

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": str(self.category),
            "language": str(self.language),
            "is_network": self.is_network,
            "urgency": self.urgency,
            "sentiment": self.sentiment,
            "confidence": self.confidence,
            "method": self.method,
            "needs_human": self.needs_human,
        }


@dataclass(frozen=True, slots=True)
class EnrichmentConfig:
    """Thresholds for the rules classifier.

    Module-local rather than policy: these are lexical scoring constants, not the kind of
    judgement a reviewer argues about. The *confidence floor* that decides whether a human
    sees the complaint does live in policy (`confidence.classification_min`).
    """

    #: Below this, the category is `OTHER` whatever scored highest. Calibrated at 0.40 so
    #: that **two** suggestive single words in the same family are enough to name a
    #: category, while one is not. Those labels land at a confidence below the policy
    #: floor (`confidence.classification_min`, 0.50), so they are marked `needs_human` and
    #: treated as provisional — which is the right outcome for a lexical guess.
    min_category_score: float = 0.40
    #: A second category within this distance of the winner means the text is ambiguous.
    ambiguity_margin: float = 0.15
    max_text_chars: int = 4_000
