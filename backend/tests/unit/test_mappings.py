"""Source-mapping validation: schema drift must fail loudly, not silently return NULLs."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tazama.core.errors import ValidationFailed
from tazama.modules.discovery.mappings import (
    app_owned_tables,
    load_mappings,
    validate_mappings,
)

CANONICAL: dict[str, frozenset[str]] = {
    "cell": frozenset({"id", "site_id", "tech", "region", "name"}),
    "incident": frozenset({"id", "status"}),
}

MINIMAL = {
    "version": 1,
    "default_timezone": "Africa/Nairobi",
    "entities": {
        "cell": {
            "demo": {"source": "cell", "columns": {}},
            "live": {
                "source": "NETWORK.DIM_CELL",
                "columns": {
                    "id": "CELL_ID",
                    "site_id": "SITE_CODE",
                    "tech": "RAT",
                    "region": "REGION_NAME",
                    "name": "CELL_NAME",
                },
                "types": {"id": "string"},
                "filters": ["STATUS = 'ACTIVE'"],
            },
            "required": ["id", "site_id", "tech"],
            "join_keys": {"incident": {"local": "id", "remote": "id"}},
        },
    },
    "app_owned": ["incident"],
}


def _write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "mappings.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


# ------------------------------------------------------------ the real file


def test_repo_mapping_file_is_valid_in_demo_mode(repo_root: Path) -> None:
    """The committed mapping must validate against the committed schema."""
    from tazama.infrastructure.db.base import Base

    canonical = {
        name: frozenset(c.name for c in table.columns)
        for name, table in Base.metadata.tables.items()
    }
    report = validate_mappings(
        canonical,
        path=repo_root / "config" / "source_mappings.yaml",
        mode="demo",
    )
    assert report.ok, report.render()
    assert report.coverage_pct == 100.0  # identity mappings cover every column


def test_repo_mapping_file_is_valid_in_live_mode(repo_root: Path) -> None:
    from tazama.infrastructure.db.base import Base

    canonical = {
        name: frozenset(c.name for c in table.columns)
        for name, table in Base.metadata.tables.items()
    }
    report = validate_mappings(
        canonical,
        path=repo_root / "config" / "source_mappings.yaml",
        mode="live",
    )
    assert report.ok, report.render()
    # Live mappings are partial by nature; coverage is reported, not required to be 100%.
    assert 40.0 < report.coverage_pct <= 100.0
    assert "kpi_cell" in report.entities
    assert "incident" in report.app_owned


def test_every_canonical_table_is_mapped_or_declared_app_owned(repo_root: Path) -> None:
    from tazama.infrastructure.db.base import Base

    path = repo_root / "config" / "source_mappings.yaml"
    mapped = set(load_mappings(path, mode="demo"))
    owned = set(app_owned_tables(path))
    assert set(Base.metadata.tables) - mapped - owned == set()


def test_live_timestamp_sources_declare_a_timezone(repo_root: Path) -> None:
    """A live source that stores local time must say so, or UTC conversion is wrong."""
    mappings = load_mappings(repo_root / "config" / "source_mappings.yaml", mode="live")
    for name in ("alarm", "kpi_cell", "complaint", "care_call"):
        assert mappings[name].timezone == "Africa/Nairobi", name


def test_live_reads_are_time_bounded(repo_root: Path) -> None:
    """Unbounded reads of history are refused by convention; the filters encode it."""
    mappings = load_mappings(repo_root / "config" / "source_mappings.yaml", mode="live")
    for name in ("alarm", "kpi_cell", "complaint", "care_call"):
        joined = " ".join(mappings[name].filters)
        assert ":window_start" in joined and ":window_end" in joined, name


# -------------------------------------------------------------- validation


def test_identity_mapping_resolves_columns_to_themselves(tmp_path: Path) -> None:
    mapping = load_mappings(_write(tmp_path, MINIMAL), mode="demo")["cell"]
    assert mapping.is_identity
    assert mapping.source_column("tech") == "tech"


def test_live_mapping_translates_names(tmp_path: Path) -> None:
    mapping = load_mappings(_write(tmp_path, MINIMAL), mode="live")["cell"]
    assert mapping.source_column("tech") == "RAT"
    assert mapping.canonical_for("REGION_NAME") == "region"
    assert mapping.tzinfo.key == "Africa/Nairobi"


def test_unknown_canonical_column_is_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["columns"]["bandwidth"] = "BW"
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok
    assert any("bandwidth" in i.message for i in report.errors)


def test_required_column_must_be_mapped(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    del document["entities"]["cell"]["live"]["columns"]["site_id"]
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok
    assert any("site_id" in i.message for i in report.errors)


def test_unmapped_columns_are_a_warning_not_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    del document["entities"]["cell"]["live"]["columns"]["name"]
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert report.ok
    assert any("read as NULL" in i.message for i in report.warnings)


def test_missing_table_mapping_is_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["app_owned"] = []
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="demo")
    assert not report.ok
    assert any(i.entity == "incident" for i in report.errors)


def test_mapping_for_unknown_table_is_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["unicorn"] = {"demo": {"source": "unicorn", "columns": {}}}
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="demo")
    assert not report.ok
    assert any(i.entity == "unicorn" for i in report.errors)


def test_bad_timezone_is_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["timezone"] = "Mars/Olympus"
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok
    assert any("Mars/Olympus" in i.message for i in report.errors)


def test_unknown_declared_type_is_an_error(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["types"]["id"] = "blob"
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok
    assert any("blob" in i.message for i in report.errors)


@pytest.mark.parametrize(
    "fragment",
    [
        "STATUS = 'ACTIVE'; drop table cell",
        "STATUS = 'ACTIVE' -- comment",
        "STATUS = 'ACTIVE' /* comment */",
        "1=1 union select * from dba_users",
    ],
)
def test_filters_cannot_smuggle_sql(tmp_path: Path, fragment: str) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["filters"] = [fragment]
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok, fragment


def test_filters_may_only_bind_known_parameters(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["filters"] = ["OWNER = :whoever"]
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert not report.ok
    assert any("whoever" in i.message for i in report.errors)


def test_known_bind_parameters_are_accepted(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["live"]["filters"] = [
        "TS >= :window_start",
        "TS < :window_end",
    ]
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="live")
    assert report.ok, report.render()


def test_join_keys_are_checked_both_sides(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    document["entities"]["cell"]["join_keys"] = {"incident": {"local": "nope", "remote": "id"}}
    report = validate_mappings(CANONICAL, path=_write(tmp_path, document), mode="demo")
    assert not report.ok
    assert any("nope" in i.message for i in report.errors)


def test_missing_file_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="not found"):
        load_mappings("does/not/exist.yaml")


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("entities: [unclosed", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="valid YAML"):
        load_mappings(path)


def test_missing_mode_block_is_refused(tmp_path: Path) -> None:
    document = yaml.safe_load(yaml.safe_dump(MINIMAL))
    del document["entities"]["cell"]["live"]
    with pytest.raises(ValidationFailed, match="no 'live' mapping"):
        load_mappings(_write(tmp_path, document), mode="live")


def test_report_renders_a_readable_summary(tmp_path: Path) -> None:
    report = validate_mappings(CANONICAL, path=_write(tmp_path, MINIMAL), mode="demo")
    rendered = report.render()
    assert "source mappings v1" in rendered
    assert "coverage" in rendered
    assert str(report).startswith("ok")
