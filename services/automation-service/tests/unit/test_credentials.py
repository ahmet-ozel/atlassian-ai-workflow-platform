"""Unit tests for ``automation_service.credentials.CredentialResolver``.

Covers the priority rule where a per-user credential takes precedence
over the org-default; both missing surfaces ``CredentialMissing`` with
``error_code == "credential_missing"``.

The 2x2 truth table is exercised explicitly so the priority rule is
locked down for both directions:

+------------+-------------+----------------------------+
| user_path  | org_path    | expected outcome           |
+============+=============+============================+
| present    | present     | user_session wins          |
| present    | absent      | user_session wins          |
| absent     | present     | org_default wins           |
| absent     | absent      | CredentialMissing raised   |
+------------+-------------+----------------------------+

The tests deliberately use a tiny in-memory ``VaultReader`` rather
than mocking the full ``vault_client`` package - the resolver's
contract is *only* the structural ``read(path) -> Mapping`` slice,
and exercising that slice directly keeps the test honest.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import pytest

# ---------------------------------------------------------------------------
# Path setup - make the in-tree ``src`` importable without an install.
# Mirrors the bootstrap in test_app.py / test_credential_resolver.py.
#
# We expose **both** the service root (so ``import src.config`` resolves
# from inside ``automation_service.app``) and ``src/`` itself (so the
# canonical ``automation_service`` package re-mapped under ``[wheel.sources]``
# imports cleanly without an ``hatch build``).
# ---------------------------------------------------------------------------
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from automation_service.credentials import (  # noqa: E402
    CredentialMissing,
    CredentialResolver,
    ResolvedCredential,
    build_org_default_path,
    build_user_session_path,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeVault:
    """In-memory ``VaultReader``.

    Stored under ``secrets`` keyed by full Vault path; missing paths
    raise ``KeyError`` to match the canonical
    :class:`vault_client.VaultClient` contract.
    """

    def __init__(self, secrets: dict[str, Mapping[str, str]] | None = None) -> None:
        self.secrets = dict(secrets or {})
        self.calls: list[str] = []  # ordered list of read() arguments

    def read(self, path: str) -> Mapping[str, str]:
        self.calls.append(path)
        if path not in self.secrets:
            raise KeyError(path)
        return self.secrets[path]


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

_SESSION_ID = "sess-abc123"
_DEPT_ID = "payment"
_SERVICE = "jira"

_USER_PATH = build_user_session_path(_SESSION_ID, _SERVICE)
_ORG_PATH = build_org_default_path(_DEPT_ID, _SERVICE)

_USER_SECRET: Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "alice@acme.example",
    "api_token": "user-token",
}
_ORG_SECRET: Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "bot@acme.example",
    "api_token": "org-token",
}


# ---------------------------------------------------------------------------
# Path helper sanity (cheap regression for the path layout itself)
# ---------------------------------------------------------------------------


def test_user_session_path_layout() -> None:
    assert (
        build_user_session_path("sess-1", "jira")
        == "vault:atlassian/_user_session/sess-1/jira"
    )


def test_org_default_path_layout() -> None:
    assert build_org_default_path("payment", "bitbucket") == "vault:atlassian/payment/bitbucket"


# ---------------------------------------------------------------------------
# 2x2 truth table for the priority rule
# ---------------------------------------------------------------------------


def test_resolve_user_present_org_present_prefers_user() -> None:
    """When both paths exist, the per-user override wins."""

    vault = _FakeVault({_USER_PATH: _USER_SECRET, _ORG_PATH: _ORG_SECRET})
    resolver = CredentialResolver(vault=vault)

    result = resolver.resolve(_SESSION_ID, _DEPT_ID, _SERVICE)

    assert isinstance(result, ResolvedCredential)
    assert result.source == "user_session"
    assert result.path == _USER_PATH
    assert result.data == _USER_SECRET
    # Org-default path must not be queried when the user override exists.
    assert vault.calls == [_USER_PATH]


def test_resolve_user_present_org_absent_returns_user() -> None:
    """User override is sufficient even when no org-default is registered."""

    vault = _FakeVault({_USER_PATH: _USER_SECRET})
    resolver = CredentialResolver(vault=vault)

    result = resolver.resolve(_SESSION_ID, _DEPT_ID, _SERVICE)

    assert result.source == "user_session"
    assert result.path == _USER_PATH
    assert result.data == _USER_SECRET
    assert vault.calls == [_USER_PATH]


def test_resolve_user_absent_org_present_falls_back_to_org() -> None:
    """No per-user secret -> resolver falls back to the bot credential."""

    vault = _FakeVault({_ORG_PATH: _ORG_SECRET})
    resolver = CredentialResolver(vault=vault)

    result = resolver.resolve(_SESSION_ID, _DEPT_ID, _SERVICE)

    assert result.source == "org_default"
    assert result.path == _ORG_PATH
    assert result.data == _ORG_SECRET
    # Per-user path is queried first, then the org-default path.
    assert vault.calls == [_USER_PATH, _ORG_PATH]


def test_resolve_user_absent_org_absent_raises_credential_missing() -> None:
    """Both paths empty -> ``credential_missing`` audit error code."""

    vault = _FakeVault()
    resolver = CredentialResolver(vault=vault)

    with pytest.raises(CredentialMissing) as exc_info:
        resolver.resolve(_SESSION_ID, _DEPT_ID, _SERVICE)

    err = exc_info.value
    assert err.error_code == "credential_missing"
    assert err.session_id == _SESSION_ID
    assert err.dept_id == _DEPT_ID
    assert err.service == _SERVICE
    assert err.attempted_paths == (_USER_PATH, _ORG_PATH)
    # Both paths must have been tried before raising.
    assert vault.calls == [_USER_PATH, _ORG_PATH]
    # Generic LookupError handlers should still catch it.
    assert isinstance(err, LookupError)


# ---------------------------------------------------------------------------
# Behavioural guarantees beyond the 2x2 table
# ---------------------------------------------------------------------------


def test_resolve_propagates_unexpected_vault_errors() -> None:
    """Non-KeyError exceptions are NOT swallowed as ``credential_missing``.

    A transient Vault outage must surface to the caller as the
    original error so the operator can distinguish "no credential
    registered" from "Vault is down".
    """

    class _BoomVault:
        def read(self, path: str) -> Mapping[str, str]:  # pragma: no cover - via test
            raise RuntimeError("vault HTTP 503")

    resolver = CredentialResolver(vault=_BoomVault())

    with pytest.raises(RuntimeError, match="vault HTTP 503"):
        resolver.resolve(_SESSION_ID, _DEPT_ID, _SERVICE)


def test_resolve_rejects_empty_session_id() -> None:
    resolver = CredentialResolver(vault=_FakeVault())
    with pytest.raises(ValueError, match="session_id"):
        resolver.resolve("", _DEPT_ID, _SERVICE)


def test_resolve_rejects_empty_dept_id() -> None:
    resolver = CredentialResolver(vault=_FakeVault())
    with pytest.raises(ValueError, match="dept_id"):
        resolver.resolve(_SESSION_ID, "", _SERVICE)


def test_resolve_rejects_unknown_service() -> None:
    resolver = CredentialResolver(vault=_FakeVault())
    with pytest.raises(ValueError, match="service must be one of"):
        resolver.resolve(_SESSION_ID, _DEPT_ID, "trello")  # type: ignore[arg-type]


@pytest.mark.parametrize("service", ["jira", "bitbucket", "confluence"])
def test_resolve_supports_all_atlassian_services(service: str) -> None:
    """Each supported service builds its own dedicated paths."""

    user_path = build_user_session_path(_SESSION_ID, service)  # type: ignore[arg-type]
    org_path = build_org_default_path(_DEPT_ID, service)  # type: ignore[arg-type]
    vault = _FakeVault({org_path: {"url": "x", "token": "y"}})
    resolver = CredentialResolver(vault=vault)

    result = resolver.resolve(_SESSION_ID, _DEPT_ID, service)  # type: ignore[arg-type]

    assert result.source == "org_default"
    assert result.path == org_path
    assert vault.calls == [user_path, org_path]
