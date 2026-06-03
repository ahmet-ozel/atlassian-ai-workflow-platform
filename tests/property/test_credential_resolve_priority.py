"""invariant 17 — Credential resolve priority + audit emission.



Hypothesis-driven verification of the audit-emitting credential
resolver wrapper.

Scope and surface under test
----------------------------

The base resolver
(:class:`automation_service.credentials.CredentialResolver`) already
owns the per-user → org-default *priority decision* — invariant
(``test_credential_inject.py`` §invariant) pins that contract
across the full 2x2 matrix of
``(per_user_present, org_default_present)``.

invariant layers one extra concern on top: the **audit emission**
contract owned by:class:`assistant_service.credentials.AuditingCredentialResolver`. For every resolution outcome the wrapper writes exactly
one audit event whose ``action`` field discriminates the path that
was used (or the failure mode). The four invariants below pin that
contract:

* Per-user override hit emits ``credential_resolved_per_user_session``
 (``result="ok"``); the underlying resolver returned source
 ``"user_session"`` so the wrapper MUST NOT mis-tag the event.
* Org-default fallback hit emits ``credential_resolved_org_default``
 (``result="ok"``); the underlying resolver returned source
 ``"org_default"``.
* Both paths missing emits ``credential_resolve_failed``
 (``result="error"``) and re-raises:class:`CredentialNotFoundError`
 (a documented public alias of the base:class:`CredentialMissing`); the audit row is written **before**
 the exception escapes so a caller dropping the exception still
 leaves the failure observable in the audit log.
* The audit ``payload`` dict carries diagnostic identifiers
 only (``service``, ``session_id``, ``vault_path`` /
 ``attempted_paths``) and **never** any plain-credential token /
 password / secret value.

The determinism invariant pins re-running the
wrapper with the same ``(session_id, dept_id, service)`` triple
against the same Vault state produces the same output (same
``ResolvedCredential.path`` / ``ResolvedCredential.source`` and the
same audit ``action`` / ``payload``).

Strategies stay tight: the test only varies the boolean inputs that
drive the audit decision — ``(has_user_session_cred,
has_org_default_cred)`` — plus path-safe identifier strings.
The Vault payload values are kept fixed because invariant already
covers payload pass-through; varying them here would burn Hypothesis
budget without improving coverage of the audit contract.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — the assistant-service is *not* a shared library so
# it is not on ``pytest.ini``'s ``pythonpath``. We insert its source root
# manually so ``from src.credentials import...`` resolves. We also need
# ``automation_service.credentials`` (the base resolver) so the
# wrapper's import chain succeeds; that module lives under
# ``services/automation-service/src/`` and is added the same way.
# Mirrors the bootstrap used by ``test_session_credential.py`` and
# ``test_write_action_intercept.py``.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS = (
    _REPO_ROOT / "libs" / "audit_logger" / "src",
    _REPO_ROOT / "libs" / "vault_client" / "src",
)
for _src in _LIB_SRC_DIRS:
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

_AUTOMATION_ROOT = _REPO_ROOT / "services" / "automation-service"
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (_AUTOMATION_ROOT, _AUTOMATION_SRC):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_ASSISTANT_SERVICE_ROOT = _REPO_ROOT / "services" / "assistant-service"
if str(_ASSISTANT_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ASSISTANT_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from audit_logger import AuditEvent  # noqa: E402
from automation_service.credentials import (  # noqa: E402
    AtlassianService,
    CredentialMissing,
    CredentialResolver,
    build_org_default_path,
    build_user_session_path,
)
from src.credentials import (  # noqa: E402
    AUDIT_ACTION_FAILED,
    AUDIT_ACTION_ORG_DEFAULT,
    AUDIT_ACTION_PER_USER,
    AuditingCredentialResolver,
    CredentialNotFoundError,
    PLAIN_CREDENTIAL_KEYS,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Session ids are opaque tokens minted by the Streamlit / assistant
# layer; in practice URL-safe base64 with optional hyphens / underscores.
# Restrict to ``[A-Za-z0-9_-]`` so generated strings always survive the
# Vault path canonical regex (mirrors P15's strategy).
_SESSION_ID = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
        min_codepoint=0x30,
        max_codepoint=0x7A,
    ),
    min_size=1,
    max_size=32,
)

# Department ids — same character class, non-empty (base
# resolver rejects empty strings with ValueError; we don't want to
# accidentally exercise that pre-condition path here).
_DEPT_ID = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"),
        whitelist_characters="-_",
        min_codepoint=0x30,
        max_codepoint=0x7A,
    ),
    min_size=1,
    max_size=32,
)

_SERVICE: st.SearchStrategy[AtlassianService] = st.sampled_from(
    ["jira", "bitbucket", "confluence"]
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


# Distinct sentinel payloads so "which path wonsection" is unambiguous in
# assertions. Both contain plain-credential keys
# (``personal_token`` / ``api_token``) so the no-leak invariant
# is genuinely exercised — if the wrapper ever copied
# ``data`` into the audit payload, the property would catch it.
_USER_SECRET: Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "alice@acme.example",
    "personal_token": "USER-SESSION-TOKEN-DO-NOT-LEAK",
    "api_token": "USER-SESSION-API-TOKEN-DO-NOT-LEAK",
}
_ORG_SECRET: Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "bot@acme.example",
    "personal_token": "ORG-DEFAULT-TOKEN-DO-NOT-LEAK",
    "api_token": "ORG-DEFAULT-API-TOKEN-DO-NOT-LEAK",
}

# All distinct plain-credential values that any audit payload must
# never contain. We assert against this combined set so the test
# would catch a regression that only redacts ``personal_token``
# but accidentally surfaces ``api_token``, etc.
_FORBIDDEN_VALUES: frozenset[str] = frozenset(
    {
        _USER_SECRET["personal_token"],
        _USER_SECRET["api_token"],
        _ORG_SECRET["personal_token"],
        _ORG_SECRET["api_token"],
    }
)


class _FakeVault:
    """Tiny in-memory ``VaultReader`` (KeyError on miss).

 Records every read so determinism can assert two
 consecutive calls hit the same path. Mirrors the protocol
 contract documented in:mod:`automation_service.credentials`.
 """

    def __init__(self, secrets: Mapping[str, Mapping[str, str]] | None = None) -> None:
        self.secrets: dict[str, Mapping[str, str]] = dict(secrets or {})
        self.calls: list[str] = []

    def read(self, path: str) -> Mapping[str, str]:
        self.calls.append(path)
        if path not in self.secrets:
            raise KeyError(path)
        return self.secrets[path]


class _CapturingAudit:
    """List-backed:class:`AuditWriterProtocol` for assertion."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _build_resolver(
    secrets: Mapping[str, Mapping[str, str]],
    *,
    actor_id: str = "user-alice",
    actor_role: str = "lead",
) -> tuple[AuditingCredentialResolver, _FakeVault, _CapturingAudit]:
    """Build the wrapper + its collaborators with a fixed clock.

 Using a fixed clock keeps ``timestamp`` stable across the two
 calls in the determinism property. The clock value
 itself is irrelevant to the audit contract; only the action /
 payload / result fields are asserted on.
 """

    vault = _FakeVault(secrets)
    audit = _CapturingAudit()
    fixed_now = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    resolver = AuditingCredentialResolver(
        resolver=CredentialResolver(vault=vault),
        audit=audit,
        actor_id=actor_id,
        actor_role=actor_role,
        clock=lambda: fixed_now,
    )
    return resolver, vault, audit


