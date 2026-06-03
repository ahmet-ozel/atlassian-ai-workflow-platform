"""Unit tests for the Python ``Sensitive_Env_Key`` matcher.

Exercises ``services/admin-dashboard-api/src/lifecycle/sensitive.py``
directly. The test set is intentionally example-based; the parity and
log-redaction suites provide broader property-level checks, while the
cases below pin the *Python-side* contract:

* The expected suffix and infix patterns are present, in the documented
  order, with their source strings character-for-character
  identical to the TypeScript twin in
  ``libs/web-shared/src/sensitive.ts``.
* :func:`is_sensitive_env_key` returns ``True`` for representative
  Sensitive_Env_Key examples and ``False`` for clearly non-sensitive
  keys.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest


# ---------------------------------------------------------------------------
# Module loader — admin-dashboard-api isn't on ``pythonpath`` (its ``src/``
# is not a shared library), so we import the lifecycle package by file path
# under a unique alias just like ``tests/property/test_health_contract.py``
# does for the FastAPI ``main`` modules. This keeps the test independent
# of any future changes to ``pytest.ini`` ``pythonpath`` and avoids
# clashing with sibling services that also expose a ``src`` package.
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_sensitive_module() -> ModuleType:
    """Import ``services/admin-dashboard-api/src/lifecycle/sensitive.py``.

    Uses ``importlib.util.spec_from_file_location`` with a unique alias so
    repeated test runs reuse a single module instance and don't collide
    with other services' ``src`` packages.
    """

    alias = "_msf_admin_dashboard_api.lifecycle.sensitive"
    if alias in sys.modules:
        return sys.modules[alias]

    src_path = (
        _WORKSPACE_ROOT
        / "services"
        / "admin-dashboard-api"
        / "src"
        / "lifecycle"
        / "sensitive.py"
    )
    if not src_path.is_file():
        raise FileNotFoundError(f"Expected sensitive.py at {src_path}")

    spec = importlib.util.spec_from_file_location(alias, str(src_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


sensitive = _load_sensitive_module()
SENSITIVE_ENV_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    sensitive.SENSITIVE_ENV_KEY_PATTERNS
)
is_sensitive_env_key = sensitive.is_sensitive_env_key


# ---------------------------------------------------------------------------
# Pattern list shape — order and source strings are part of the contract
# ---------------------------------------------------------------------------


#: Source strings expected on both sides of the TS↔Python twin module,
#: in the documented order. Any change here
#: must be applied to ``libs/web-shared/src/sensitive.ts`` in lock-step,
#: otherwise the parity suite will fail.
EXPECTED_PATTERN_SOURCES: tuple[str, ...] = (
    r"_TOKEN$",
    r"_KEY$",
    r"_SECRET$",
    r"_PASSWORD$",
    r"_DSN$",
    r"_CREDENTIAL$",
    r"_PRIVATE_",
)


def test_sensitive_env_key_patterns_is_a_tuple_of_compiled_patterns() -> None:
    """Module exposes the canonical tuple of compiled regexes."""

    assert isinstance(SENSITIVE_ENV_KEY_PATTERNS, tuple)
    assert all(
        isinstance(pattern, re.Pattern) for pattern in SENSITIVE_ENV_KEY_PATTERNS
    )


def test_sensitive_env_key_patterns_have_documented_sources_in_order() -> None:
    """Source strings match the shared glossary character-by-character.

    The TypeScript twin uses the same literals in the same order; if
    this test fails, update both files together so parity keeps
    passing.
    """

    actual_sources = tuple(p.pattern for p in SENSITIVE_ENV_KEY_PATTERNS)

    assert actual_sources == EXPECTED_PATTERN_SOURCES


# ---------------------------------------------------------------------------
# is_sensitive_env_key — positive cases (one per documented pattern)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        # _TOKEN$
        "VAULT_TOKEN",
        "GITHUB_TOKEN",
        # _KEY$
        "API_KEY",
        "OPENAI_API_KEY",
        # _SECRET$
        "JWT_SECRET",
        "WEBHOOK_SECRET",
        # _PASSWORD$
        "DB_PASSWORD",
        "ADMIN_PASSWORD",
        # _DSN$
        "POSTGRES_DSN",
        "SENTRY_DSN",
        # _CREDENTIAL$
        "AWS_CREDENTIAL",
        "GCP_CREDENTIAL",
        # _PRIVATE_  (infix — anywhere in the key)
        "DB_PRIVATE_HOST",
        "SERVER_PRIVATE_KEY_PATH",
    ],
)
def test_is_sensitive_env_key_matches_documented_examples(key: str) -> None:
    """Each Sensitive_Env_Key suffix/infix is recognised."""

    assert is_sensitive_env_key(key) is True


# ---------------------------------------------------------------------------
# is_sensitive_env_key — negative cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        # No leading underscore → glob ``*_TOKEN`` doesn't match.
        "TOKEN",
        "KEY",
        "SECRET",
        "PASSWORD",
        "DSN",
        "CREDENTIAL",
        # Sensitive token in the middle of a longer suffix.
        "TOKENIZER_PATH",
        "KEYBOARD_LAYOUT",
        "SECRETARY_EMAIL",
        # Plainly non-sensitive operational keys.
        "PORT",
        "LOG_LEVEL",
        "CLIENT_SOURCE",
        "TEMPORAL_HOST",
        "VAULT_ADDR",
        # ``PRIVATE`` without trailing underscore should not match the
        # ``_PRIVATE_`` infix pattern.
        "USE_PRIVATE",
        "_PRIVATE",
        # Empty string is benign; nothing to match.
        "",
    ],
)
def test_is_sensitive_env_key_rejects_non_sensitive_keys(key: str) -> None:
    """Non-sensitive keys are reported as such."""

    assert is_sensitive_env_key(key) is False


# ---------------------------------------------------------------------------
# Determinism / purity sanity check
# ---------------------------------------------------------------------------


def test_is_sensitive_env_key_is_pure_and_deterministic() -> None:
    """Calling the matcher twice on the same key returns the same answer.

    The module is supposed to be pure (no I/O, no globals besides the
    compiled patterns). This guards against accidentally introducing a
    cache that mutates state on read.
    """

    for key in ("API_KEY", "LOG_LEVEL", "DB_PRIVATE_HOST", "PORT"):
        assert is_sensitive_env_key(key) == is_sensitive_env_key(key)
