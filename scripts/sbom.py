#!/usr/bin/env python3
"""Write a CycloneDX-flavoured SBOM for the backend (and the frontend when installed).

Kept dependency-free on purpose: the SBOM must be producible on a machine that cannot
install anything new. Output: artifacts/sbom.json
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts" / "sbom.json"


def python_components() -> list[dict[str, object]]:
    components: list[dict[str, object]] = []
    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata["Name"] or "").lower()):
        name = dist.metadata["Name"]
        if not name:
            continue
        components.append(
            {
                "type": "library",
                "name": name,
                "version": dist.version,
                "purl": f"pkg:pypi/{name.lower()}@{dist.version}",
                "licenses": [{"license": {"name": dist.metadata.get("License") or "unknown"}}],
            },
        )
    return components


def node_components() -> list[dict[str, object]]:
    web = ROOT / "web"
    if not (web / "node_modules").exists():
        return []
    probe = subprocess.run(  # noqa: S603, S607
        ["npm", "ls", "--all", "--json", "--omit", "dev"],
        cwd=web,
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return []
    try:
        tree = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return []

    out: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()

    def walk(node: dict[str, object]) -> None:
        deps = node.get("dependencies")
        if not isinstance(deps, dict):
            return
        for name, spec in deps.items():
            if not isinstance(spec, dict):
                continue
            version = str(spec.get("version", "unknown"))
            if (name, version) not in seen:
                seen.add((name, version))
                out.append(
                    {
                        "type": "library",
                        "name": name,
                        "version": version,
                        "purl": f"pkg:npm/{name}@{version}",
                    },
                )
            walk(spec)

    walk(tree)
    return out


def main() -> int:
    components = python_components() + node_components()
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "component": {
                "type": "application",
                "name": "tazama",
                "version": "0.1.0",
                "description": "TAZAMA — Smart Optimization Intelligence Platform",
            },
            "tools": [{"name": "scripts/sbom.py", "version": "1.0.0"}],
        },
        "components": components,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} with {len(components)} components")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
