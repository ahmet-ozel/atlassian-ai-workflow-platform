"""Unit tests for ``db_shared.session``.

Covers task 4.2 of the ``platform-mimari-foundation`` spec: the
``with_dept_session`` async context manager (issues ``SET LOCAL
app.current_dept_id`` / ``app.current_role`` at the start of every
transaction) and the ``bind_actor`` helper (auto-scopes ``dept_admin``
actors to their owned department).

Validates: Requirements 7.4, 9.5.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

import pytest

from db_shared.session import (
    ALLOWED_ROLES,
    AuthContext,
    TenantAwareSession,
    bind_actor,
    with_actor_session,
    with_dept_session,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeConnection:
    """In-memory ``AsyncConnection`` recording every executed statement.

    Enough surface to exercise :func:`with_dept_session` without
    pulling Postgres / asyncpg into the unit-test path. The recorded
    statements (``query``, positional ``args``) are inspected by tests
    to assert the SET LOCAL / BEGIN / COMMIT / ROLLBACK ordering.
    """

    def __init__(
        self,
        *,
        fail_on: str | None = None,
        rollback_fails: bool = False,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fail_on = fail_on
        self._rollback_fails = rollback_fails

    async def execute(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        if self._rollback_fails and query == "ROLLBACK":
            raise RuntimeError("rollback failed")
        if self._fail_on is not None and self._fail_on in query:
            raise RuntimeError(f"injected failure on: {query}")
        return None


@dataclass
class _Actor:
    """Fake :class:`AuthContext` for :func:`bind_actor` tests."""

    actor_id: str = "user-1"
    actor_role: str = "viewer"
    dept_ids: Iterable[str] = field(default_factory=tuple)


def _run(coro):
    """Run a coroutine in a fresh event loop (sync test entry point)."""

    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# ALLOWED_ROLES — schema parity with audit_events.actor_role CHECK
# ---------------------------------------------------------------------------


def test_allowed_roles_matches_check_constraint() -> None:
    """``ALLOWED_ROLES`` mirrors the four RBAC roles + 'system'.

    Requirement 7.7 ties the DB CHECK constraint to the same set;
    drift here causes silent INSERT failures in production.
    """

    assert ALLOWED_ROLES == frozenset(
        {"viewer", "lead", "admin", "dept_admin", "system"}
    )


# ---------------------------------------------------------------------------
# with_dept_session — happy paths
# ---------------------------------------------------------------------------


def test_with_dept_session_emits_set_local_and_commits() -> None:
    """Successful exit issues BEGIN, both ``set_config`` calls, COMMIT."""

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session(
            "dept_admin", "engineering", connection=conn
        ) as wrapped:
            assert wrapped is conn

    _run(run())

    queries = [q for q, _ in conn.calls]
    assert queries == [
        "BEGIN",
        "SELECT set_config('app.current_dept_id', $1, true)",
        "SELECT set_config('app.current_role', $1, true)",
        "COMMIT",
    ]
    # Bound values are forwarded as positional parameters, never
    # interpolated into the SQL string.
    assert conn.calls[1][1] == ("engineering",)
    assert conn.calls[2][1] == ("dept_admin",)


def test_with_dept_session_admin_without_dept_id_uses_empty_string() -> None:
    """``admin`` may open a global session; GUC bound to empty string.

    Postgres ``set_config(name, '', true)`` clears the GUC for the
    transaction, which makes the policy's
    ``current_role = 'admin'`` branch the deciding factor (the
    contract documented in design.md / 10_automation.sql).
    """

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session("admin", None, connection=conn):
            pass

    _run(run())

    # First set_config call (dept_id) is bound to '' so the GUC is
    # cleared for this transaction.
    assert conn.calls[1][1] == ("",)
    assert conn.calls[2][1] == ("admin",)


def test_with_dept_session_system_without_dept_id_allowed() -> None:
    """``system`` actors may open a global session like ``admin``."""

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session("system", None, connection=conn):
            pass

    _run(run())
    assert conn.calls[2][1] == ("system",)


# ---------------------------------------------------------------------------
# with_dept_session — input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_role",
    ["", "ADMIN", "root", "guest", "dept-admin", "Admin"],
)
def test_with_dept_session_rejects_unknown_role(bad_role: str) -> None:
    """Roles outside :data:`ALLOWED_ROLES` raise before any SQL runs."""

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session(bad_role, "engineering", connection=conn):
            pass  # pragma: no cover

    with pytest.raises(ValueError):
        _run(run())

    # No SQL was emitted because validation fires before BEGIN.
    assert conn.calls == []


@pytest.mark.parametrize(
    "bad_dept_id",
    [
        "Engineering",  # uppercase
        "1eng",  # starts with digit
        "x",  # too short (1 char, regex requires >=2)
        "engineering!",  # invalid character
        "eng_team",  # underscore not allowed by schema regex
        "a" * 32,  # too long (>31)
    ],
)
def test_with_dept_session_rejects_invalid_dept_id(bad_dept_id: str) -> None:
    """``dept_id`` must match the ``Department.id`` schema regex."""

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session(
            "dept_admin", bad_dept_id, connection=conn
        ):
            pass  # pragma: no cover

    with pytest.raises(ValueError):
        _run(run())
    assert conn.calls == []


@pytest.mark.parametrize("role", ["viewer", "lead", "dept_admin"])
def test_with_dept_session_requires_dept_id_for_non_global_roles(
    role: str,
) -> None:
    """Non-admin/system roles MUST bind to a department.

    Otherwise the RLS policy's ``id = current_setting('...')`` branch
    silently filters every row, masking the misconfiguration.
    """

    conn = _FakeConnection()

    async def run() -> None:
        async with with_dept_session(role, None, connection=conn):
            pass  # pragma: no cover

    with pytest.raises(PermissionError):
        _run(run())
    assert conn.calls == []


# ---------------------------------------------------------------------------
# with_dept_session — rollback on body exception
# ---------------------------------------------------------------------------


def test_with_dept_session_rolls_back_on_body_exception() -> None:
    """An exception inside the ``async with`` block triggers ROLLBACK."""

    conn = _FakeConnection()

    class _Boom(RuntimeError):
        pass

    async def run() -> None:
        async with with_dept_session(
            "dept_admin", "finance", connection=conn
        ):
            raise _Boom("body failed")

    with pytest.raises(_Boom):
        _run(run())

    queries = [q for q, _ in conn.calls]
    assert queries[0] == "BEGIN"
    assert "ROLLBACK" in queries
    assert "COMMIT" not in queries


def test_with_dept_session_rollback_failure_does_not_mask_original() -> None:
    """If ROLLBACK itself errors, the original exception still propagates."""

    conn = _FakeConnection(rollback_fails=True)

    class _Boom(RuntimeError):
        pass

    async def run() -> None:
        async with with_dept_session(
            "dept_admin", "finance", connection=conn
        ):
            raise _Boom("body failed")

    with pytest.raises(_Boom):
        _run(run())


def test_with_dept_session_set_config_failure_triggers_rollback() -> None:
    """A failure during ``set_config`` rolls back the BEGIN."""

    conn = _FakeConnection(fail_on="app.current_role")

    async def run() -> None:
        async with with_dept_session(
            "dept_admin", "ops", connection=conn
        ):
            pass  # pragma: no cover

    with pytest.raises(RuntimeError, match="injected failure"):
        _run(run())

    queries = [q for q, _ in conn.calls]
    assert queries[0] == "BEGIN"
    assert queries[-1] == "ROLLBACK"


# ---------------------------------------------------------------------------
# bind_actor — role-specific scoping rules
# ---------------------------------------------------------------------------


def test_bind_actor_dept_admin_auto_scopes_to_owned_dept() -> None:
    """``dept_admin`` is pinned to the single dept they own."""

    actor = _Actor(actor_role="dept_admin", dept_ids=("engineering",))
    role, dept_id = bind_actor(actor)
    assert role == "dept_admin"
    assert dept_id == "engineering"


def test_bind_actor_dept_admin_explicit_match_allowed() -> None:
    """Passing the owned dept_id explicitly is harmless."""

    actor = _Actor(actor_role="dept_admin", dept_ids=("engineering",))
    role, dept_id = bind_actor(actor, dept_id="engineering")
    assert (role, dept_id) == ("dept_admin", "engineering")


def test_bind_actor_dept_admin_cross_dept_raises() -> None:
    """``dept_admin`` cannot widen scope to a foreign department.

    This is the central guard behind Requirement 7.3 (a ``dept_admin``
    must not see other departments' rows).
    """

    actor = _Actor(actor_role="dept_admin", dept_ids=("engineering",))
    with pytest.raises(PermissionError):
        bind_actor(actor, dept_id="finance")


def test_bind_actor_dept_admin_with_zero_or_many_depts_raises() -> None:
    """``dept_admin`` MUST own exactly one department."""

    with pytest.raises(ValueError):
        bind_actor(_Actor(actor_role="dept_admin", dept_ids=()))
    with pytest.raises(ValueError):
        bind_actor(
            _Actor(actor_role="dept_admin", dept_ids=("a", "b"))
        )


def test_bind_actor_admin_without_dept_id_returns_none() -> None:
    """Global ``admin`` actor opens a session without a tenant binding."""

    actor = _Actor(actor_role="admin", dept_ids=())
    role, dept_id = bind_actor(actor)
    assert role == "admin"
    assert dept_id is None


def test_bind_actor_admin_with_explicit_dept_id_validates_format() -> None:
    """Admin acting *for* a department: dept_id is preserved."""

    actor = _Actor(actor_role="admin", dept_ids=())
    role, dept_id = bind_actor(actor, dept_id="finance")
    assert (role, dept_id) == ("admin", "finance")


def test_bind_actor_admin_invalid_dept_id_raises() -> None:
    """Even for admin, dept_id must match the schema regex."""

    actor = _Actor(actor_role="admin", dept_ids=())
    with pytest.raises(ValueError):
        bind_actor(actor, dept_id="BAD!")


@pytest.mark.parametrize("role", ["viewer", "lead"])
def test_bind_actor_viewer_lead_require_explicit_dept_id(role: str) -> None:
    """``viewer`` / ``lead`` cannot open a session without a dept_id."""

    actor = _Actor(actor_role=role, dept_ids=("engineering",))
    with pytest.raises(PermissionError):
        bind_actor(actor)


@pytest.mark.parametrize("role", ["viewer", "lead"])
def test_bind_actor_viewer_lead_must_own_dept_id(role: str) -> None:
    """Cross-dept access is rejected for ``viewer`` / ``lead`` too."""

    actor = _Actor(actor_role=role, dept_ids=("engineering",))
    with pytest.raises(PermissionError):
        bind_actor(actor, dept_id="finance")


@pytest.mark.parametrize("role", ["viewer", "lead"])
def test_bind_actor_viewer_lead_valid_dept_id(role: str) -> None:
    """Owned dept_id is accepted for ``viewer`` / ``lead``."""

    actor = _Actor(actor_role=role, dept_ids=("engineering", "finance"))
    resolved_role, dept_id = bind_actor(actor, dept_id="finance")
    assert (resolved_role, dept_id) == (role, "finance")


def test_bind_actor_unknown_role_raises() -> None:
    """An unknown role is rejected before any RLS reasoning happens."""

    with pytest.raises(ValueError):
        bind_actor(_Actor(actor_role="root", dept_ids=()))


# ---------------------------------------------------------------------------
# with_actor_session — convenience wrapper integration
# ---------------------------------------------------------------------------


def test_with_actor_session_pins_dept_admin_dept() -> None:
    """``with_actor_session`` runs bind_actor + with_dept_session."""

    actor = _Actor(actor_role="dept_admin", dept_ids=("engineering",))
    conn = _FakeConnection()

    async def run() -> None:
        async with with_actor_session(actor, connection=conn):
            pass

    _run(run())
    # dept_id GUC bound to 'engineering', role to 'dept_admin'.
    assert conn.calls[1][1] == ("engineering",)
    assert conn.calls[2][1] == ("dept_admin",)


def test_with_actor_session_admin_global() -> None:
    """``admin`` actor opens a global session (dept_id GUC empty)."""

    actor = _Actor(actor_role="admin", dept_ids=())
    conn = _FakeConnection()

    async def run() -> None:
        async with with_actor_session(actor, connection=conn):
            pass

    _run(run())
    assert conn.calls[1][1] == ("",)
    assert conn.calls[2][1] == ("admin",)


def test_with_actor_session_dept_admin_cross_dept_raises() -> None:
    """Cross-dept attempt rejected before BEGIN is issued."""

    actor = _Actor(actor_role="dept_admin", dept_ids=("engineering",))
    conn = _FakeConnection()

    async def run() -> None:
        async with with_actor_session(
            actor, connection=conn, dept_id="finance"
        ):
            pass  # pragma: no cover

    with pytest.raises(PermissionError):
        _run(run())
    assert conn.calls == []


# ---------------------------------------------------------------------------
# AuthContext protocol compatibility (auth-shared task 8.1)
# ---------------------------------------------------------------------------


def test_auth_shared_authcontext_satisfies_protocol() -> None:
    """The real :class:`auth_shared.AuthContext` satisfies the local Protocol.

    Exercised via :func:`bind_actor` so a structural mismatch (eg. a
    rename of ``dept_ids``) fails this test instead of leaking into
    the admin-dashboard-api integration.
    """

    pytest.importorskip("auth_shared")
    from auth_shared.oidc import AuthContext as RealAuthContext

    real = RealAuthContext(
        actor_id="oidc-sub",
        actor_role="dept_admin",
        dept_ids=frozenset({"engineering"}),
    )
    role, dept_id = bind_actor(real)
    assert (role, dept_id) == ("dept_admin", "engineering")


# ---------------------------------------------------------------------------
# TenantAwareSession backward-compatibility shim
# ---------------------------------------------------------------------------


def test_tenant_aware_session_backwards_compatible() -> None:
    """Legacy ``TenantAwareSession`` placeholder still imports + runs."""

    s = TenantAwareSession("engineering", "postgresql://localhost/ai")
    assert s.tenant_id == "engineering"
    assert s.dsn == "postgresql://localhost/ai"
    # ``set_rls`` is an explicit no-op kept for the scaffold tests.
    assert s.set_rls() is None


# ---------------------------------------------------------------------------
# Sanity: AuthContext Protocol is structural (runtime_checkable)
# ---------------------------------------------------------------------------


def test_authcontext_runtime_checkable_with_simple_dataclass() -> None:
    """A plain dataclass exposing the three fields satisfies the Protocol."""

    actor = _Actor(actor_role="viewer", dept_ids=("a-dept",))
    assert isinstance(actor, AuthContext)
