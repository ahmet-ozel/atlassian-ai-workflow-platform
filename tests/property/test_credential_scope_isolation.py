"""invariant for cross-scope Vault read isolation (uyumluluk Q7).

**invariant: Cross-Scope Vault Read Isolation (Q7 Kritik Güvenlik)**



This module pins the security invariant from the design's invariant:

 *For any* ``(scope, dept_id, session_id, service)`` çağrısı için:

 - ``scope ∈ {"org"}`` (veya deprecation alias ``"bot"``  ``"org"``)
 durumunda, Credential_Resolver Vault'a yapılan **hiçbir** ``get``
 çağrısının path'i ``secret/atlassian/_user_session/...`` veya
 ``secret/atlassian/_user_persisted/...`` prefix'iyle başlamaz.
 - ``scope == "user"`` durumunda, Credential_Resolver Vault'a yapılan
 **hiçbir** ``get`` çağrısının path'i
 ``secret/atlassian/{dept_id}/...`` (org-default) prefix'iyle
 başlamaz.
 - ``scope ∉ {"org", "user", "bot"}``  ``ValueError``.

 İhlal durumunda ``credential_scope_violation_attempt`` audit yazılır.

The test instruments the resolver's Vault client with a recording fake
that captures **every** ``read_secret`` and ``read_with_metadata`` call,
then walks the captured path list for the forbidden prefix per scope.
Hypothesis generates the ``(dept_id, session_id, service)`` triple so
the property holds across an arbitrary input space rather than a small
hand-curated example set.

Implementation notes
--------------------

* The resolver under test is the real
 ``decision.credential_resolver.CredentialResolver`` shipped with
 ``services/automation-service`` (no scope-isolation logic is
 duplicated in the test). invariant is a **black-box** invariant on
 that production module.
* For ``scope="org"`` we seed
 ``automation.department_bots.credential_ref`` with the canonical
 org-shape (``secret/atlassian/{dept_id}/{service}``). The fake Vault
 populates that path with valid credential material so the happy
 path returns. The violation case (leaked ``_user_session`` ref) is
 covered by a dedicated property below.
* For ``scope="user"`` we bypass the DB entirely (the resolver builds
 the path directly from ``session_id``). The fake Vault populates
 ``secret/atlassian/_user_session/{session_id}/{service}``.
* Hypothesis settings use ``deadline=None`` and a moderate
 ``max_examples`` so the suite runs in seconds on Windows file I/O.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrapping
# ---------------------------------------------------------------------------
#
# Mirrors the bootstrap used by ``test_dept_atomic_create.py`` and
# ``test_credential_inject.py``: we expose the automation-service ``src/``
# directory so the production resolver imports cleanly (without first
# pip-installing the service).

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (str(_AUTOMATION_SRC), str(_AUTOMATION_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from decision.credential_resolver import (  # noqa: E402
    CredentialResolver,
    CredentialScopeViolationError,
)
from http_shared.auth_inject import CredentialResolutionError  # noqa: E402


# ---------------------------------------------------------------------------
# Shared event loop
# ---------------------------------------------------------------------------
#
# invariant are synchronous from Hypothesis' perspective; we run
# each example's coroutine on a single shared loop so per-example
# overhead stays low. This mirrors the pattern used by
# ``test_credential_inject.py``.

_LOOP: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def _run_async(coro: Any) -> Any:
    return _get_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Recording Vault fake
# ---------------------------------------------------------------------------


@dataclass
class _RecordingVaultClient:
    """In-memory Vault stub that records every read attempt.

 The resolver calls either ``read_secret(path)`` or
 ``read_with_metadata(path)`` depending on what the underlying
 backend supports; we expose both methods and append to a single
 ``read_calls`` log so the property can assert against the union
 of read paths regardless of which method was chosen.

 Both methods return ``None`` for unknown paths (Vault 404 shape).
 """

    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    read_calls: list[str] = field(default_factory=list)

    async def read_secret(self, path: str) -> dict[str, str] | None:
        self.read_calls.append(path)
        return self.secrets.get(path)

    async def read_with_metadata(
        self, path: str
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        self.read_calls.append(path)
        data = self.secrets.get(path)
        if data is None:
            return None
        # Provide a stable created_time so the resolver's drift logic
        # has metadata to consume; the property under test never
        # touches that branch but the resolver still calls into the
        # metadata helper first.
        return data, {"created_time": "2025-01-01T00:00:00.000000Z", "version": 1}


# ---------------------------------------------------------------------------
# DB fake (asyncpg shape)
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Asyncpg-shaped connection backed by a dict-row list."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return list(self._rows)

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        dept_id = args[0] if len(args) > 0 else None
        service = args[1] if len(args) > 1 else None
        for row in self._rows:
            if (
                row.get("department_id") == dept_id
                and row.get("service") == service
            ):
                return row
        return None


class _FakePool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._conn = _FakeConnection(rows)

    def acquire(self) -> "_FakePoolAcquire":
        return _FakePoolAcquire(self._conn)


class _FakePoolAcquire:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Audit capture
# ---------------------------------------------------------------------------


class _CapturingAuditLogger:
    """Audit sink that captures every event for post-hoc assertions."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Path-prefix sentinels
