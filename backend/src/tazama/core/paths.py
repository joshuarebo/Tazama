"""Where things live on disk, independent of the working directory.

The same command runs from three places — the repository root (`make`), `backend/`
(pytest, alembic) and `/app` (container) — so any relative path in configuration would
resolve differently in each. Everything that reads a path goes through here.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

#: Files that only exist at the root of this repository.
ROOT_MARKERS = ("PRD.md", "CLAUDE.md")


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """Locate the repository root by walking up from this module.

    `TAZAMA_ROOT` overrides it for deployments that lay the tree out differently; in a
    container with no marker files, the current directory is the sensible fallback.
    """
    override = os.environ.get("TAZAMA_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if all((parent / marker).exists() for marker in ROOT_MARKERS):
            return parent
    return Path.cwd()


def resolve(path_like: str | Path) -> Path:
    """Absolute path: already-absolute values are kept, relative ones anchor to the root."""
    path = Path(path_like).expanduser()
    return path if path.is_absolute() else (repo_root() / path).resolve()


def config_dir() -> Path:
    override = os.environ.get("TAZAMA_CONFIG_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else repo_root() / "config"


def data_dir() -> Path:
    return resolve(os.environ.get("TAZAMA_DATA_DIR", "data"))


def artifacts_dir() -> Path:
    return resolve(os.environ.get("TAZAMA_ARTIFACTS_DIR", "artifacts"))


def reset_path_cache() -> None:
    """Test hook for the `TAZAMA_ROOT` override."""
    repo_root.cache_clear()
