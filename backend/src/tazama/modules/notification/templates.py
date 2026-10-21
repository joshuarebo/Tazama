"""Approved notification templates: loading, validation and strict rendering.

CLAUDE.md section 7 forbids any component — the LLM included — from writing free text to a
customer. This module is where that rule is enforced, so it is deliberately paranoid:

  * The body comes from a YAML file under `config/notification_templates/`, loaded with
    `yaml.safe_load` only.
  * Rendering is a **non-evaluating** substitution over a fixed `{{slot}}` grammar. There
    is no `str.format` (which exposes attribute and index access on untrusted input), no
    Jinja (whose autoescape and `{% %}` statements are a template-injection surface in a
    file a human may later edit) and no `eval`.
  * Slot values are sanitised with `core.security.sanitise_output`, length-bounded, and
    refused outright if they contain template syntax, markup or anything MSISDN-shaped.
  * The rendered text is length-checked against the template's own cap and against the
    global two-part SMS ceiling.

Every refusal raises `ValidationFailed` with a message that names the offending slot: the
care agent who hit it has to be able to fix the input without reading this file.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import yaml

from tazama.core.errors import ValidationFailed
from tazama.core.paths import config_dir
from tazama.core.security import sanitise_output
from tazama.modules.notification.models import (
    MAX_SLOT_CHARS,
    MAX_SMS_PARTS,
    SMS_PART_CHARS,
    Template,
)

TEMPLATE_DIRNAME = "notification_templates"

#: Languages every template must provide. English and Swahili are both first-class in
#: Kenya; a template that exists in one only cannot be proposed to half the audience.
REQUIRED_LANGUAGES: Final[tuple[str, ...]] = ("en", "sw")

SUPPORTED_CHANNELS: Final[frozenset[str]] = frozenset({"sms"})

#: The entire template grammar. Anything else in a body is a literal character.
_SLOT = re.compile(r"\{\{\s*([a-z][a-z0-9_]{0,31})\s*\}\}")

#: Shapes that mean "someone is trying to template, script or mark up a slot value".
_FORBIDDEN_IN_SLOT = re.compile(r"[{}<>\\`]|\$\{|%\(|\[\[|\]\]")

#: Kenyan MSISDN shapes. A slot value may never carry one: the record holds a pseudonym
#: token and nothing else (ADR 0008).
_MSISDN_SHAPE = re.compile(r"(?<!\d)(?:\+?254|0)(?:7|1)\d{8}(?!\d)")

#: A long digit run is an identifier of some kind; it does not belong in a slot either.
_LONG_DIGITS = re.compile(r"(?<!\d)\d{10,}(?!\d)")


def templates_dir(base: Path | str | None = None) -> Path:
    return Path(base) if base is not None else config_dir() / TEMPLATE_DIRNAME


def load_template(path: Path) -> Template:
    """Read and validate one template file."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"{path.name} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValidationFailed(f"{path.name} must contain a mapping at the top level")
    return _build(raw, source=path.name)


def load_templates(base: Path | str | None = None) -> dict[str, Template]:
    """Load every `*.yaml` template in the directory, keyed by template id."""
    directory = templates_dir(base)
    if not directory.is_dir():
        raise ValidationFailed(f"notification template directory not found: {directory}")
    loaded: dict[str, Template] = {}
    for path in sorted(directory.glob("*.yaml")):
        template = load_template(path)
        if template.id in loaded:
            raise ValidationFailed(f"duplicate template id {template.id!r} in {path.name}")
        loaded[template.id] = template
    if not loaded:
        raise ValidationFailed(f"no templates found in {directory}")
    return loaded