# ---------------------------------------------------------------------------
#
# The resolver's invariant is: an ``org`` read NEVER touches the user
# prefix, and a ``user`` read NEVER touches the org prefix. We pin the
# two prefixes here so the property assertion stays in lock-step with
# the production code (any future rename in the resolver will surface
# as a test failure here).

_USER_PREFIXES: tuple[str, ...] = (
    "secret/atlassian/_user_session/",
    "secret/atlassian/_user_persisted/",
)


def _org_prefix(dept_id: str) -> str:
    """Return the canonical org-default Vault prefix for *dept_id*.

 The resolver does not own this shape directly - the prefix lives
 in ``automation.department_bots.credential_ref`` rows - but the
 invariant enforces that user-scope reads never see paths
 starting with ``secret/atlassian/{dept_id}/`` even if such a row
 exists in the DB seed.
 """

    return f"secret/atlassian/{dept_id}/"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# Identifiers stay within a tight ASCII range so the generated values
# slot cleanly into Vault paths without escape concerns. Length bounds
# are tuned to keep examples short while still exercising distinct
# values across runs.

_DEPT_ID = st.from_regex(r"[a-z][a-z0-9_-]{1,12}", fullmatch=True)
_SESSION_ID = st.from_regex(r"sess-[a-z0-9]{4,16}", fullmatch=True)
_SERVICE = st.sampled_from(["jira", "bitbucket", "confluence"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_org_vault(dept_id: str, service: str) -> _RecordingVaultClient:
    """Vault stub seeded with the canonical org-default secret only."""
    org_path = f"secret/atlassian/{dept_id}/{service}"
    return _RecordingVaultClient(
        secrets={
            org_path: {
                "url": f"https://{dept_id}.atlassian.net",
                "username": f"bot-{dept_id}",
                "personal_token": f"org-token-{dept_id}-{service}",
            }
        }
    )


def _build_user_vault(
    session_id: str, service: str
) -> _RecordingVaultClient:
    """Vault stub seeded with the canonical user-session secret only."""
    user_path = f"secret/atlassian/_user_session/{session_id}/{service}"
    return _RecordingVaultClient(
        secrets={
            user_path: {
                "url": "https://example.atlassian.net",
                "username": "user-alice",
                "personal_token": f"user-token-{session_id}-{service}",
            }
        }
    )


def _build_dept_pool(dept_id: str, service: str) -> _FakePool:
    """DB pool seeded with one canonical org-shaped credential_ref."""
    return _FakePool(
        rows=[
            {
                "id": 1,
                "department_id": dept_id,
                "service": service,
                "credential_ref": f"secret/atlassian/{dept_id}/{service}",
                "account_id": f"acct-{dept_id}",
                "username": f"bot-{dept_id}",
                "deployment": "cloud",
            }
        ]
    )


# ---------------------------------------------------------------------------
# invariant - org scope must not read user-prefix paths
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(dept_id=_DEPT_ID, session_id=_SESSION_ID, service=_SERVICE)
def test_org_scope_never_reads_user_prefix(
    dept_id: str, session_id: str, service: str
) -> None:
    """``scope="org"`` MUST NOT touch any ``_user_*`` Vault path.



 Setup: a DB row pointing at the canonical org-default path and a
 Vault stub seeded only with that path. Hypothesis varies
 ``(dept_id, session_id, service)`` so the property holds for an
 arbitrary input space.

 Invariant: every recorded ``read_secret`` / ``read_with_metadata``
 call's path must NOT start with ``secret/atlassian/_user_session/``
 or ``secret/atlassian/_user_persisted/``. ``session_id`` is
 irrelevant for org reads but is included in the strategy so the
 test exercises the same (dept, session, service) shape required
 by the task description.
 """
    vault = _build_org_vault(dept_id, service)
    db = _build_dept_pool(dept_id, service)
    resolver = CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]

    async def _check() -> None:
        cred = await resolver.get(dept_id, service, scope="org")
        # Sanity: the resolver returned the seeded org secret.
        assert cred.personal_token == f"org-token-{dept_id}-{service}"

    _run_async(_check())

    # Core invariant: no read call escaped to a user-prefix path.
    leaked = [
        path
        for path in vault.read_calls
        if any(path.startswith(prefix) for prefix in _USER_PREFIXES)
    ]
    assert leaked == [], (
        f"scope='org' leaked into user-prefix paths: {leaked!r} "
        f"(all read_calls={vault.read_calls!r}, session_id={session_id!r})"
    )


# ---------------------------------------------------------------------------
# invariant - user scope must not read org-prefix paths
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(dept_id=_DEPT_ID, session_id=_SESSION_ID, service=_SERVICE)
def test_user_scope_never_reads_org_prefix(
    dept_id: str, session_id: str, service: str
) -> None:
    """``scope="user"`` MUST NOT touch the org-default Vault prefix.



 The DB seed deliberately contains an org-shaped row for
 ``(dept_id, service)`` to mimic a misleading deployment where a
 bot registration exists alongside per-user sessions. The Vault
 stub holds **both** the org secret and the user-session secret
 so the resolver could in principle read either. The invariant:
 a ``scope="user"`` call MUST short-circuit on the user-session
 path and NEVER reach Postgres or the org-shaped Vault entry.
 """
    org_path = f"secret/atlassian/{dept_id}/{service}"
    user_path = f"secret/atlassian/_user_session/{session_id}/{service}"
    vault = _RecordingVaultClient(
        secrets={
            # Org-shaped secret seeded as a tempting-but-forbidden read.
            org_path: {
                "url": "https://org.atlassian.net",
                "username": "bot",
                "personal_token": "org-must-not-leak",
            },
            user_path: {
                "url": "https://user.atlassian.net",
                "username": "alice",
                "personal_token": f"user-token-{session_id}",
            },
        }
    )
    db = _build_dept_pool(dept_id, service)
    resolver = CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]

    async def _check() -> None:
        cred = await resolver.get(
            dept_id, service, scope="user", session_id=session_id
        )
        # Sanity: the resolver returned the seeded user secret.
        assert cred.personal_token == f"user-token-{session_id}"

    _run_async(_check())

    forbidden_prefix = _org_prefix(dept_id)
    leaked = [
        path
        for path in vault.read_calls
        if path.startswith(forbidden_prefix)
    ]
    assert leaked == [], (
        f"scope='user' leaked into org-prefix path {forbidden_prefix!r}: "
        f"{leaked!r} (all read_calls={vault.read_calls!r})"
    )


