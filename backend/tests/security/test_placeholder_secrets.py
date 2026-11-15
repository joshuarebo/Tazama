"""A placeholder signing key or salt must never be accepted, in any environment.

This exists because the chain below nearly shipped:

1. `deploy/openshift/base/platform.yaml` carried a Secret with `JWT_SECRET:
   "REPLACE_IN_CLUSTER"`, and that file was listed in the kustomize resources, so
   `oc apply -k` would have overwritten the real cluster Secret on every deploy.
2. The ConfigMap did not set `APP_ENV`, so it fell back to its default of `"local"`.
3. The validator that rejects placeholder secrets returned early for `"local"`.
4. `"REPLACE_IN_CLUSTER"` matched none of the placeholder patterns that were checked.

The result would have been a production deployment with a signing key readable by anyone
with repository access, which is admin-token forgery, and a known pseudonymisation salt,
which makes every pseudonym reversible.

Each link is now broken independently. This file covers the last two.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tazama.core.settings import PLACEHOLDER_TOKENS, Settings


def _settings(**overrides: str) -> Settings:
    base = {
        "APP_ENV": "local",
        "APP_MODE": "demo",
        "AUTH_MODE": "dev",
        "DB_BACKEND": "sqlite",
        "JWT_SECRET": "a-real-enough-local-secret-value",
        "PSEUDONYM_SALT": "a-real-enough-local-salt-value",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_a_sane_local_configuration_still_loads() -> None:
    assert _settings().APP_ENV == "local"


@pytest.mark.parametrize("token", PLACEHOLDER_TOKENS)
def test_every_placeholder_token_is_refused_for_the_signing_key(token: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(JWT_SECRET=f"prefix-{token}-suffix-padding-padding")


@pytest.mark.parametrize("token", PLACEHOLDER_TOKENS)
def test_every_placeholder_token_is_refused_for_the_salt(token: str) -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(PSEUDONYM_SALT=f"prefix-{token}-suffix-padding-padding")


def test_the_exact_manifest_value_is_refused() -> None:
    """The literal string that was in the manifest, in the environment where the other
    checks are skipped. This is the regression."""
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(APP_ENV="local", JWT_SECRET="REPLACE_IN_CLUSTER")


def test_the_check_is_case_insensitive() -> None:
    for variant in ("REPLACE_IN_CLUSTER", "Replace-Me-Please", "replace_me"):
        with pytest.raises(ValidationError, match="placeholder"):
            _settings(JWT_SECRET=variant)


def test_a_placeholder_is_refused_in_production_too() -> None:
    with pytest.raises(ValidationError):
        _settings(APP_ENV="prod", AUTH_MODE="oidc", JWT_SECRET="REPLACE_IN_CLUSTER")


def test_the_committed_env_example_would_be_refused() -> None:
    """`.env.example` is committed, so its secrets are placeholders by design.

    Copying it verbatim into `.env` must fail rather than quietly becoming a signing key
    that is identical across every clone of the repository. `scripts/mk.py` generates real
    values instead, and this asserts the refusal that makes that necessary.
    """
    with pytest.raises(ValidationError, match="placeholder"):
        _settings(JWT_SECRET="replace-with-32-plus-random-chars-before-use")
