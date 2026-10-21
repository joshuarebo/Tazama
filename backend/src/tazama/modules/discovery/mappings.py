"""Source-mapping layer: the canonical model expressed in terms of a real system.

`config/source_mappings.yaml` is the only place that knows real schema, table and column
names. Demo mode maps the canonical model onto the seeded tables; live mode maps it onto
the operator's objects. Switching is configuration, never code (PRD section 7).

Validation runs at startup and in `make doctor`, and it is deliberately strict: a mapping
that references an unknown canonical column, omits a required one, or points a timestamp
at a non-timestamp column fails *before* anything queries a real system. Schema drift in
a source table therefore surfaces as a clear configuration error rather than as silent
NULLs in a KPI chart.

This module is pure: it reads YAML and validates structure. It never opens a database —
the canonical column names it validates against are injected by the caller (module
boundaries: `modules/` must not import `infrastructure/`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from tazama.core.errors import ValidationFailed

Mode = Literal["demo", "live"]

#: Types a mapping may declare for a canonical column.
VALID_TYPES: frozenset[str] = frozenset(
    {"string", "int", "float", "bool", "timestamp", "json"},
)

#: Parameters a filter fragment may bind. Anything else is rejected, which is what keeps
#: filters from becoming a SQL injection vector: they are bound, never formatted.
ALLOWED_BIND_PARAMS: frozenset[str] = frozenset({"window_start", "window_end", "region"})


@dataclass(frozen=True, slots=True)
class EntityMapping:
    """How one canonical entity is read from a source."""

    entity: str
    mode: Mode
    source: str
    columns: dict[str, str]
    types: dict[str, str] = field(default_factory=dict)
    filters: tuple[str, ...] = ()
    timezone: str = "UTC"
    required: tuple[str, ...] = ()
    join_keys: dict[str, dict[str, str]] = field(default_factory=dict)
    source_fine: str | None = None
    msisdn_source: str | None = None
    pii_source_columns: tuple[str, ...] = ()

    @property
    def is_identity(self) -> bool:
        """True when canonical names are used verbatim (the demo case)."""
        return not self.columns

    def source_column(self, canonical: str) -> str:
        """Source column for a canonical column (itself, under an identity mapping)."""
        return self.columns.get(canonical, canonical)

    def canonical_for(self, source_column: str) -> str | None:
        for canonical, source in self.columns.items():
            if source == source_column:
                return canonical
        return None

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True, slots=True)
class MappingIssue:
    entity: str
    severity: Literal["error", "warning"]
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.entity}: {self.message}"


@dataclass(frozen=True, slots=True)
class MappingReport:
    """Validation outcome plus coverage, printed by `make doctor`."""

    mode: Mode
    version: int
    entities: tuple[str, ...]
    app_owned: tuple[str, ...]
    issues: tuple[MappingIssue, ...]
    covered_columns: int
    total_columns: int

    @property
    def errors(self) -> tuple[MappingIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[MappingIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def coverage_pct(self) -> float:
        if not self.total_columns:
            return 0.0
        return round(100.0 * self.covered_columns / self.total_columns, 1)

    def render(self) -> str:
        lines = [
            f"source mappings v{self.version} ({self.mode} mode): "
            f"{len(self.entities)} mapped entities, {len(self.app_owned)} app-owned, "
            f"column coverage {self.coverage_pct}% "
            f"({self.covered_columns}/{self.total_columns})",
        ]
        lines.extend(f"  {issue}" for issue in self.issues)
        if self.ok and not self.warnings:
            lines.append("  no issues")
        return "\n".join(lines)

    def __str__(self) -> str:
        status = "ok" if self.ok else f"{len(self.errors)} error(s)"
        return f"{status}, coverage {self.coverage_pct}%"


#: Relative path of the mapping file inside the repository.
CONFIG_RELATIVE = Path("config") / "source_mappings.yaml"


def default_mappings_path() -> Path:
    """Locate `config/source_mappings.yaml` without depending on the working directory.

    The CLI runs from the repository root, pytest runs from `backend/`, and a container
    runs from `/app`, so a relative path would resolve differently in each. The file is
    found by walking up from this module, with `TAZAMA_CONFIG_DIR` as an override for
    deployments that mount configuration elsewhere.
    """
    override = os.environ.get("TAZAMA_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / CONFIG_RELATIVE.name
    for parent in Path(__file__).resolve().parents:
        candidate = parent / CONFIG_RELATIVE
        if candidate.exists():
            return candidate
    return CONFIG_RELATIVE


DEFAULT_PATH = default_mappings_path()


def _as_str_dict(value: Any, entity: str, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationFailed(
            f"{entity}.{field_name} must be a mapping, got {type(value).__name__}"
        )
    return {str(k): str(v) for k, v in value.items()}


def load_raw(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    """Read and parse the mapping file. `safe_load` only — never `load`."""
    file_path = Path(path)
    if not file_path.exists():
        raise ValidationFailed(f"source mapping file not found: {file_path}")
    try:
        data = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"{file_path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationFailed(f"{file_path} must contain a mapping at the top level")
    return data


def load_mappings(
    path: Path | str = DEFAULT_PATH,
    *,
    mode: Mode = "demo",
) -> dict[str, EntityMapping]:
    """Parse the file into one `EntityMapping` per canonical entity."""
    data = load_raw(path)
    default_tz = str(data.get("default_timezone", "UTC"))
    entities_raw = data.get("entities") or {}
    if not isinstance(entities_raw, dict) or not entities_raw:
        raise ValidationFailed("source mappings declare no entities")

    out: dict[str, EntityMapping] = {}
    for entity, spec in entities_raw.items():
        if not isinstance(spec, dict):
            raise ValidationFailed(f"entity {entity} must be a mapping")
        block = spec.get(mode)
        if block is None:
            raise ValidationFailed(f"entity {entity} has no '{mode}' mapping")
        if not isinstance(block, dict):
            raise ValidationFailed(f"entity {entity}.{mode} must be a mapping")
        source = str(block.get("source") or "")
        if not source:
            raise ValidationFailed(f"entity {entity}.{mode} is missing 'source'")

        filters = block.get("filters") or []
        if not isinstance(filters, list):
            raise ValidationFailed(f"entity {entity}.{mode}.filters must be a list")

        out[str(entity)] = EntityMapping(
            entity=str(entity),
            mode=mode,
            source=source,
            columns=_as_str_dict(block.get("columns"), str(entity), "columns"),
            types=_as_str_dict(block.get("types"), str(entity), "types"),
            filters=tuple(str(f) for f in filters),
            # A source that does not declare its timezone inherits the file default;
            # declaring UTC explicitly is how a UTC source opts out.
            timezone=str(block.get("timezone") or default_tz),
            required=tuple(str(r) for r in (spec.get("required") or ())),
            join_keys={
                str(k): _as_str_dict(v, str(entity), f"join_keys.{k}")
                for k, v in (spec.get("join_keys") or {}).items()
            },
            source_fine=(str(block["source_fine"]) if block.get("source_fine") else None),
            msisdn_source=(str(block["msisdn_source"]) if block.get("msisdn_source") else None),
            pii_source_columns=tuple(str(c) for c in (spec.get("pii_source_columns") or ())),
        )
    return out


def app_owned_tables(path: Path | str = DEFAULT_PATH) -> tuple[str, ...]:
    """Tables TAZAMA is the system of record for: no live source is expected."""
    return tuple(str(t) for t in (load_raw(path).get("app_owned") or ()))


def validate_mappings(
    canonical_columns: dict[str, frozenset[str]],
    *,
    path: Path | str = DEFAULT_PATH,
    mode: Mode = "demo",
) -> MappingReport:
    """Check the mapping file against the canonical schema.

    Args:
        canonical_columns: canonical table name -> its column names. Injected by the
            caller (the composition root reads it from the SQLAlchemy metadata), so this
            module stays free of infrastructure imports.

    Returns a report rather than raising, so `make doctor` can print every problem at
    once. Callers that must not start on a bad mapping check `report.ok`.
    """
    data = load_raw(path)
    version = int(data.get("version", 0))
    mappings = load_mappings(path, mode=mode)
    owned = app_owned_tables(path)

    issues: list[MappingIssue] = []
    covered = 0
    total = 0

    unmapped = set(canonical_columns) - set(mappings) - set(owned)
    for table in sorted(unmapped):
        issues.append(
            MappingIssue(
                entity=table,
                severity="error",
                message=(
                    "canonical table has no mapping and is not listed under app_owned — "
                    "add a mapping or declare it app-owned"
                ),
            ),
        )

    for name in sorted(set(mappings) - set(canonical_columns)):
        issues.append(
            MappingIssue(
                entity=name,
                severity="error",
                message="mapping refers to a table that is not in the canonical schema",
            ),
        )

    for entity, mapping in sorted(mappings.items()):
        known = canonical_columns.get(entity)
        if known is None:
            continue
        total += len(known)

        for canonical in sorted(mapping.columns):
            if canonical not in known:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"maps unknown canonical column {canonical!r}",
                    ),
                )

        mapped = {c for c in mapping.columns if c in known}
        covered += len(known) if mapping.is_identity else len(mapped)

        for required in mapping.required:
            if required not in known:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"declares required column {required!r} which does not exist",
                    ),
                )
            elif not mapping.is_identity and required not in mapping.columns:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"required column {required!r} is not mapped to a source column",
                    ),
                )

        for canonical, declared in sorted(mapping.types.items()):
            if declared not in VALID_TYPES:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=(
                            f"column {canonical!r} declares unknown type {declared!r}; "
                            f"expected one of {', '.join(sorted(VALID_TYPES))}"
                        ),
                    ),
                )
            if canonical not in known:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"type declared for unknown column {canonical!r}",
                    ),
                )

        try:
            ZoneInfo(mapping.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            issues.append(
                MappingIssue(
                    entity=entity,
                    severity="error",
                    message=f"timezone {mapping.timezone!r} is not a known IANA zone",
                ),
            )

        issues.extend(_filter_issues(mapping))

        if not mapping.is_identity:
            missing = sorted(known - set(mapping.columns) - {"id"})
            if missing:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="warning",
                        message=(
                            f"{len(missing)} canonical column(s) unmapped and will read as "
                            f"NULL: {', '.join(missing[:8])}" + (" …" if len(missing) > 8 else "")
                        ),
                    ),
                )

        for target, keys in sorted(mapping.join_keys.items()):
            if set(keys) != {"local", "remote"}:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"join to {target!r} must declare exactly 'local' and 'remote'",
                    ),
                )
                continue
            if keys["local"] not in known:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"join to {target!r} uses unknown local column {keys['local']!r}",
                    ),
                )
            remote_known = canonical_columns.get(target)
            if remote_known is None:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=f"join target {target!r} is not a canonical table",
                    ),
                )
            elif keys["remote"] not in remote_known:
                issues.append(
                    MappingIssue(
                        entity=entity,
                        severity="error",
                        message=(
                            f"join to {target!r} uses unknown remote column {keys['remote']!r}"
                        ),
                    ),
                )

        if mode == "live" and mapping.msisdn_source and "id" in known:
            # The live reader hashes this column at the boundary; flag it so a reviewer
            # can see which sources carry an identifier that must never be stored.
            issues.append(
                MappingIssue(
                    entity=entity,
                    severity="warning",
                    message=(
                        f"source column {mapping.msisdn_source!r} is pseudonymised on read; "
                        "the raw value must never be selected into the app schema"
                    ),
                ),
            )

    return MappingReport(
        mode=mode,
        version=version,
        entities=tuple(sorted(mappings)),
        app_owned=owned,
        issues=tuple(issues),
        covered_columns=covered,
        total_columns=total,
    )


def _filter_issues(mapping: EntityMapping) -> list[MappingIssue]:
    """Filters must be simple, bound predicates — not a place to smuggle SQL."""
    issues: list[MappingIssue] = []
    for fragment in mapping.filters:
        lowered = fragment.lower()
        if ";" in fragment or "--" in fragment or "/*" in fragment:
            issues.append(
                MappingIssue(
                    entity=mapping.entity,
                    severity="error",
                    message=f"filter contains a statement separator or comment: {fragment!r}",
                ),
            )
        # A filter is a simple predicate. Anything that could start a new query — a
        # verb, a subselect, a set operator — does not belong in it.
        forbidden = (
            "insert",
            "update",
            "delete",
            "merge",
            "drop",
            "alter",
            "grant",
            "exec",
            "select",
            "from",
            "union",
            "intersect",
            "minus",
            "except",
            "join",
        )
        for keyword in forbidden:
            if keyword in lowered.replace("(", " ").replace(")", " ").split():
                issues.append(
                    MappingIssue(
                        entity=mapping.entity,
                        severity="error",
                        message=f"filter contains the keyword {keyword!r}: {fragment!r}",
                    ),
                )
        for token in _bind_params(fragment):
            if token not in ALLOWED_BIND_PARAMS:
                issues.append(
                    MappingIssue(
                        entity=mapping.entity,
                        severity="error",
                        message=(
                            f"filter binds unknown parameter :{token}; allowed: "
                            f"{', '.join(sorted(ALLOWED_BIND_PARAMS))}"
                        ),
                    ),
                )
    return issues


def _bind_params(fragment: str) -> list[str]:
    params: list[str] = []
    index = 0
    while index < len(fragment):
        if fragment[index] == ":":
            end = index + 1
            while end < len(fragment) and (fragment[end].isalnum() or fragment[end] == "_"):
                end += 1
            if end > index + 1:
                params.append(fragment[index + 1 : end])
            index = end
        else:
            index += 1
    return params
