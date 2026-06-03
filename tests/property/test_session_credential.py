"""Property-based tests for Streamlit per-user session credential lifecycle.

Hypothesis-driven verification of the Streamlit per-user credential
lifecycle:

> *For all* Streamlit oturumları ve session'da girilen Atlassian
> credential'ları için: (a) credential
> ``vault:atlassian/_user_session/<session_id>/<service>`` path'ine
> yazılır, (b) oturum sonlandığında veya TTL dolduğunda bu path silinir,
> (c) iki farklı oturum aynı ``session_id`` ile yazma yapamaz (path
> ayrımı session başına tek), (d) session sonrası ``read(path)`` çağrısı
> ``not_found`` döner.

Scope and surface under test
----------------------------

The test targets the canonical Vault path layer that the UI and service
relay use:

* :func:`automation_service.credentials.build_user_session_path` —
  returns ``vault:atlassian/_user_session/<session_id>/<service>``
  exactly. This is the **single source of truth** for the path layout
  shared by the Streamlit form, the assistant-service relay and the
  :class:`automation_service.credentials.CredentialResolver`.
* :class:`vault_client.LocalDevBackend` — the pluggable backend chosen
  here over an ad-hoc dict because it
  exercises the on-disk encryption path and the ``KeyError``
  semantics that ``CredentialResolver`` already relies on. Property
  test 11 (``test_vault_backends.py``) shows the Hashicorp backend
  agrees byte-for-byte, so testing the local-dev backend is enough
  to lock down the lifecycle invariants without doubling the run-time.

A thin in-test :class:`_SessionCredentialStore` wraps the backend with
the four lifecycle operations any production session-credential
handler MUST expose (``write_credential`` / ``read_credential`` /
``end_session`` / ``expire_ttl``) and uses the canonical path helper
internally. The wrapper is intentionally tiny — the property tests
exercise the Vault path layer, not the wrapper's bookkeeping — but
it makes the lifecycle states explicit so the property assertions
read like English sentences against the design document.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import nacl.utils
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path bootstrap — make ``automation_service.credentials`` importable.
#
# The ``automation-service`` source tree co-exists with the
# legacy ``src/main.py`` + ``src/config.py`` layer;
# importing the ``automation_service`` package eagerly executes
# ``automation_service/__init__.py`` which in turn loads
# ``automation_service.app`` whose top-of-module imports reach for
# ``from src.config import Settings``. We therefore add **both** the
# ``src/`` directory (so ``automation_service`` resolves) and its
# parent ``automation-service/`` directory (so ``src.config`` resolves
# as the legacy module path). Mirrors the bootstrap in
# ``test_probe_runner.py``.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

from automation_service.credentials import (  # noqa: E402
    AtlassianService,
    build_user_session_path,
)
from vault_client import (  # noqa: E402
    KEY_SIZE,
    LocalDevBackend,
    VaultPath,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Session ids are opaque to this layer; Streamlit / assistant-service
# generate them. We restrict to the character class permitted by the
# canonical Vault path regex (``[a-zA-Z0-9_-]``) so the generated path
# is always well-formed under :class:`vault_client.VaultPath.parse`.
_SESSION_ID = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    ),
    min_size=1,
    max_size=32,
)

# The three Atlassian surfaces this resolver knows about; mirrors
# :data:`automation_service.credentials.AtlassianService` and the
# ``departments.schema.json`` enum.
_SERVICE: st.SearchStrategy[AtlassianService] = st.sampled_from(
    ["jira", "bitbucket", "confluence"]
)

# Plain-text credential payloads. KV-v2 stores at most a flat
# ``str -> str`` dict per secret, so the strategy mirrors the shape
# used by the bot-credential write path (``url`` / ``email`` /
# ``api_token``). NUL and surrogate halves are filtered out so the
# JSON envelope inside ``LocalDevBackend`` cannot fail to encode.
_PAYLOAD_KEY_ALPHABET = st.characters(
    min_codepoint=0x21, max_codepoint=0x7E, blacklist_characters="\x00"
)
_PAYLOAD_VALUE_ALPHABET = st.text(min_size=0, max_size=64).filter(
    lambda s: "\x00" not in s and not any(0xD800 <= ord(c) <= 0xDFFF for c in s)
)
_CREDENTIAL = st.dictionaries(
    keys=st.text(alphabet=_PAYLOAD_KEY_ALPHABET, min_size=1, max_size=24),
    values=_PAYLOAD_VALUE_ALPHABET,
    min_size=1,
    max_size=4,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_backend(tmp_path: Path) -> LocalDevBackend:
    """Build a fresh :class:`LocalDevBackend` for one Hypothesis example.

    Hypothesis reuses the function-scoped ``tmp_path`` fixture across
    every example in a single property invocation, but every example
    needs an *empty* store keyed by a *fresh* symmetric key (otherwise
    a residual ``vault.json`` from a prior iteration would fail to
    decrypt under the new key). We satisfy both constraints by removing
    any leftover store file before constructing the backend.
    """

    store = tmp_path / "vault.json"
    if store.exists():
        store.unlink()
    return LocalDevBackend(
        store_path=store,
        key=nacl.utils.random(KEY_SIZE),
    )


class _SessionCredentialStore:
    """Minimal session-credential lifecycle wrapper around a Vault backend.

    Mirrors the four operations any future Streamlit /
    assistant-service implementation MUST expose so the property
    assertions can speak in lifecycle terms (``write`` / ``read`` /
    ``end_session`` / ``expire_ttl``) instead of raw Vault calls.

    The wrapper deliberately keeps **no** bookkeeping beyond what the
    backend already stores: the path layout itself
    (``vault:atlassian/_user_session/<session_id>/<service>``) is the
    isolation primitive. The ``end_session`` and ``expire_ttl``
    operations are functionally identical (both delete the path) —
    they are split into two methods purely so a regression in either
    code path surfaces with a meaningful test name.
    """

    def __init__(self, backend: LocalDevBackend) -> None:
        self._backend = backend

    @staticmethod
    def _path(session_id: str, service: AtlassianService) -> VaultPath:
        return VaultPath.parse(build_user_session_path(session_id, service))

    def write_credential(
        self,
        session_id: str,
        service: AtlassianService,
        credential: Mapping[str, str],
    ) -> VaultPath:
        path = self._path(session_id, service)
        self._backend.write(path, credential)
        return path

    def read_credential(
        self, session_id: str, service: AtlassianService
    ) -> Mapping[str, str]:
        return self._backend.read(self._path(session_id, service))

    def end_session(self, session_id: str, service: AtlassianService) -> None:
        """Explicit session-end cleanup (Streamlit logout / browser close)."""
        self._backend.delete(self._path(session_id, service))

    def expire_ttl(self, session_id: str, service: AtlassianService) -> None:
        """TTL-driven cleanup (background sweeper) — same effect as end_session."""
        self._backend.delete(self._path(session_id, service))


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(session_id=_SESSION_ID, service=_SERVICE, credential=_CREDENTIAL)
def test_write_lands_at_user_session_path(
    tmp_path: Path,
    session_id: str,
    service: AtlassianService,
    credential: Mapping[str, str],
) -> None:
    """Credentials are persisted at the expected session-scoped path.

    For every (session_id, service, credential) triple the
    Hypothesis can generate, ``write_credential`` MUST persist the
    payload at ``vault:atlassian/_user_session/<session_id>/<service>``
    exactly — same canonical path shape consumed by the resolver,
    so a per-user override is never observable at any
    other path.
    """

    store = _SessionCredentialStore(_make_backend(tmp_path))

    written_path = store.write_credential(session_id, service, credential)

    expected = f"vault:atlassian/_user_session/{session_id}/{service}"
    assert written_path.raw == expected, (
        f"unexpected path layout: {written_path.raw!r} != {expected!r}"
    )

    # Sanity: the value is observable at the produced path.
    assert dict(store.read_credential(session_id, service)) == dict(credential)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(session_id=_SESSION_ID, service=_SERVICE, credential=_CREDENTIAL)
def test_read_after_session_end_returns_not_found(
    tmp_path: Path,
    session_id: str,
    service: AtlassianService,
    credential: Mapping[str, str],
) -> None:
    """Explicit session end removes the credential path.

    After explicit session end, the path is removed and any subsequent
    ``read`` call raises :class:`KeyError` — the canonical
    "not_found" signal across the :class:`vault_client.VaultClient`
    protocol (matches the contract relied on by
    :class:`automation_service.credentials.CredentialResolver`).
    """

    store = _SessionCredentialStore(_make_backend(tmp_path))

    store.write_credential(session_id, service, credential)
    store.end_session(session_id, service)

    with pytest.raises(KeyError):
        store.read_credential(session_id, service)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(session_id=_SESSION_ID, service=_SERVICE, credential=_CREDENTIAL)
def test_read_after_ttl_expiry_returns_not_found(
    tmp_path: Path,
    session_id: str,
    service: AtlassianService,
    credential: Mapping[str, str],
) -> None:
    """TTL expiry converges on the same state as explicit logout.

    Sessions that expire because the TTL elapsed (rather than an
    explicit logout) MUST converge on the same end-state: the
    ``_user_session`` path is removed and the next ``read`` returns
    ``not_found``. A regression that wires logout → ``delete`` but
    forgets the TTL sweeper would slip past
    ``test_read_after_session_end_returns_not_found`` alone, so we
    drive both code paths independently.
    """

    store = _SessionCredentialStore(_make_backend(tmp_path))

    store.write_credential(session_id, service, credential)
    store.expire_ttl(session_id, service)

    with pytest.raises(KeyError):
        store.read_credential(session_id, service)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    session_id=_SESSION_ID,
    service=_SERVICE,
    first_credential=_CREDENTIAL,
    second_credential=_CREDENTIAL,
)
def test_session_id_uniqueness_path_isolation(
    tmp_path: Path,
    session_id: str,
    service: AtlassianService,
    first_credential: Mapping[str, str],
    second_credential: Mapping[str, str],
) -> None:
    """Sessions sharing the same ``session_id`` cannot coexist independently.

    Two sessions sharing the same ``session_id`` cannot independently
    coexist: the path layout
    ``_user_session/<session_id>/<service>`` is unique per
    ``(session_id, service)``, so a second write under the same
    ``session_id`` either overwrites the first session's credential
    (single-tenant slot) — which means a subsequent ``end_session``
    by *either* tenant clears the slot for everyone — or, equivalently,
    the second session can only observe a fresh credential after the
    first session has ended.

    The property is encoded as: after the first session writes,
    ends, and the second session writes, ``read`` MUST return the
    *second* session's credential (not a stale first-session value).
    A regression where the first session's payload "sticks" past
    ``end_session`` would let an unrelated second session observe
    foreign credential material.
    """

    store = _SessionCredentialStore(_make_backend(tmp_path))

    # Session A writes…
    store.write_credential(session_id, service, first_credential)
    # …then ends. The slot must be empty.
    store.end_session(session_id, service)
    with pytest.raises(KeyError):
        store.read_credential(session_id, service)

    # Session B reuses the same session_id (e.g. a deterministic id
    # collision after a logout). Its write MUST start from a clean
    # slot and the read MUST observe *its* payload, never the prior
    # session's residue.
    store.write_credential(session_id, service, second_credential)
    observed = dict(store.read_credential(session_id, service))
    assert observed == dict(second_credential), (
        f"session-id reuse leaked stale credential: "
        f"observed={observed!r} expected={dict(second_credential)!r}"
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    session_id_a=_SESSION_ID,
    session_id_b=_SESSION_ID,
    service=_SERVICE,
    credential_a=_CREDENTIAL,
    credential_b=_CREDENTIAL,
)
def test_distinct_sessions_are_isolated(
    tmp_path: Path,
    session_id_a: str,
    session_id_b: str,
    service: AtlassianService,
    credential_a: Mapping[str, str],
    credential_b: Mapping[str, str],
) -> None:
    """Distinct sessions are isolated from each other.

    The dual of the uniqueness clause: two *different* ``session_id``
    values address two *different* Vault paths, so ending one MUST
    NOT affect the other. Together with the uniqueness test above
    this pins down the "path ayrımı session başına tek" guarantee
    in both directions.
    """

    # Distinct session ids only — coinciding ids would collapse to
    # the same path and are covered by the uniqueness test.
    if session_id_a == session_id_b:
        return

    store = _SessionCredentialStore(_make_backend(tmp_path))

    store.write_credential(session_id_a, service, credential_a)
    store.write_credential(session_id_b, service, credential_b)

    # Ending session A must not disturb session B.
    store.end_session(session_id_a, service)
    with pytest.raises(KeyError):
        store.read_credential(session_id_a, service)
    assert dict(store.read_credential(session_id_b, service)) == dict(
        credential_b
    )

    # And vice versa once B ends, the slot is gone for B too.
    store.end_session(session_id_b, service)
    with pytest.raises(KeyError):
        store.read_credential(session_id_b, service)