def _build(raw: dict[str, Any], *, source: str) -> Template:
    template_id = str(raw.get("id", "")).strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", template_id):
        raise ValidationFailed(f"{source}: id must be a lowercase identifier, got {template_id!r}")

    version = str(raw.get("version", "")).strip()
    if not version:
        raise ValidationFailed(f"{source}: version is required — an audit row names it")

    channel = str(raw.get("channel", "")).strip()
    if channel not in SUPPORTED_CHANNELS:
        raise ValidationFailed(
            f"{source}: channel {channel!r} is not supported "
            f"(allowed: {', '.join(sorted(SUPPORTED_CHANNELS))})",
        )

    languages_raw = raw.get("languages")
    if not isinstance(languages_raw, dict) or not languages_raw:
        raise ValidationFailed(f"{source}: languages must be a non-empty mapping")
    languages: dict[str, str] = {}
    for code, body in languages_raw.items():
        if not isinstance(body, str) or not body.strip():
            raise ValidationFailed(
                f"{source}: body for language {code!r} must be a non-empty string"
            )
        languages[str(code)] = body.strip()
    missing_languages = [code for code in REQUIRED_LANGUAGES if code not in languages]
    if missing_languages:
        raise ValidationFailed(
            f"{source}: missing required language(s) {', '.join(missing_languages)}",
        )

    required_slots = tuple(str(name) for name in (raw.get("required_slots") or ()))
    for name in required_slots:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise ValidationFailed(f"{source}: required_slots entry {name!r} is not a slot name")
    if len(set(required_slots)) != len(required_slots):
        raise ValidationFailed(f"{source}: required_slots contains a duplicate")

    max_parts = int(raw.get("max_parts", 1))
    if max_parts < 1 or max_parts > MAX_SMS_PARTS:
        raise ValidationFailed(
            f"{source}: max_parts must be between 1 and {MAX_SMS_PARTS}; "
            f"a longer customer SMS is refused by policy",
        )

    max_length = int(raw.get("max_length", SMS_PART_CHARS))
    ceiling = SMS_PART_CHARS * max_parts
    if max_length < 1 or max_length > ceiling:
        raise ValidationFailed(
            f"{source}: max_length {max_length} exceeds {max_parts} SMS part(s) of "
            f"{SMS_PART_CHARS} characters",
        )

    template = Template(
        id=template_id,
        version=version,
        channel=channel,
        languages=languages,
        required_slots=required_slots,
        max_length=max_length,
        approval_required=bool(raw.get("approval_required", True)),
        max_parts=max_parts,
        description=str(raw.get("description", "")),
    )

    # Every body must use exactly the declared slots: a body referencing an undeclared slot
    # would render a literal `{{...}}` to a customer, and a declared-but-unused slot means
    # an approver reviews a value that never appears in the message.
    for code, body in languages.items():
        used = frozenset(_SLOT.findall(body))
        if used != template.slot_names:
            undeclared = sorted(used - template.slot_names)
            unused = sorted(template.slot_names - used)
            raise ValidationFailed(
                f"{source}: language {code!r} slot mismatch — "
                f"undeclared: {undeclared or 'none'}, declared but unused: {unused or 'none'}",
            )
        skeleton = _SLOT.sub("", body)
        if len(skeleton) > template.max_length:
            raise ValidationFailed(
                f"{source}: language {code!r} body is {len(skeleton)} characters before any "
                f"slot value, over the {template.max_length}-character cap",
            )
    if not template.approval_required:
        raise ValidationFailed(
            f"{source}: approval_required=false is refused — a customer-facing message is "
            f"never sent by a machine alone",
        )
    return template