def _assert_no_plain_credential(payload: Mapping[str, object] | None) -> None:
    """Assert no Vault ``data`` value bleeds into the audit payload.

 The audit row carries diagnostic identifiers only.
 We check both shapes the leak could take:

 1. A ``data`` / token-shaped key landed in the payload.
 2. A token *value* appears anywhere in the payload's stringified
 form (defends against a future regression that nests the
 resolver result under an unexpected key).
 """

    assert payload is not None
    # Shape: forbidden keys.
    for key in payload.keys():
        assert key not in PLAIN_CREDENTIAL_KEYS, (
            f"invariant violated: audit payload contains plain-credential "
            f"key {key!r}. payload={dict(payload)!r}"
        )
    # Shape: forbidden values, anywhere in the rendered payload.
    rendered = repr(payload)
    for forbidden in _FORBIDDEN_VALUES:
        assert forbidden not in rendered, (
            f"invariant violated: audit payload contains plain-credential "
            f"value {forbidden!r}. payload={dict(payload)!r}"
        )


# ---------------------------------------------------------------------------
# invariant — per-user override hit emits the per-user audit action
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_SESSION_ID, dept_id=_DEPT_ID, service=_SERVICE)
def test_p17a_per_user_hit_emits_per_user_session_audit(
    session_id: str, dept_id: str, service: AtlassianService
) -> None:
    """invariant — per-user hit emits ``credential_resolved_per_user_session``.



 For every ``(session_id, dept_id, service)`` triple where the
 per-user override exists at
 ``vault:atlassian/_user_session/<session_id>/<service>``, the
 wrapper MUST emit exactly one audit event whose ``action`` is
 the per-user session action and whose ``result`` is ``"ok"``.

 The org-default path may or may not exist — the audit decision
 is owned solely by which path *produced* the credential
 (invariant short-circuits on the per-user hit, so the
 org-default state is irrelevant here).
 """

    user_path = build_user_session_path(session_id, service)
    org_path = build_org_default_path(dept_id, service)

    # Both paths populated — proves the wrapper does not mis-tag
    # the event by inspecting org-default state.
    resolver, _vault, audit = _build_resolver(
        {user_path: _USER_SECRET, org_path: _ORG_SECRET}
    )

    result = resolver.resolve(session_id, dept_id, service)

    assert result.source == "user_session"
    assert result.path == user_path
    assert result.data == _USER_SECRET

    assert len(audit.events) == 1, (
        f"invariant violated: expected exactly one audit event, "
        f"got {len(audit.events)} ({audit.events!r})."
    )
    event = audit.events[0]
    assert event.action == AUDIT_ACTION_PER_USER, (
        f"invariant violated: expected action="
        f"{AUDIT_ACTION_PER_USER!r}, got {event.action!r}."
    )
    assert event.result == "ok"
    assert event.dept_id == dept_id
    # Payload identifiers — diagnostic only, no secret material.
    assert event.payload is not None
    assert event.payload.get("vault_path") == user_path
    assert event.payload.get("source") == "user_session"
    assert event.payload.get("service") == service
    _assert_no_plain_credential(event.payload)


