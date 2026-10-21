#!/usr/bin/env python3
"""`make catalog` — profile the data and regenerate docs/data-catalog.md.

Also prints the join-coverage report: the share of complaints resolvable to a cell, to a
site and to an incident. That number is the day-one measure of the hardest problem in the
PRD, so it is printed on every run, not buried in a file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))


def main() -> int:
    try:
        from tazama.modules.discovery.service import DiscoveryService
    except ModuleNotFoundError:
        print("profiler lands in Phase 1 (modules/discovery).")  # noqa: T201
        return 0

    from tazama.api.deps import build_discovery_service  # type: ignore[attr-defined]

    service: DiscoveryService = build_discovery_service()
    catalog = service.profile_all()
    (ROOT / "docs" / "data-catalog.md").write_text(catalog.to_markdown(), encoding="utf-8")
    coverage = service.join_coverage()
    print(coverage.render())  # noqa: T201
    print(f"wrote docs/data-catalog.md ({len(catalog.tables)} tables)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
