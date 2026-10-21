#!/usr/bin/env python3
"""`make doctor` — check the environment and print the fix for anything broken.

Exit codes:
    0  everything required is in place (warnings may still be printed)
    1  at least one blocking problem

This is the first thing to run on a new machine and after editing .env.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend" / "src"))

OK, WARN, FAIL = "ok", "warn", "fail"
MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
COLOUR = (
    {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[31m", "": "\033[0m"}
    if os.environ.get("NO_COLOR") is None
    else {OK: "", WARN: "", FAIL: "", "": ""}
)
MIN_PYTHON = (3, 12)


@dataclass(slots=True)
class Result:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


def emit(result: Result) -> None:
    colour = COLOUR[result.status]
    reset = COLOUR[""]
    line = f"{colour}{MARK[result.status]}{reset} {result.name}"
    if result.detail:
        line += f" — {result.detail}"
    print(line)
    if result.fix and result.status != OK:
        print(f"        fix: {result.fix}")


# ----------------------------------------------------------------- toolchain


def check_python() -> Result:
    version = sys.version_info
    current = f"{version.major}.{version.minor}.{version.micro}"
    if version[:2] < MIN_PYTHON:
        return Result(
            "python",
            FAIL,
            f"{current} is too old",
            "install Python 3.12 or newer (uv python install 3.12)",
        )
    return Result("python", OK, current)


def check_tool(name: str, *, required: bool, fix: str, version_args: tuple[str, ...] = ("--version",)) -> Result:
    path = shutil.which(name)
    if path is None:
        return Result(name, FAIL if required else WARN, "not found", fix)
    detail = ""
    try:
        probe = subprocess.run(  # noqa: S603
            [path, *version_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        detail = (probe.stdout or probe.stderr).strip().splitlines()[0][:70]
    except (OSError, subprocess.SubprocessError):
        detail = path
    return Result(name, OK, detail)


def check_docker_daemon() -> Result:
    if shutil.which("docker") is None:
        return Result(
            "docker daemon",
            WARN,
            "docker CLI not found",
            "Oracle integration tests and `make up` need Docker Desktop; demo mode does not",
        )
    probe = subprocess.run(  # noqa: S603, S607
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return Result(
            "docker daemon",
            WARN,
            "not running",
            "start Docker Desktop if you want the Oracle backend; sqlite demo mode works without it",
        )
    return Result("docker daemon", OK, probe.stdout.strip())


# -------------------------------------------------------------------- config


def check_env_file() -> Result:
    env = ROOT / ".env"
    if not env.exists():
        return Result(
            ".env",
            WARN,
            "missing",
            "copy .env.example to .env (make bootstrap does this for you)",
        )
    return Result(".env", OK, "present")


def check_gitignores_env() -> Result:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8")
    if ".env" not in patterns:
        return Result(
            ".env is gitignored",
            FAIL,
            ".gitignore does not exclude .env",
            "add `.env` to .gitignore before committing anything",
        )
    return Result(".env is gitignored", OK)


def check_settings() -> tuple[Result, object | None]:
    try:
        from tazama.core.settings import ConfigurationError, get_settings
    except Exception as exc:  # pragma: no cover - import-time failure
        return (
            Result(
                "settings import",
                FAIL,
                f"{type(exc).__name__}: {exc}",
                "run: make bootstrap",
            ),
            None,
        )
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        return Result("settings", FAIL, str(exc).splitlines()[0], str(exc)), None
    detail = (
        f"mode={settings.APP_MODE} env={settings.APP_ENV} db={settings.DB_BACKEND} "
        f"vector={settings.VECTOR_BACKEND} auth={settings.AUTH_MODE}"
    )
    return Result("settings", OK, detail), settings


def check_env_example_parity() -> Result:
    """Every settings field must appear in .env.example, and vice versa."""
    try:
        from tazama.core.settings import Settings
    except Exception:  # pragma: no cover
        return Result(".env.example parity", WARN, "settings not importable")
    documented = {
        line.split("=", 1)[0].strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    declared = set(Settings.model_fields)
    missing = sorted(declared - documented)
    extra = sorted(documented - declared)
    if missing or extra:
        problems = []
        if missing:
            problems.append(f"missing from .env.example: {', '.join(missing)}")
        if extra:
            problems.append(f"unknown in .env.example: {', '.join(extra)}")
        return Result(
            ".env.example parity",
            FAIL,
            "; ".join(problems),
            "keep .env.example and core/settings.py in lockstep (CI enforces this too)",
        )
    return Result(".env.example parity", OK, f"{len(declared)} variables")


def check_secret_placeholders(settings: object) -> Result:
    app_env = getattr(settings, "APP_ENV", "local")
    salt = getattr(settings, "PSEUDONYM_SALT", None)
    raw = salt.get_secret_value() if salt is not None else ""
    if app_env in ("local", "dev"):
        if "dev-only" in raw or "replace-with" in raw:
            return Result(
                "secrets",
                WARN,
                "placeholder PSEUDONYM_SALT (fine locally, never beyond dev)",
                "generate one: python -c \"import secrets;print(secrets.token_urlsafe(32))\"",
            )
        return Result("secrets", OK, "set")
    return Result("secrets", OK, "validated by settings for this APP_ENV")


# ------------------------------------------------------------------ runtime


def check_dependencies() -> Result:
    required = ("fastapi", "pydantic", "pydantic_settings", "sqlalchemy", "alembic", "sqlglot", "jwt")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        return Result(
            "python packages",
            FAIL,
            f"missing: {', '.join(missing)}",
            "run: make bootstrap   (or: uv sync --project backend --extra dev)",
        )
    return Result("python packages", OK, f"{len(required)} core packages importable")


def check_readiness(settings: object) -> list[Result]:
    try:
        from tazama.api.readiness import run_checks
    except Exception as exc:
        return [Result("readiness probes", WARN, f"{type(exc).__name__}: {exc}")]
    checks = asyncio.run(run_checks(settings))  # type: ignore[arg-type]
    mapping = {"ok": OK, "degraded": WARN, "skipped": WARN, "down": FAIL}
    return [
        Result(f"probe:{check.name}", mapping[check.status], check.detail)
        for check in checks
    ]


def check_mappings() -> Result:
    path = ROOT / "config" / "source_mappings.yaml"
    if not path.exists():
        return Result("source mappings", WARN, "not written yet (Phase 1)")
    try:
        from tazama.modules.discovery.mappings import load_mappings  # type: ignore[import-not-found]
    except Exception:
        return Result("source mappings", WARN, "loader lands in Phase 1")
    try:
        report = load_mappings(path)
    except Exception as exc:
        return Result(
            "source mappings",
            FAIL,
            f"{type(exc).__name__}: {exc}",
            "fix config/source_mappings.yaml; see docs/runbooks/bad-mapping.md",
        )
    return Result("source mappings", OK, str(report))


def check_web() -> Result:
    if not (ROOT / "web" / "package.json").exists():
        return Result("web", WARN, "frontend lands in Phase 5")
    if not (ROOT / "web" / "node_modules").exists():
        return Result("web", WARN, "dependencies not installed", "run: make web-install")
    return Result("web", OK, "installed")


def main() -> int:
    print(f"TAZAMA doctor — {platform.system()} {platform.release()} / {platform.machine()}")
    print(f"repo: {ROOT}\n")

    results: list[Result] = [
        check_python(),
        check_tool("uv", required=True, fix="install uv: https://docs.astral.sh/uv/"),
        check_tool("git", required=True, fix="install git"),
        check_tool("node", required=False, fix="install Node.js 20+ for the frontend"),
        check_tool("gitleaks", required=False, fix="optional; detect-secrets is used as fallback"),
        check_docker_daemon(),
        check_env_file(),
        check_gitignores_env(),
        check_env_example_parity(),
        check_dependencies(),
    ]

    settings_result, settings = check_settings()
    results.append(settings_result)
    if settings is not None:
        results.append(check_secret_placeholders(settings))
        results.extend(check_readiness(settings))
    results.append(check_mappings())
    results.append(check_web())

    for result in results:
        emit(result)

    failures = [r for r in results if r.status == FAIL]
    warnings = [r for r in results if r.status == WARN]
    print()
    if failures:
        print(f"{len(failures)} blocking problem(s), {len(warnings)} warning(s).")
        return 1
    print(f"doctor: OK ({len(warnings)} warning(s) — see fixes above).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
