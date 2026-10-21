#!/usr/bin/env python3
"""`make seed` — deterministic synthetic seed.

The generator itself lives in `tazama.modules.discovery`-adjacent code under
`tazama.infrastructure.db.seed` (Phase 1); this file is the thin CLI around it so the
command surface stays stable across phases.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed TAZAMA with deterministic synthetic data")
    parser.add_argument("--seed", type=int, default=20_251_020, help="random seed (fixed by default)")
    parser.add_argument("--days", type=int, default=30, help="days of KPI history")
    parser.add_argument("--reset", action="store_true", help="truncate seeded tables first")
    parser.add_argument("--report", action="store_true", help="print the scenario ground-truth table")
    args = parser.parse_args()

    from tazama.infrastructure.db.seed import SeedAborted, run_seed

    try:
        summary = run_seed(seed=args.seed, days=args.days, reset=args.reset)
    except SeedAborted as refusal:
        # An operator should see the sentence that tells them what to do, not a traceback.
        print(f"seed aborted: {refusal}")  # noqa: T201
        return 2
    print(summary.render(include_scenarios=args.report))  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
