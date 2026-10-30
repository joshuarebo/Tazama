#!/usr/bin/env python3
"""TAZAMA task runner — the single source of truth for every build command.

`make <target>`, `./make.ps1 <target>` and `python scripts/mk.py <target>` all end up
here, so the three entry points cannot drift apart (GNU make is not installed on every
developer machine, Windows in particular).

Usage:
    python scripts/mk.py help
    python scripts/mk.py ci
    python scripts/mk.py demo
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
WEB = ROOT / "web"
IS_WINDOWS = platform.system() == "Windows"

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
    if os.environ.get("NO_COLOR") is None
    else ("", "", "", "", "", "")
)


def say(message: str, colour: str = "") -> None:
    sys.stdout.write(f"{colour}{message}{RESET}\n")
    sys.stdout.flush()


class TaskFailed(RuntimeError):
    def __init__(self, command: str, code: int) -> None:
        super().__init__(f"{command} exited with {code}")
        self.code = code


def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
) -> int:
    where = cwd or ROOT
    printable = " ".join(args)
    if not quiet:
        say(f"  $ {printable}", DIM)
    merged = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(args, cwd=where, env=merged, check=False)  # noqa: S603
    except FileNotFoundError as exc:
        raise TaskFailed(printable, 127) from exc
    if check and completed.returncode != 0:
        raise TaskFailed(printable, completed.returncode)
    return completed.returncode


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------- environment


def uv() -> str:
    if not have("uv"):
        raise TaskFailed(
            "uv not found. Install it: https://docs.astral.sh/uv/getting-started/install/",
            127,
        )
    return "uv"


def uv_run(args: Sequence[str], **kwargs: object) -> int:
    """Run a command inside the backend virtual environment managed by uv."""
    return run([uv(), "run", "--project", str(BACKEND), *args], **kwargs)  # type: ignore[arg-type]


def npm() -> str:
    candidate = "npm.cmd" if IS_WINDOWS else "npm"
    if not have(candidate) and not have("npm"):
        raise TaskFailed("npm not found. Install Node.js 20+.", 127)
    return candidate if have(candidate) else "npm"


def docker_available() -> bool:
    if not have("docker"):
        return False
    probe = subprocess.run(  # noqa: S603, S607
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def ensure_env_file() -> None:
    """Materialise a .env from .env.example on first run (demo defaults only)."""
    env_file = ROOT / ".env"
    if env_file.exists():
        return
    example = ROOT / ".env.example"
    env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    say("  created .env from .env.example (demo defaults; no secrets)", YELLOW)


# --------------------------------------------------------------------- tasks


@dataclass(slots=True)
class Task:
    name: str
    help: str
    fn: Callable[[], None]
    group: str = "misc"
    deps: tuple[str, ...] = field(default_factory=tuple)


TASKS: dict[str, Task] = {}


def task(
    name: str,
    help: str,
    group: str = "misc",
    deps: tuple[str, ...] = (),
) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def wrap(fn: Callable[[], None]) -> Callable[[], None]:
        TASKS[name] = Task(name=name, help=help, fn=fn, group=group, deps=deps)
        return fn

    return wrap


# ---- setup -----------------------------------------------------------------


@task("bootstrap", "Create the Python venv and install backend dependencies", "setup")
def bootstrap() -> None:
    ensure_env_file()
    run([uv(), "sync", "--project", str(BACKEND), "--extra", "dev"])
    say("backend environment ready", GREEN)


@task("bootstrap-ml", "Install the optional ML dependency group (Phase 3+)", "setup")
def bootstrap_ml() -> None:
    run([uv(), "sync", "--project", str(BACKEND), "--extra", "dev", "--extra", "ml"])
    say("ML dependencies installed", GREEN)


@task("web-install", "Install frontend dependencies", "setup")
def web_install() -> None:
    if not (WEB / "package.json").exists():
        say("  web/ not scaffolded yet (Phase 5) — skipping", YELLOW)
        return
    run([npm(), "install", "--no-fund", "--no-audit"], cwd=WEB)


# ---- quality ---------------------------------------------------------------


@task("fmt", "Format Python (ruff format) and the frontend if present", "quality")
def fmt() -> None:
    uv_run(["ruff", "format", "src", "tests"], cwd=BACKEND)
    uv_run(["ruff", "check", "--fix", "src", "tests"], cwd=BACKEND)


@task("lint", "ruff + import boundaries + frontend lint", "quality")
def lint() -> None:
    uv_run(["ruff", "check", "src", "tests"], cwd=BACKEND)
    uv_run(["ruff", "format", "--check", "src", "tests"], cwd=BACKEND)
    boundaries()
    if (WEB / "node_modules").exists():
        run([npm(), "run", "lint"], cwd=WEB)
    else:
        say("  web lint skipped (run: mk web-install)", YELLOW)


@task("typecheck", "mypy (strict on domain/modules/core) and tsc", "quality")
def typecheck() -> None:
    uv_run(["mypy", "src"], cwd=BACKEND)
    if (WEB / "node_modules").exists():
        run([npm(), "run", "typecheck"], cwd=WEB)
    else:
        say("  tsc skipped (run: mk web-install)", YELLOW)


@task("boundaries", "Enforce module boundaries (import-linter contracts)", "quality")
def boundaries() -> None:
    uv_run(["lint-imports", "--config", "pyproject.toml"], cwd=BACKEND)


@task("secrets", "Secret scan (gitleaks when available, detect-secrets otherwise)", "quality")
def secrets() -> None:
    if have("gitleaks"):
        run(["gitleaks", "detect", "--no-banner", "--redact", "--source", str(ROOT)])
        return
    say("  gitleaks not installed; using detect-secrets", YELLOW)
    uv_run(
        [
            "python",
            str(ROOT / "scripts" / "scan_secrets.py"),
        ],
        cwd=ROOT,
    )


@task("audit", "Dependency vulnerability audit (pip-audit + npm audit)", "quality")
def audit() -> None:
    uv_run(["pip-audit", "--strict", "--progress-spinner", "off"], cwd=BACKEND, check=False)
    if (WEB / "node_modules").exists():
        run([npm(), "audit", "--omit", "dev"], cwd=WEB, check=False)


@task("sbom", "Write a CycloneDX-style SBOM to artifacts/sbom.json", "quality")
def sbom() -> None:
    uv_run(["python", str(ROOT / "scripts" / "sbom.py")], cwd=ROOT)


# ---- tests -----------------------------------------------------------------


@task("test", "Full pytest suite except integration/load/live-eval", "test")
def test() -> None:
    uv_run(
        [
            "pytest",
            "-m",
            "not integration and not load and not eval_live",
            "--cov=tazama",
            "--cov-report=term-missing:skip-covered",
            "--cov-report=xml",
        ],
        cwd=BACKEND,
    )


@task("test-unit", "Unit tests only (fast loop)", "test")
def test_unit() -> None:
    uv_run(["pytest", "tests/unit", "-q"], cwd=BACKEND)


@task("test-it", "Oracle integration suite (needs the oracle container)", "test")
def test_it() -> None:
    if not docker_available():
        say("  Docker daemon unavailable: integration suite SKIPPED (not passed)", YELLOW)
        say("  start Docker Desktop, then: mk up-oracle && mk test-it", YELLOW)
        return
    uv_run(
        ["pytest", "-m", "integration", "-q"],
        cwd=BACKEND,
        env={"DB_BACKEND": "oracle"},
    )


@task("coverage-gate", "Fail unless domain+modules coverage is at least 80%", "test")
def coverage_gate() -> None:
    uv_run(["python", str(ROOT / "scripts" / "coverage_gate.py")], cwd=BACKEND)


@task("eval", "LLM/agent eval suite against the deterministic fake LLM", "test")
def evals() -> None:
    uv_run(["pytest", "-m", "eval", "-q"], cwd=BACKEND)


@task("eval-live", "Real-Qwen evals (skipped unless LLM_BASE_URL is set)", "test")
def eval_live() -> None:
    uv_run(["pytest", "-m", "eval_live", "-q"], cwd=BACKEND, check=False)


@task("e2e", "Playwright journeys against a running stack", "test")
def e2e() -> None:
    if not (WEB / "node_modules").exists():
        say("  web/ not installed — run: mk web-install", YELLOW)
        return
    run([npm(), "run", "e2e"], cwd=WEB)


@task("load", "Load test (Locust, headless)", "test")
def load() -> None:
    uv_run(
        [
            "locust",
            "-f",
            str(BACKEND / "tests" / "load" / "locustfile.py"),
            "--headless",
            "-u",
            "20",
            "-r",
            "5",
            "-t",
            "30s",
            "--host",
            os.environ.get("NEXT_PUBLIC_API_BASE_URL", "http://localhost:8000"),
        ],
        cwd=BACKEND,
    )


# ---- data ------------------------------------------------------------------


@task("migrate", "Apply database migrations (alembic upgrade head)", "data")
def migrate() -> None:
    ensure_env_file()
    uv_run(["alembic", "upgrade", "head"], cwd=BACKEND)


@task("downgrade", "Roll back one migration (proves down-migrations work)", "data")
def downgrade() -> None:
    uv_run(["alembic", "downgrade", "-1"], cwd=BACKEND)


@task("seed", "Deterministic synthetic seed with all labelled scenarios", "data", ("migrate",))
def seed() -> None:
    # `--reset` keeps `make seed` and `make demo` idempotent. The script itself refuses to
    # overwrite a populated database without it, which is the behaviour you want from a CLI.
    uv_run(["python", str(ROOT / "scripts" / "seed.py"), "--reset"], cwd=ROOT)


@task("catalog", "Regenerate docs/data-catalog.md and the join-coverage report", "data")
def catalog() -> None:
    uv_run(["python", str(ROOT / "scripts" / "catalog.py")], cwd=ROOT)


@task("replay", "Replay an incident deterministically (TAZAMA_REPLAY_ID=...)", "data")
def replay() -> None:
    target = os.environ.get("TAZAMA_REPLAY_ID", "")
    args = ["python", str(ROOT / "scripts" / "replay.py")]
    if target:
        args += ["--incident", target]
    uv_run(args, cwd=ROOT)


@task("train", "Train the Phase 3 models and refresh the registry", "data")
def train() -> None:
    uv_run(["python", "-m", "tazama.training"], cwd=BACKEND)


@task("openapi", "Export the OpenAPI document to docs/openapi.json", "data")
def openapi() -> None:
    uv_run(["python", str(ROOT / "scripts" / "export_openapi.py")], cwd=ROOT)


# ---- run -------------------------------------------------------------------


@task("doctor", "Check the environment and print actionable fixes", "run")
def doctor() -> None:
    uv_run(["python", str(ROOT / "scripts" / "doctor.py")], cwd=ROOT)


@task("api", "Run the API with reload (http://localhost:8000/docs)", "run")
def api() -> None:
    ensure_env_file()
    uv_run(
        [
            "uvicorn",
            "tazama.api.app:asgi",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--reload",
        ],
        cwd=BACKEND,
    )


@task("worker", "Run the event worker and scheduler", "run")
def worker() -> None:
    ensure_env_file()
    uv_run(["python", "-m", "tazama.worker"], cwd=BACKEND)


@task("fake-llm", "Run the deterministic fake LLM server on :8088", "run")
def fake_llm() -> None:
    uv_run(["python", str(ROOT / "scripts" / "fake_llm.py")], cwd=ROOT)


@task("web", "Run the Next.js dev server (http://localhost:3000)", "run")
def web() -> None:
    if not (WEB / "node_modules").exists():
        web_install()
    run([npm(), "run", "dev"], cwd=WEB)


@task("up", "Start the docker-compose stack", "run")
def up() -> None:
    if not docker_available():
        raise TaskFailed("Docker daemon is not running. Start Docker Desktop and retry.", 1)
    ensure_env_file()
    run(["docker", "compose", "up", "-d", "--build"])
    run(["docker", "compose", "ps"])


@task("up-oracle", "Start only the Oracle Free container (integration tests)", "run")
def up_oracle() -> None:
    if not docker_available():
        raise TaskFailed("Docker daemon is not running. Start Docker Desktop and retry.", 1)
    run(["docker", "compose", "--profile", "oracle", "up", "-d", "oracle"])
    say("Oracle Free starting; first boot takes several minutes.", YELLOW)


@task("down", "Stop the stack (keeps volumes)", "run")
def down() -> None:
    if docker_available():
        run(["docker", "compose", "down"], check=False)
    else:
        say("  Docker daemon unavailable; nothing to stop", YELLOW)


@task("clean", "Remove caches, local database and build output", "run")
def clean() -> None:
    targets = [
        ROOT / "data",
        ROOT / "artifacts",
        BACKEND / ".pytest_cache",
        BACKEND / ".mypy_cache",
        BACKEND / ".ruff_cache",
        BACKEND / "htmlcov",
        WEB / ".next",
    ]
    for target in targets:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            say(f"  removed {target.relative_to(ROOT)}", DIM)


@task(
    "demo",
    "Clean-machine demo: install, migrate, seed, start API + worker + web",
    "run",
)
def demo() -> None:
    ensure_env_file()
    bootstrap()
    migrate()
    seed()
    catalog()
    web_install()
    say("", "")
    say("Starting API, worker and web. Press Ctrl+C to stop.", BOLD)
    say("  API    http://localhost:8000/docs", GREEN)
    say("  Web    http://localhost:3000", GREEN)
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(
            subprocess.Popen(  # noqa: S603
                [
                    uv(), "run", "--project", str(BACKEND),
                    "uvicorn", "tazama.api.app:asgi", "--factory",
                    "--host", "127.0.0.1", "--port", "8000",
                ],
                cwd=BACKEND,
            ),
        )
        procs.append(
            subprocess.Popen(  # noqa: S603
                [uv(), "run", "--project", str(BACKEND), "python", "-m", "tazama.worker"],
                cwd=BACKEND,
            ),
        )
        if (WEB / "package.json").exists():
            procs.append(
                subprocess.Popen([npm(), "run", "dev"], cwd=WEB),  # noqa: S603
            )
        while all(p.poll() is None for p in procs):
            time.sleep(1.0)
        for p in procs:
            if p.poll() not in (None, 0):
                raise TaskFailed("demo process exited", p.returncode or 1)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        say("stopping demo…", YELLOW)
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()


# ---- gates -----------------------------------------------------------------


@task("ci", "The gate: lint, types, boundaries, tests, coverage, secrets, evals", "gate")
def ci() -> None:
    steps: list[tuple[str, Callable[[], None]]] = [
        ("lint", lint),
        ("typecheck", typecheck),
        ("test", test),
        ("coverage-gate", coverage_gate),
        ("eval", evals),
        ("secrets", secrets),
    ]
    failures: list[str] = []
    for name, fn in steps:
        say(f"\n==> {name}", BOLD)
        try:
            fn()
        except TaskFailed as exc:
            failures.append(f"{name}: {exc}")
            say(f"  {name} FAILED", RED)
    say("")
    if failures:
        for failure in failures:
            say(f"RED  {failure}", RED)
        raise TaskFailed("ci", 1)
    say("GREEN  ci passed", GREEN)


@task("help", "List available targets", "gate")
def help_task() -> None:
    say(f"{BOLD}TAZAMA task runner{RESET}  (make <target> | ./make.ps1 <target> | py scripts/mk.py <target>)")
    groups: dict[str, list[Task]] = {}
    for item in TASKS.values():
        groups.setdefault(item.group, []).append(item)
    for group in ("setup", "quality", "test", "data", "run", "gate", "misc"):
        items = sorted(groups.get(group, []), key=lambda t: t.name)
        if not items:
            continue
        say(f"\n{group}:", BOLD)
        for item in items:
            say(f"  {item.name:<16} {item.help}")


def main(argv: Sequence[str]) -> int:
    requested = list(argv) or ["help"]
    for name in requested:
        item = TASKS.get(name)
        if item is None:
            say(f"unknown target: {name}", RED)
            help_task()
            return 2
        for dep in item.deps:
            TASKS[dep].fn()
        say(f"{BOLD}==> {name}{RESET}")
        try:
            item.fn()
        except TaskFailed as exc:
            say(f"{name} failed: {exc}", RED)
            return exc.code or 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
