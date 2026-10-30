"""What never reaches a model, and what never comes back out of one.

Two directions, two different problems.

**Inbound.** A model endpoint is an external system. Even a self-hosted one logs, caches
and may be swapped for a hosted one by a config change, so a raw MSISDN crossing this
boundary is a breach waiting for a deployment. Everything is redacted before it is sent,
and the redaction is *stable* — the same number becomes the same placeholder inside one
request — so the model can still reason about "the same subscriber" without ever knowing
who that is.

**Outbound.** A model's output is untrusted text. It can carry an identifier it inferred or
hallucinated, a prompt-injection payload that got through, or the canary token that proves
the system prompt leaked. All three are caught here rather than at the point of display,
because by the time text reaches a card or a log it has already escaped.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

#: Kenyan MSISDNs in every shape the channels produce, plus loose long digit runs. The
#: order matters: the specific patterns run first so a `+254…` is labelled as an MSISDN
#: rather than swallowed by the generic digit rule.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("MSISDN", re.compile(r"(?<!\d)\+?254\s?[71]\d{2}\s?\d{3}\s?\d{3}(?!\d)")),
    ("MSISDN", re.compile(r"(?<!\d)0[71]\d{2}\s?\d{3}\s?\d{3}(?!\d)")),
    ("TOKEN", re.compile(r"\bSUB-[A-Z2-7]{8,}\b")),
    ("EMAIL", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("IMSI", re.compile(r"(?<!\d)639\d{12}(?!\d)")),
    ("IMEI", re.compile(r"(?<!\d)\d{15}(?!\d)")),
    ("ID", re.compile(r"(?<!\d)\d{8,14}(?!\d)")),
)

#: Patterns that must never appear in a model's output. A hit is a leak, not a style issue.
_OUTBOUND_PATTERNS = _PATTERNS[:4]


@dataclass(frozen=True, slots=True)
class Redaction:
    """Redacted text plus what was removed, so the audit row can say "3 MSISDNs"."""

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def redacted(self) -> bool:
        return bool(self.counts)

    def summary(self) -> str:
        if not self.counts:
            return "nothing redacted"
        return ", ".join(f"{count}x{kind}" for kind, count in sorted(self.counts.items()))


def redact(text: str) -> Redaction:
    """Replace every identifier with a stable placeholder.

    Stable within one call: the second occurrence of a number becomes the same
    `[MSISDN_1]` as the first, so "the number that complained twice" survives redaction
    while the number itself does not. Not stable *across* calls, deliberately — a
    placeholder that meant the same person in every prompt would be a pseudonym the model
    could accumulate behaviour against.
    """
    if not text:
        return Redaction(text="")

    counts: dict[str, int] = {}
    seen: dict[str, str] = {}

    def replace(kind: str) -> Callable[[re.Match[str]], str]:
        def _inner(match: re.Match[str]) -> str:
            value = match.group(0)
            if value in seen:
                return seen[value]
            index = sum(1 for key in seen if seen[key].startswith(f"[{kind}_")) + 1
            placeholder = f"[{kind}_{index}]"
            seen[value] = placeholder
            counts[kind] = counts.get(kind, 0) + 1
            return placeholder

        return _inner

    result = text
    for kind, pattern in _PATTERNS:
        result = pattern.sub(replace(kind), result)
    return Redaction(text=result, counts=counts)


def contains_identifier(text: str) -> tuple[str, ...]:
    """Which identifier kinds appear in this text. Empty means clean."""
    return tuple(
        sorted({kind for kind, pattern in _OUTBOUND_PATTERNS if pattern.search(text or "")}),
    )


def assert_clean_output(text: str) -> None:
    """Raise when a model's output carries an identifier.

    Raising rather than redacting: an identifier in model output means one got *in*, and
    quietly scrubbing the symptom leaves the inbound hole open.
    """
    found = contains_identifier(text)
    if found:
        raise OutputContainsIdentifier(
            f"model output contains {', '.join(found)}; the inbound redaction has a hole",
        )


class OutputContainsIdentifier(RuntimeError):
    """A model returned something that looks like a subscriber identifier."""
