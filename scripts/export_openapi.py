#!/usr/bin/env python3
"""Export the OpenAPI document to docs/openapi.json (PRD section 8: OpenAPI published)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))

OUT = ROOT / "docs" / "openapi.json"


def main() -> int:
    from tazama.api.app import create_app

    app = create_app()
    document = app.openapi()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    paths = len(document.get("paths", {}))
    print(f"wrote {OUT.relative_to(ROOT)} ({paths} paths)")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
