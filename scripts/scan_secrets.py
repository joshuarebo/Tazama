#!/usr/bin/env python3
"""Secret scan fallback for machines without gitleaks.

Scans tracked files for credential shapes and for `.env` being committed. Deliberately
simple and noisy-on-purpose: it is a backstop, not a replacement for gitleaks in CI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", "htmlcov", "artifacts", "data",
}
SKIP_SUFFIXES = {".lock", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf", ".zip"}

# Files that legitimately document the *shape* of a secret.
ALLOWLIST_FILES = {".env.example", "scan_secrets.py", "security-baseline.md", "threat-model.md"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("teams-webhook", re.compile(r"https://[a-z0-9.-]*logic\.azure\.com[^\s\"']*")),
    ("power-automate-webhook", re.compile(r"https://prod-\d+\.[a-z0-9-]+\.logic\.azure\.com[^\s\"']*")),
    ("office365-connector", re.compile(r"https://outlook\.office\.com/webhook/[^\s\"']+")),
    ("url-with-credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]{3,}@")),
    (
        "assigned-secret",
        re.compile(
            # The `["']?` after the key name matters: in JSON and YAML the key is itself
            # quoted (`"password": "…"`), and without it the commonest shape a leaked
            # config file takes was never examined at all.
            r"(?i)\b(password|passwd|secret|api[_-]?key|token|salt)\b[\"']?\s*[:=]\s*"
            r"[\"']([^\"'\s]{12,})[\"']",
        ),
    ),
]

PLACEHOLDER = re.compile(
    r"(?i)(change-?me|replace-?with|dev-only|example|placeholder|xxx+|your-|fake|dummy"
    r"|\btest-|\bci-|sample|not-the-|another-secret|hunter2"
    # Interpolation syntax, i.e. a template rather than a value. These are deliberately
    # *shaped* rather than a bare `{`: exempting anything containing a single brace let a
    # real credential through whenever it sat inside a one-line JSON object, e.g.
    # `{"password": "<a real one>"}`, which is precisely the shape a leaked config has.
    r"|\{\{[a-z0-9_. -]*\}\}"  # {{ jinja }} / {{handlebars}}
    r"|\$\{[a-z0-9_.:-]+\}"  # ${SHELL_VAR}
    r"|\{[a-z_][a-z0-9_]*\}"  # {python_format}
    r"|<[a-z_]+>)",  # <angle-placeholder>
)

#: Inline suppression for lines that deliberately contain a credential *shape* — the
#: redaction tests need one in order to prove it gets scrubbed. Same marker as
#: detect-secrets, so a reader does not have to learn a second convention.
ALLOWLIST_MARKER = "pragma: allowlist secret"


def tracked_files() -> list[Path]:
    probe = subprocess.run(  # noqa: S603, S607
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return [ROOT / line for line in probe.stdout.splitlines() if line.strip()]
    return [p for p in ROOT.rglob("*") if p.is_file()]


def should_scan(path: Path) -> bool:
    if not path.is_file():
        return False
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    return path.suffix.lower() not in SKIP_SUFFIXES


def main() -> int:
    findings: list[str] = []
    committed_env = []
    for path in tracked_files():
        rel = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            committed_env.append(rel)
        if not should_scan(path) or path.name in ALLOWLIST_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if len(line) > 2000 or ALLOWLIST_MARKER in line:
                continue
            for name, pattern in RULES:
                match = pattern.search(line)
                # Test the captured *value* where a rule isolates one, not the whole
                # match: for `assigned-secret` the match includes the `password:` prefix,
                # and a placeholder-looking word in that prefix must not excuse the value
                # beside it.
                candidate = match.group(2) if match and match.re.groups >= 2 else None
                if match and not PLACEHOLDER.search(candidate or match.group(0)):
                    findings.append(f"{rel}:{lineno}: {name}")
                    break

    for rel in committed_env:
        findings.append(f"{rel}: an environment file must never be committed")

    if findings:
        print("secret scan FAILED:")  # noqa: T201
        for finding in sorted(set(findings)):
            print(f"  {finding}")  # noqa: T201
        return 1
    print("secret scan: clean")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