# ---------------------------------------------------------------------------
# invariant — org-default hit emits the org-default audit action
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_SESSION_ID, dept_id=_DEPT_ID, service=_SERVICE)
def test_p17b_org_default_fallback_emits_org_default_audit(
    session_id: str, dept_id: str, service: AtlassianService
) -> None:
    """invariant — org-default fallback emits ``credential_resolved_org_default``.



 When the per-user path is absent and the org-default path
 populated, the wrapper MUST emit exactly one audit event whose
 ``action`` is the org-default action and whose ``result`` is
 ``"ok"``. The payload's ``vault_path`` MUST point at the
 org-default path so dashboards can correlate the resolution
 with the bot credential.
 """

    org_path = build_org_default_path(dept_id, service)

    resolver, _vault, audit = _build_resolver({org_path: _ORG_SECRET})

    result = resolver.resolve(session_id, dept_id, service)

    assert result.source == "org_default"
    assert result.path == org_path
    assert result.data == _ORG_SECRET

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == AUDIT_ACTION_ORG_DEFAULT, (
        f"invariant violated: expected action="
        f"{AUDIT_ACTION_ORG_DEFAULT!r}, got {event.action!r}."
    )
    assert event.result == "ok"
    assert event.dept_id == dept_id
    assert event.payload is not None
    assert event.payload.get("vault_path") == org_path
    assert event.payload.get("source") == "org_default"
    assert event.payload.get("service") == service
    _assert_no_plain_credential(event.payload)


# ---------------------------------------------------------------------------
# invariant — both missing → CredentialNotFoundError + failure audit
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_SESSION_ID, dept_id=_DEPT_ID, service=_SERVICE)
def test_p17c_both_missing_raises_and_emits_failure_audit(
    session_id: str, dept_id: str, service: AtlassianService
) -> None:
    """invariant — both missing → ``credential_resolve_failed`` + raise.



 The wrapper writes the failure audit **before** re-raising the
 exception so a caller dropping the exception still leaves the
 failure observable in the audit log. The exception type is the
 documented public alias:class:`CredentialNotFoundError` which
 subclasses the base:class:`CredentialMissing` so callers
 that catch the base type keep working.
 """

    user_path = build_user_session_path(session_id, service)
    org_path = build_org_default_path(dept_id, service)

    # Empty Vault — both paths miss.
    resolver, _vault, audit = _build_resolver({})

    with pytest.raises(CredentialNotFoundError) as exc_info:
        resolver.resolve(session_id, dept_id, service)

    err = exc_info.value
    # Public alias preserves the base resolver contract.
    assert isinstance(err, CredentialMissing)
    assert err.attempted_paths == (user_path, org_path)

    # Exactly one failure audit event.
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == AUDIT_ACTION_FAILED, (
        f"invariant violated: expected action="
        f"{AUDIT_ACTION_FAILED!r}, got {event.action!r}."
    )
    assert event.result == "error"
    assert event.dept_id == dept_id
    assert event.payload is not None
    # Both paths recorded (path strings only — no secret material).
    attempted = event.payload.get("attempted_paths")
    assert attempted == [user_path, org_path], (
        f"invariant violated: failure payload attempted_paths "
        f"mismatch. expected={[user_path, org_path]!r}, got={attempted!r}"
    )
    assert event.payload.get("service") == service
    _assert_no_plain_credential(event.payload)