# ---------------------------------------------------------------------------
# invariant - deprecated ``"bot"`` alias inherits the org invariant
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(dept_id=_DEPT_ID, session_id=_SESSION_ID, service=_SERVICE)
def test_bot_scope_alias_inherits_org_isolation(
    dept_id: str, session_id: str, service: str
) -> None:
    """``scope="bot"`` (deprecation alias) honours the user-prefix ban.



 The resolver rewrites ``"bot"`` to ``"org"`` internally; the
 property simply re-runs invariant with ``scope="bot"`` to
 confirm the alias does not weaken the invariant. ``session_id``
 is generated for symmetry with the task spec but is unused for
 bot/org reads.
 """
    vault = _build_org_vault(dept_id, service)
    db = _build_dept_pool(dept_id, service)
    resolver = CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]

    async def _check() -> None:
        cred = await resolver.get(dept_id, service, scope="bot")
        assert cred.personal_token == f"org-token-{dept_id}-{service}"

    _run_async(_check())

    leaked = [
        path
        for path in vault.read_calls
        if any(path.startswith(prefix) for prefix in _USER_PREFIXES)
    ]
    assert leaked == [], (
        f"scope='bot' (alias for org) leaked into user-prefix paths: "
        f"{leaked!r} (all read_calls={vault.read_calls!r}, "
        f"session_id={session_id!r})"
    )


# ---------------------------------------------------------------------------
# invariant - unknown scope raises ValueError
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(
    dept_id=_DEPT_ID,
    session_id=_SESSION_ID,
    service=_SERVICE,
    invalid_scope=st.text(
        alphabet=st.characters(
            min_codepoint=0x21, max_codepoint=0x7E
        ),
        min_size=1,
        max_size=16,
    ).filter(lambda s: s not in {"org", "user", "bot"}),
)
def test_invalid_scope_raises_value_error(
    dept_id: str,
    session_id: str,
    service: str,
    invalid_scope: str,
) -> None:
    """Any scope outside ``{"org", "user", "bot"}`` raises ``ValueError``.



 The resolver must reject unknown scopes BEFORE issuing any Vault
 or DB call so an attacker cannot induce a probe-style read by
 feeding crafted scope literals.
 """
    vault = _build_org_vault(dept_id, service)
    db = _build_dept_pool(dept_id, service)
    resolver = CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]

    async def _check() -> None:
        with pytest.raises(ValueError):
            await resolver.get(
                dept_id, service, scope=invalid_scope, session_id=session_id
            )

    _run_async(_check())

    # No Vault read should have escaped - invalid scope is rejected
    # before any I/O.
    assert vault.read_calls == [], (
        f"invalid scope {invalid_scope!r} triggered Vault reads: "
        f"{vault.read_calls!r}"
    )


