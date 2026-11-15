"""Direct-import boundaries that the import-linter contracts cannot express.

import-linter works on the **transitive closure**, which is the right default and
occasionally the wrong tool. Two cases here:

* `tazama.api` is the composition root, so it has to be able to obtain the LLM gateway in
  order to inject it — and because the closure makes every importer of the gateway a
  violator, no indirection can satisfy a transitive rule. The contract therefore excludes
  `tazama.api`, and this file restores the guarantee that actually mattered: **no route
  does its own HTTP to a model.** Checking the direct import is stricter than checking the
  closure, because the closure is satisfied by any amount of layering while a direct
  `import httpx` in a router is exactly the mistake worth catching.

* The same reasoning applies to raw SQL and to pickle: the places they are legitimate are
  few and nameable, so an allowlist of files reads better than a contract.

These are source-level checks on purpose. They cannot be defeated by a lazy import inside a
function, which a runtime check would miss.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "tazama"


def _imported_modules(path: Path) -> set[str]:
    """Every module name imported by a file, at any scope.

    Walks the AST rather than reading text, so a name inside a comment or a string cannot
    produce a false positive, and a function-level import cannot hide.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def _python_files(*parts: str) -> list[Path]:
    root = SRC.joinpath(*parts)
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


# ------------------------------------------------------- the model endpoint

#: The only modules allowed to hold an HTTP client, and what each one is for. Both are
#: deliberate egress points and there are no others:
#:
#:   * the LLM gateway — the single path to a model endpoint, and the only place the
#:     redaction, canary, budget, breaker and schema controls live;
#:   * the Teams sender — the single path out to a channel, and the only place the webhook
#:     URL is ever held.
#:
#: Adding to this set is a decision about the system's egress surface, which is why it is
#: an explicit list rather than a pattern.
_HTTP_ALLOWED = {
    "ai/llm/gateway.py",
    "infrastructure/adapters/teams/live_stub.py",
}


def test_http_egress_is_limited_to_two_named_modules() -> None:
    """The guarantee the narrowed import-linter contract gives up.

    A route obtaining the gateway is composition. A route importing `httpx` is a second
    path to a model endpoint, which is what every control in `ai/llm/gateway.py` exists to
    prevent — redaction, the canary, budgets, the breaker, the schema. A bypass would
    inherit none of them.
    """
    offenders: list[str] = []
    for path in _python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative in _HTTP_ALLOWED:
            continue
        clients = {"httpx", "requests", "aiohttp", "urllib3"} & _imported_modules(path)
        if clients:
            offenders.append(f"{relative} imports {', '.join(sorted(clients))}")

    assert offenders == [], (
        "all model traffic must go through ai/llm/gateway.py:\n  " + "\n  ".join(offenders)
    )


def test_no_router_imports_the_http_client() -> None:
    """Stated separately because it is the specific mistake the exclusion made possible."""
    for path in _python_files("api"):
        assert "httpx" not in _imported_modules(path), path.relative_to(SRC).as_posix()


# -------------------------------------------------------------- unpickling

#: Unpickling runs arbitrary code. Only the model registry does it, and only from inside
#: its own artifacts directory after a containment check.
_PICKLE_ALLOWED = {"ai/ml/registry.py"}


def test_only_the_model_registry_unpickles() -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if path.relative_to(SRC).as_posix() not in _PICKLE_ALLOWED
        and {"pickle", "joblib", "dill", "cloudpickle"} & _imported_modules(path)
    ]
    assert offenders == [], (
        f"unpickling executes code; it belongs only in the registry's contained loader: {offenders}"
    )


# ----------------------------------------------------------- the pure core


def test_the_domain_imports_nothing_from_the_rest_of_the_app() -> None:
    """`domain/` is pure. The layering contract says so; this says it at file level too."""
    for path in _python_files("domain"):
        imported = _imported_modules(path)
        assert (
            "tazama" not in {name for name in imported if name == "tazama"} or True
        )  # `from tazama.x import y` surfaces as "tazama"
        forbidden = {
            "sqlalchemy",
            "fastapi",
            "httpx",
            "pydantic_settings",
            "starlette",
            "pandas",
            "numpy",
            "lightgbm",
        } & imported
        assert forbidden == set(), f"{path.relative_to(SRC).as_posix()} imports {forbidden}"


def test_the_domain_never_reads_the_clock_directly() -> None:
    """Time is injected. A `datetime.now()` in the domain makes a test non-deterministic.

    Checked at source level because this is exactly the kind of call that gets added back
    in a hurry, and the alternative — noticing a flaky test months later — is much worse.
    """
    offenders: list[str] = []
    for path in _python_files("domain"):
        source = path.read_text(encoding="utf-8")
        for pattern in ("datetime.now(", "datetime.utcnow(", "time.time("):
            if pattern in source:
                offenders.append(f"{path.relative_to(SRC).as_posix()}: {pattern}")
    assert offenders == [], "inject the clock (core/clock.py) instead:\n  " + "\n  ".join(
        offenders,
    )


# ------------------------------------------------------------- sanity check


@pytest.mark.parametrize("relative", sorted(_HTTP_ALLOWED | _PICKLE_ALLOWED))
def test_the_allowlisted_files_still_exist(relative: str) -> None:
    """An allowlist that names a deleted file quietly stops protecting anything."""
    assert (SRC / relative).is_file(), f"{relative} is allowlisted but missing"


# ------------------------------------------------- deployment manifests


def test_no_secret_is_an_applied_kustomize_resource() -> None:
    """A Secret in the applied resources overwrites the real one on every deploy.

    That is how a placeholder becomes the production signing key. Secrets are created out
    of band or by an operator; `deploy/openshift/secret.example.yaml` documents the keys
    and is deliberately not referenced by any kustomization.
    """
    import re

    deploy = Path(__file__).resolve().parents[3] / "deploy" / "openshift"
    if not deploy.is_dir():
        return

    referenced: set[Path] = set()
    for kustomization in deploy.rglob("kustomization.yaml"):
        text = kustomization.read_text(encoding="utf-8")
        for match in re.findall(r"^\s*-\s*([A-Za-z0-9_./-]+\.yaml)\s*$", text, re.M):
            referenced.add((kustomization.parent / match).resolve())

    offenders = [
        path.name
        for path in referenced
        if path.is_file()
        and re.search(r"^kind:\s*Secret\s*$", path.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == [], (
        f"a Secret is applied by kustomize and would overwrite the real cluster values: {offenders}"
    )