# ---------------------------------------------------------------------------
# invariant — full 2x2 matrix: payload never carries plain credential
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    session_id=_SESSION_ID,
    dept_id=_DEPT_ID,
    service=_SERVICE,
    has_user=st.booleans(),
    has_org=st.booleans(),
)
def test_p17d_audit_payload_never_contains_plain_credential(
    session_id: str,
    dept_id: str,
    service: AtlassianService,
    has_user: bool,
    has_org: bool,
) -> None:
    """invariant — audit payload never leaks plain-credential material.



 For *every* combination of ``(has_user, has_org)`` — including
 the failure case — the resulting audit ``payload`` MUST NOT
 contain any forbidden token / password / secret key, and MUST
 NOT contain any of the plain-credential *values* the resolver
 fetched from Vault. This is the application-layer counterpart to
 the log redaction filter.
 """

    user_path = build_user_session_path(session_id, service)
    org_path = build_org_default_path(dept_id, service)

    secrets: dict[str, Mapping[str, str]] = {}
    if has_user:
        secrets[user_path] = _USER_SECRET
    if has_org:
        secrets[org_path] = _ORG_SECRET

    resolver, _vault, audit = _build_resolver(secrets)

    if has_user or has_org:
        resolver.resolve(session_id, dept_id, service)
    else:
        with pytest.raises(CredentialNotFoundError):
            resolver.resolve(session_id, dept_id, service)

    # Exactly one audit event regardless of branch.
    assert len(audit.events) == 1
    _assert_no_plain_credential(audit.events[0].payload)


# ---------------------------------------------------------------------------
# invariant — determinism: same inputs ⇒ same path read + same audit
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    session_id=_SESSION_ID,
    dept_id=_DEPT_ID,
    service=_SERVICE,
    has_user=st.booleans(),
    has_org=st.booleans(),
)
def test_p17e_resolution_is_deterministic(
    session_id: str,
    dept_id: str,
    service: AtlassianService,
    has_user: bool,
    has_org: bool,
) -> None:
    """invariant — two calls produce the same path + audit action.



 Calling:meth:`resolve` twice with identical
 ``(session_id, dept_id, service)`` against an unchanged Vault
 state MUST produce identical observable behaviour:

 * The resolver reads the same path(s) on both calls (vault.calls
 doubles in length and the second half mirrors the first).
 * The audit emits the same ``action`` / ``result`` / ``payload``
 shape on both calls (timestamps may differ in production but
 we pin the clock for the property; the *kinds* of fields are
 what matter for the determinism contract).
 """

    user_path = build_user_session_path(session_id, service)
    org_path = build_org_default_path(dept_id, service)

    secrets: dict[str, Mapping[str, str]] = {}
    if has_user:
        secrets[user_path] = _USER_SECRET
    if has_org:
        secrets[org_path] = _ORG_SECRET

    resolver, vault, audit = _build_resolver(secrets)

    # Two consecutive calls.
    if has_user or has_org:
        first = resolver.resolve(session_id, dept_id, service)
        second = resolver.resolve(session_id, dept_id, service)
        # ResolvedCredential is frozen, so equality is structural.
        assert first.source == second.source
        assert first.path == second.path
        assert first.data == second.data
    else:
        with pytest.raises(CredentialNotFoundError):
            resolver.resolve(session_id, dept_id, service)
        with pytest.raises(CredentialNotFoundError):
            resolver.resolve(session_id, dept_id, service)

    # Vault was queried via the same ordered path sequence on each
    # call — the second call's reads mirror the first call's.
    n = len(vault.calls)
    assert n % 2 == 0, (
        f"invariant violated: vault.calls length is odd ({n}); "
        f"the two resolver invocations did not produce a symmetric "
        f"call sequence."
    )
    half = n // 2
    assert vault.calls[:half] == vault.calls[half:], (
        f"invariant violated: vault.calls are not deterministic. "
        f"first half={vault.calls[:half]!r}, "
        f"second half={vault.calls[half:]!r}."
    )

    # Audit events agree on the action / result / payload-shape.
    assert len(audit.events) == 2
    a, b = audit.events
    assert a.action == b.action
    assert a.result == b.result
    assert a.dept_id == b.dept_id
    assert a.payload == b.payload, (
        f"invariant violated: audit payloads diverge across "
        f"identical resolver inputs. first={a.payload!r}, "
        f"second={b.payload!r}."
    )