# ---------------------------------------------------------------------------
# invariant - leaked org credential_ref pointing at user prefix raises
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(dept_id=_DEPT_ID, session_id=_SESSION_ID, service=_SERVICE)
def test_org_scope_with_leaked_user_ref_raises_violation(
    dept_id: str, session_id: str, service: str
) -> None:
    """A leaked user-shape ``credential_ref`` triggers a security violation.



 Setup: a corrupted DB row whose ``credential_ref`` points at a
 ``_user_session/...`` path. The resolver MUST detect the cross-
 scope leak before issuing the Vault read and:

 1. raise:class:`CredentialScopeViolationError`,
 2. emit a ``credential_scope_violation_attempt`` audit event with
 ``actor_role="system"`` and ``result="denied"``,
 3. NOT issue any Vault read for the leaked path (the guard runs
 before ``_read_secret_with_metadata``).
 """
    leaked_ref = (
        f"secret/atlassian/_user_session/{session_id}/{service}"
    )
    db = _FakePool(
        rows=[
            {
                "id": 1,
                "department_id": dept_id,
                "service": service,
                "credential_ref": leaked_ref,
                "account_id": None,
                "username": None,
                "deployment": "cloud",
            }
        ]
    )
    vault = _RecordingVaultClient(
        secrets={
            # Even if the leaked secret existed, the resolver must
            # NOT reach it. We seed it to make the property strict.
            leaked_ref: {
                "url": "https://user.atlassian.net",
                "username": "alice",
                "personal_token": "must-never-leak",
            }
        }
    )
    audit = _CapturingAuditLogger()
    resolver = CredentialResolver(
        vault=vault,  # type: ignore[arg-type]
        db=db,
        audit_logger=audit,
    )

    async def _check() -> None:
        with pytest.raises(CredentialScopeViolationError) as exc_info:
            await resolver.get(dept_id, service, scope="org")
        assert exc_info.value.scope == "org"
        assert exc_info.value.dept_id == dept_id
        assert exc_info.value.service == service
        assert "_user_session" in exc_info.value.attempted_path

    _run_async(_check())

    # 1) No Vault read happened - the guard short-circuits before I/O.
    assert vault.read_calls == [], (
        f"scope='org' attempted to read leaked user path: "
        f"{vault.read_calls!r}"
    )

    # 2) Exactly one violation audit event was emitted.
    violations = [
        e
        for e in audit.events
        if getattr(e, "action", None) == "credential_scope_violation_attempt"
    ]
    assert len(violations) == 1, (
        f"expected exactly one credential_scope_violation_attempt audit, "
        f"got {audit.events!r}"
    )
    event = violations[0]
    assert event.actor_role == "system"
    assert event.result == "denied"
    assert event.payload is not None
    assert event.payload["scope"] == "org"
    assert event.payload["service"] == service
    assert "_user_session" in event.payload["attempted_path"]


# ---------------------------------------------------------------------------
# invariant - user scope rejects empty session_id without I/O
# ---------------------------------------------------------------------------


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
@given(dept_id=_DEPT_ID, service=_SERVICE)
def test_user_scope_without_session_id_raises_value_error(
    dept_id: str, service: str
) -> None:
    """``scope="user"`` without a session_id raises before any I/O.



 A missing or empty ``session_id`` would otherwise leave the user
 path under-constrained (``secret/atlassian/_user_session//{service}``)
 and could collide with a shared prefix. The resolver rejects the
 call up front so no Vault or DB read ever happens.
 """
    vault = _build_org_vault(dept_id, service)
    db = _build_dept_pool(dept_id, service)
    resolver = CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]

    async def _check() -> None:
        with pytest.raises(ValueError):
            await resolver.get(dept_id, service, scope="user")
        with pytest.raises(ValueError):
            await resolver.get(
                dept_id, service, scope="user", session_id=""
            )

    _run_async(_check())

    assert vault.read_calls == [], (
        f"scope='user' without session_id triggered Vault reads: "
        f"{vault.read_calls!r}"
    )
