"""A fresh clone must produce a working `.env`.

Two bugs this covers, both found by running it rather than reading it:

1. `.env.example` is committed, so its secrets are placeholders. Copying it verbatim now
   fails startup, because placeholder rejection is unconditional. `ensure_env_file` has to
   mint real values instead, which also means two clones never share a signing key.
2. `scripts/mk.py` defines a module-level `secrets()` task (the secret-scan target), which
   shadows the stdlib module. `import secrets` followed by `secrets.token_urlsafe` raised
   `AttributeError` at runtime, so `make demo` broke on every fresh clone while every
   existing checkout kept working because it already had a `.env`.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load_mk():
    spec = importlib.util.spec_from_file_location("_mk_under_test", REPO / "scripts" / "mk.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mk_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fresh_clone(tmp_path: Path):
    """A directory holding only `.env.example`, like a clone before the first run."""
    shutil.copy(REPO / ".env.example", tmp_path / ".env.example")
    mk = _load_mk()
    original = mk.ROOT
    mk.ROOT = tmp_path
    yield mk, tmp_path
    mk.ROOT = original


def test_bootstrap_generates_real_secrets(fresh_clone) -> None:
    mk, root = fresh_clone
    mk.ensure_env_file()

    values = {}
    for line in (root / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.split("#")[0].strip()

    for name in ("JWT_SECRET", "PSEUDONYM_SALT"):
        secret = values[name]
        assert "replace" not in secret.lower(), f"{name} is still the committed placeholder"
        assert len(secret) >= 32, f"{name} is only {len(secret)} characters"


def test_two_clones_do_not_share_a_signing_key(tmp_path: Path) -> None:
    """The reason for generating rather than shipping a fixed dev value."""
    mk = _load_mk()
    original = mk.ROOT
    produced = []
    try:
        for name in ("a", "b"):
            root = tmp_path / name
            root.mkdir()
            shutil.copy(REPO / ".env.example", root / ".env.example")
            mk.ROOT = root
            mk.ensure_env_file()
            for line in (root / ".env").read_text(encoding="utf-8").splitlines():
                if line.startswith("JWT_SECRET="):
                    produced.append(line.partition("=")[2])
    finally:
        mk.ROOT = original

    assert len(produced) == 2
    assert produced[0] != produced[1]


def test_an_existing_env_is_never_overwritten(fresh_clone) -> None:
    """Re-running `make demo` must not rotate the key out from under a running stack."""
    mk, root = fresh_clone
    (root / ".env").write_text("JWT_SECRET=already-here-and-must-survive\n", encoding="utf-8")
    mk.ensure_env_file()
    assert (root / ".env").read_text(encoding="utf-8") == (
        "JWT_SECRET=already-here-and-must-survive\n"
    )


def test_the_generated_env_actually_loads(fresh_clone, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end the other tests do not reach: the settings validator accepts the result."""
    from tazama.core.settings import get_settings

    mk, root = fresh_clone
    mk.ensure_env_file()
    monkeypatch.setenv("TAZAMA_ENV_FILE", str(root / ".env"))
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert "replace" not in settings.JWT_SECRET.get_secret_value().lower()
        assert "replace" not in settings.PSEUDONYM_SALT.get_secret_value().lower()
    finally:
        get_settings.cache_clear()
