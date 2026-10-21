#!/usr/bin/env python3
"""`make catalog` — profile the data and regenerate docs/data-catalog.md.

Also prints the join-coverage report: the share of complaints resolvable to a cell, to a
site and to an incident. That number is the day-one measure of the hardest problem in the
PRD, so it is printed on every run rather than buried in a file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))

OUTPUT = ROOT / "docs" / "data-catalog.md"


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile the TAZAMA schema")
    parser.add_argument(
        "--shallow",
        action="store_true",
        help="skip per-column statistics (fast; useful on very large tables)",
    )
    parser.add_argument("--out", default=str(OUTPUT), help="markdown output path")
    args = parser.parse_args()

    from tazama.api.deps import build_discovery_service
    from tazama.core.logging import configure_logging
    from tazama.core.settings import get_settings
    from tazama.modules.discovery.models import ProfileRequest

    settings = get_settings()
    configure_logging(level="WARNING", service="tazama-catalog", mode=settings.APP_MODE)

    service = build_discovery_service(settings)
    mapping_report = service.validate_mappings()
    print(mapping_report.render())  # noqa: T201
    print()  # noqa: T201

    snapshot = service.profile_all(
        ProfileRequest(depth="shallow" if args.shallow else "full"),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(snapshot.to_markdown(), encoding="utf-8")

    if snapshot.coverage is not None:
        print(snapshot.coverage.render())  # noqa: T201
        print()  # noqa: T201

    try:
        relative = out_path.relative_to(ROOT)
    except ValueError:  # an absolute path outside the repo is fine too
        relative = out_path
    # ASCII only: a Windows console in cp1252 would mangle anything else.
    print(  # noqa: T201
        f"wrote {relative} - {len(snapshot.tables)} tables, {snapshot.total_rows:,} rows",
    )
    # A broken mapping is a configuration error worth failing the command for.
    return 0 if mapping_report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
