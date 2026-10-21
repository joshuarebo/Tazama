#!/usr/bin/env python3
"""`make replay` — deterministically replay an event stream or a single incident.

Replay is the debugging tool the runbooks point at: given an incident id, it re-runs the
recorded events through the pipeline with a fixed clock and prints the decisions, so a
disputed outcome can be reproduced exactly. Implemented in Phase 2.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay TAZAMA events deterministically")
    parser.add_argument("--incident", help="incident id to replay")
    parser.add_argument("--since", help="ISO-8601 instant; replay everything after it")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--diff", action="store_true", help="compare against the stored outcome")
    args = parser.parse_args()

    try:
        from tazama.worker.replay import replay_incident, replay_since
    except ModuleNotFoundError:
        print("replay lands in Phase 2 (worker/replay.py).")  # noqa: T201
        return 0

    if args.incident:
        result = replay_incident(args.incident, diff=args.diff)
    else:
        result = replay_since(args.since, diff=args.diff)
    print(result.render())  # noqa: T201
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
