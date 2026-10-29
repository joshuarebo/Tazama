"""The deterministic, multilingual rules classifier.

It exists for three reasons, in order of importance:
  1. The product must keep working when the LLM is unavailable (CLAUDE.md, AI rules). This
     is the fallback that makes "AI degraded" a degradation rather than an outage.
  2. Phase 4 needs something to beat. A classifier with no baseline cannot be shown to be
     worth its cost.
  3. Twenty thousand complaints a day through an LLM is the wrong economics; the PRD's own
     plan is to bootstrap labels with a model and then serve volume with a small one.

It is lexical, not clever: casefolded word-boundary matching against the vocabulary in
`lexicon.py`, phrases weighted above single words, with the score normalised so a long
complaint does not out-score a short one by sheer length. Code-switched text needs no
special handling — a mixed sentence simply matches terms from several languages.

Nothing here interprets the text as an instruction. An injected "ignore all previous
instructions" scores like any other sentence, which is the only safe way to treat
customer-written input.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from tazama.domain.value_objects import NETWORK_CATEGORIES, ComplaintCategory, Language
from tazama.modules.enrichment.lexicon import (
    ANGRY_TERMS,
    CALM_TERMS,
    CATEGORY_TERMS,
    FRUSTRATED_TERMS,
    LANGUAGE_MARKERS,
    NETWORK_HINTS,
    TIE_BREAK_ORDER,
    URGENCY_TERMS,
    WEAK_TERMS,
)
from tazama.modules.enrichment.models import (
    CategoryScore,
    EnrichmentConfig,
    EnrichmentInput,
    EnrichmentResult,
)

#: A phrase is worth this much more than a single word, per extra word in it.
PHRASE_BONUS = 0.6

#: Normalisation floor: scores are divided by `max(words, this)`, so a two-word complaint
#: cannot reach an implausible score and a long one is not unfairly penalised.
LENGTH_FLOOR = 12

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def normalise(text: str) -> str:
    """Casefold, strip accents and collapse whitespace — once, for every matcher."""
    folded = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(stripped.casefold().split())


#: A single suggestive word is worth a fraction of a phrase: enough to name a family, not
#: enough to outvote an explicit phrase.
WEAK_TERM_WEIGHT = 0.45

#: A category matched *only* by suggestive words never clears the policy confidence floor
#: (`confidence.classification_min`, 0.50), however many of them pile up. Three vague words
#: are still a lexical guess: the label is useful — it routes the complaint to the right
#: family — but it is provisional, and a human should confirm it. Capping here rather than
#: in the service keeps the honesty in the score itself, so every consumer sees it.
WEAK_ONLY_CEILING = 0.49


@lru_cache(maxsize=1)
def _term_patterns() -> dict[ComplaintCategory, tuple[tuple[re.Pattern[str], float], ...]]:
    """Compile every term once. Word boundaries stop `data` matching `update`."""
    compiled: dict[ComplaintCategory, tuple[tuple[re.Pattern[str], float], ...]] = {}
    categories = set(CATEGORY_TERMS) | set(WEAK_TERMS)
    for category in categories:
        entries: list[tuple[re.Pattern[str], float]] = []
        for term in CATEGORY_TERMS.get(category, ()):
            words = term.split()
            weight = 1.0 + PHRASE_BONUS * (len(words) - 1)
            pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b")
            entries.append((pattern, weight))
        for term in WEAK_TERMS.get(category, ()):
            entries.append((re.compile(rf"\b{re.escape(term)}\b"), WEAK_TERM_WEIGHT))
        compiled[category] = tuple(entries)
    return compiled


def score_categories(text: str) -> tuple[CategoryScore, ...]:
    """Score every category against the text, highest first."""
    normalised = normalise(text)
    # A fixed divisor, not the word count. Dividing by length *dilutes* a clear signal:
    # "mazungumzo yanakatika and I lose my clients" is unambiguous, and scoring it lower
    # than the same phrase in a shorter sentence is backwards. Length still matters in one
    # direction — a long rambling complaint with one weak word match stays low because its
    # single match is small — but it no longer punishes a complaint for being detailed.
    divisor = float(LENGTH_FLOOR)

    scored: list[CategoryScore] = []
    for category, entries in _term_patterns().items():
        total = 0.0
        matched: list[str] = []
        has_strong = False
        for pattern, weight in entries:
            found = pattern.findall(normalised)
            if found:
                total += weight * min(len(found), 3)  # repetition helps, but only so much
                matched.append(pattern.pattern.replace(r"\b", "").replace(r"\s+", " "))
                # Weak terms are the only ones registered below full weight.
                has_strong = has_strong or weight >= 1.0
        if total:
            # Scaled so a single strong phrase in a short complaint lands near 0.5-0.8.
            score = min(total * 6.0 / divisor, 1.0)
            if not has_strong:
                score = min(score, WEAK_ONLY_CEILING)
            scored.append(
                CategoryScore(
                    category=category,
                    score=round(score, 4),
                    matched=tuple(matched[:6]),
                    weak_only=not has_strong,
                ),
            )
    # On a tie, prefer the category that *explains* rather than the one that describes a
    # symptom: "laini imekufa baada ya kubadilisha SIM" is both a dead line and a SIM swap,
    # and the swap is the actionable reading. Alphabetical order would have answered
    # "no_service", which is true but useless to whoever has to fix it.
    scored.sort(key=lambda s: (-s.score, TIE_BREAK_ORDER.get(s.category, 50), str(s.category)))
    return tuple(scored)


def detect_language(text: str, declared: Language | None = None) -> Language:
    """What the complaint is actually written in.

    A declared language is a hint, not an answer: channels frequently declare `en` for
    everything. Two or more languages scoring together is `MIXED`, which is the common
    case in Kenyan care traffic and worth recording as itself.
    """
    normalised = normalise(text)
    if not normalised:
        return declared or Language.ENGLISH

    words = set(_WORD.findall(normalised))
    hits = {
        language: sum(1 for marker in markers if marker in words or marker in normalised)
        for language, markers in LANGUAGE_MARKERS.items()
    }
    ranked = sorted(hits.items(), key=lambda item: (-item[1], str(item[0])))
    best, best_hits = ranked[0]
    if best_hits == 0:
        return declared or Language.ENGLISH

    runners = [lang for lang, count in ranked[1:] if count >= max(1, best_hits - 1)]
    if runners:
        return Language.MIXED
    return best


def detect_urgency(text: str, category: ComplaintCategory) -> int:
    """1..5. Total loss of service and business impact outrank annoyance."""
    normalised = normalise(text)
    urgency = 3 if category in NETWORK_CATEGORIES else 2
    if category is ComplaintCategory.NO_SERVICE:
        urgency = 4
    for term, level in URGENCY_TERMS.items():
        if term in normalised:
            urgency = max(urgency, level)
    return min(max(urgency, 1), 5)


def detect_sentiment(text: str) -> str:
    """`angry` | `frustrated` | `calm` | `neutral`, in that order of precedence."""
    normalised = normalise(text)
    if any(term in normalised for term in ANGRY_TERMS):
        return "angry"
    if any(term in normalised for term in FRUSTRATED_TERMS):
        return "frustrated"
    if any(term in normalised for term in CALM_TERMS):
        return "calm"
    return "neutral"


def mentions_network(text: str) -> bool:
    normalised = normalise(text)
    return any(re.search(rf"\b{re.escape(hint)}\b", normalised) for hint in NETWORK_HINTS)


class RulesClassifier:
    """`Classifier` implemented with the lexicon. Pure, deterministic, total."""

    __slots__ = ("_config",)

    def __init__(self, config: EnrichmentConfig | None = None) -> None:
        self._config = config or EnrichmentConfig()

    @property
    def method(self) -> str:
        return "rules"

    def classify(self, inbound: EnrichmentInput) -> EnrichmentResult:
        text = (inbound.text or "")[: self._config.max_text_chars]
        scores = score_categories(text)
        reasons: list[str] = []

        language = detect_language(text, inbound.declared_language)
        top = scores[0] if scores else None

        if top is None or top.score < self._config.min_category_score:
            # Nothing cleared the floor. Before giving up on OTHER, answer the question the
            # pipeline actually needs: is this about the network at all? A weak network
            # category plus a network word is far more useful than OTHER, as long as the
            # low confidence travels with it and a human is asked to confirm.
            network_best = next(
                (s for s in scores if s.category in NETWORK_CATEGORIES),
                None,
            )
            if network_best is not None and mentions_network(text):
                category = network_best.category
                confidence = round(min(network_best.score + 0.1, 0.49), 4)
                reasons.append(
                    f"weak match on {network_best.category} ({network_best.score:.2f}) "
                    "but the text names the network: labelled provisionally",
                )
            else:
                category = ComplaintCategory.OTHER
                confidence = 0.2 if top is None else round(min(top.score + 0.1, 0.45), 4)
                reasons.append(
                    "no category term scored above the floor"
                    if top is None
                    else f"best category {top.category} scored only {top.score:.2f}",
                )
            needs_human = True
        else:
            category = top.category
            confidence = top.score
            # A win carried entirely by suggestive words is provisional by construction:
            # the family is probably right, the label is not confirmed.
            needs_human = top.weak_only
            if top.weak_only:
                reasons.append(
                    f"{top.category} matched only suggestive terms "
                    f"({', '.join(top.matched)}) — provisional",
                )
            runner_up = scores[1] if len(scores) > 1 else None
            if runner_up and top.score - runner_up.score < self._config.ambiguity_margin:
                # Two plausible readings: keep the winner but lower the confidence and say
                # why, so the pipeline can route it to a human if policy demands.
                confidence = round(confidence * 0.8, 4)
                reasons.append(
                    f"ambiguous between {top.category} and {runner_up.category} "
                    f"({top.score:.2f} vs {runner_up.score:.2f})",
                )

        if not normalise(text):
            reasons.append("complaint text is empty")
            needs_human = True
            confidence = 0.1

        is_network = category in NETWORK_CATEGORIES
        if category is ComplaintCategory.OTHER and mentions_network(text):
            # "mtandao ni mbaya", "network is bad" — an enormous share of real traffic says
            # only that something is wrong with the network, naming no symptom the lexicon
            # can key on. That is still a network complaint, and treating it as non-network
            # would hide it from correlation entirely, which is exactly the signal the
            # product exists to catch. So it counts towards an incident while the category
            # stays `other` and a human is asked to label it.
            is_network = True
            needs_human = True
            reasons.append("mentions the network but names no symptom — network, unclassified")

        return EnrichmentResult(
            complaint_id=inbound.complaint_id,
            category=category,
            language=language,
            is_network=is_network,
            urgency=detect_urgency(text, category),
            sentiment=detect_sentiment(text),
            confidence=confidence,
            method=self.method,
            scores=scores[:4],
            reasons=tuple(reasons),
            needs_human=needs_human,
        )