def clean_slot_value(name: str, value: object) -> str:
    """Sanitise and bound one slot value, or refuse it.

    The order matters: refuse the shapes we never want *before* sanitising, so a value that
    only passes because the sanitiser removed its payload is still rejected. A caller must
    not be able to launder `<script>` into a customer message by relying on a strip.
    """
    if not isinstance(value, str):
        raise ValidationFailed(f"slot {name!r} must be a string, got {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValidationFailed(f"slot {name!r} must not be empty")
    if _FORBIDDEN_IN_SLOT.search(text):
        raise ValidationFailed(
            f"slot {name!r} contains template or markup syntax, which is never rendered "
            f"to a customer",
        )
    # Check the digits with separators removed too: "+254 712 345 678" is an MSISDN, and a
    # regex over the raw text would not see it.
    compact = re.sub(r"[\s\-().]", "", text)
    if (
        _MSISDN_SHAPE.search(text)
        or _MSISDN_SHAPE.search(compact)
        or _LONG_DIGITS.search(text)
        or _LONG_DIGITS.search(compact)
    ):
        raise ValidationFailed(
            f"slot {name!r} looks like a subscriber identifier; a notification carries the "
            f"pseudonym token only",
        )
    if "\n" in text or "\r" in text:
        raise ValidationFailed(f"slot {name!r} must be a single line")
    if len(text) > MAX_SLOT_CHARS:
        raise ValidationFailed(
            f"slot {name!r} is {len(text)} characters, over the {MAX_SLOT_CHARS}-character limit",
        )
    cleaned = sanitise_output(text, max_len=MAX_SLOT_CHARS, strip_html=True).strip()
    if not cleaned:
        raise ValidationFailed(f"slot {name!r} is empty after sanitisation")
    return cleaned


def render(template: Template, *, language: str, slots: Mapping[str, object]) -> str:
    """Render an approved template. The only way customer text is produced.

    Raises `ValidationFailed` for an unknown slot, a missing required slot, an unsafe slot
    value, a language the template does not carry, or a result over the length cap.
    """
    body = template.body(language)
    if body is None:
        raise ValidationFailed(
            f"template {template.id!r} has no body for language {language!r} "
            f"(available: {', '.join(sorted(template.languages))})",
        )

    provided = frozenset(str(name) for name in slots)
    unknown = sorted(provided - template.slot_names)
    if unknown:
        raise ValidationFailed(
            f"template {template.id!r} has no slot(s) named {', '.join(unknown)}",
        )
    missing = sorted(template.slot_names - provided)
    if missing:
        raise ValidationFailed(
            f"template {template.id!r} requires slot(s) {', '.join(missing)}",
        )

    cleaned = {name: clean_slot_value(name, slots[name]) for name in template.required_slots}

    def _substitute(match: re.Match[str]) -> str:
        # The key is guaranteed present: the body's slots were validated at load time to
        # equal `required_slots`, and every required slot is in `cleaned`.
        return cleaned[match.group(1)]

    text = _SLOT.sub(_substitute, body)
    if len(text) > template.max_length:
        raise ValidationFailed(
            f"template {template.id!r} rendered to {len(text)} characters, over its "
            f"{template.max_length}-character cap — shorten the slot values",
        )
    parts = max(1, -(-len(text) // SMS_PART_CHARS))
    if parts > template.max_parts:
        raise ValidationFailed(
            f"template {template.id!r} rendered to {parts} SMS parts, over the "
            f"{template.max_parts} allowed",
        )
    return text


class FileTemplateRepository:
    """`TemplateRepository` backed by the YAML files. Loaded once, then read-only.

    An adapter rather than a service concern: the service receives the port, so a test can
    hand it two templates without touching the filesystem.
    """

    __slots__ = ("_templates",)

    def __init__(self, base: Path | str | None = None) -> None:
        self._templates = load_templates(base)

    def get(self, template_id: str) -> Template | None:
        return self._templates.get(template_id)

    def all(self) -> tuple[Template, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))


class InMemoryTemplateRepository:
    """`TemplateRepository` over templates built in code. Used by tests and by the seed."""

    __slots__ = ("_templates",)

    def __init__(self, templates: Mapping[str, Template]) -> None:
        self._templates = dict(templates)

    def get(self, template_id: str) -> Template | None:
        return self._templates.get(template_id)

    def all(self) -> tuple[Template, ...]:
        return tuple(self._templates[key] for key in sorted(self._templates))
