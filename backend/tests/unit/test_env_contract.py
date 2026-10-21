"""The repo-level contract: .env.example, .gitignore and the three task entry points.

These are cheap tests that catch the mistakes that hurt most: a new setting that nobody
documented, a secret that becomes committable, or a make target that exists in one entry
point and not the others.
"""

from __future__ import annotations

import re
from pathlib import Path

from tazama.core.settings import Settings


def _documented(repo_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (repo_root / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.split("#", 1)[0].strip()
    return out


def test_every_setting_is_documented(repo_root: Path) -> None:
    documented = set(_documented(repo_root))
    declared = set(Settings.model_fields)
    assert declared - documented == set(), "settings missing from .env.example"
    assert documented - declared == set(), "unknown variables in .env.example"


def test_env_example_contains_no_real_secret(repo_root: Path) -> None:
    placeholder = re.compile(
        r"(?i)^$|change-me|replace-with|dev-only|localhost|127\.0\.0\.1|^[a-z0-9_.:/,-]+$",
    )
    for key, value in _documented(repo_root).items():
        if any(token in key for token in ("PASSWORD", "SECRET", "API_KEY", "SALT", "WEBHOOK")):
            assert placeholder.match(value), f"{key} looks like a real value"


def test_gitignore_protects_secrets_and_data(repo_root: Path) -> None:
    patterns = (repo_root / ".gitignore").read_text(encoding="utf-8")
    for needed in (".env", "*.db", "node_modules/", "__pycache__/", "*.pem", ".venv/"):
        assert needed in patterns, needed
    assert "!.env.example" in patterns


def test_task_entry_points_agree(repo_root: Path) -> None:
    """make, make.ps1 and scripts/mk.py must expose the same targets."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("tazama_mk", repo_root / "scripts" / "mk.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tazama_mk"] = module
    spec.loader.exec_module(module)
    runner_targets = set(module.TASKS)

    makefile = (repo_root / "Makefile").read_text(encoding="utf-8")
    # Join backslash continuations before reading the .PHONY list.
    joined = makefile.replace("\\\n", " ")
    phony_lines = [line for line in joined.splitlines() if line.startswith(".PHONY:")]
    assert phony_lines, "Makefile has no .PHONY declaration"
    make_targets = {
        target for line in phony_lines for target in line.removeprefix(".PHONY:").split()
    }
    # Every declared target must also have a recipe.
    recipe_targets = set(re.findall(r"^([a-z][a-z0-9-]*):", joined, flags=re.MULTILINE))
    assert make_targets <= recipe_targets, sorted(make_targets - recipe_targets)

    missing_in_make = runner_targets - make_targets
    unknown_in_make = make_targets - runner_targets
    assert not missing_in_make, f"Makefile is missing: {sorted(missing_in_make)}"
    assert not unknown_in_make, f"Makefile declares unknown targets: {sorted(unknown_in_make)}"

    # make.ps1 forwards everything, so it only needs to exist and delegate to mk.py.
    ps1 = (repo_root / "make.ps1").read_text(encoding="utf-8")
    assert "scripts\\mk.py" in ps1 or "scripts/mk.py" in ps1


def test_required_make_targets_exist(repo_root: Path) -> None:
    """PRD section 16 names these explicitly."""
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("tazama_mk2", repo_root / "scripts" / "mk.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["tazama_mk2"] = module
    spec.loader.exec_module(module)
    for target in ("up", "down", "demo", "test", "lint", "seed", "eval", "ci", "doctor"):
        assert target in module.TASKS, target


def test_prd_and_claude_md_are_present(repo_root: Path) -> None:
    assert (repo_root / "PRD.md").read_text(encoding="utf-8").startswith("# TAZAMA")
    claude = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
    for section in (
        "Project summary",
        "Commands",
        "Architecture rules",
        "Config rules",
        "Data rules",
        "Security rules",
        "AI rules",
        "Testing rules",
        "Git rules",
        "When stuck",
    ):
        assert section.lower() in claude.lower(), section
