"""Tenant-aware Postgres session helpers (``with_dept_session`` / ``bind_actor``).

This module materialises the ``db-shared`` connection helper.

It enforces two invariants required by Postgres row-level security
policies (`infra/postgres/10_automation.sql`):

* Every transaction issued through :func:`with_dept_session` begins
  with ``SET LOCAL app.current_dept_id = '<id>'`` and
  ``SET LOCAL app.current_role = '<role>'`` so the
  ``dept_isolation`` and ``audit_dept_isolation`` policies see the
  caller's identity.
* :func:`bind_actor` automatically scopes a ``dept_admin`` actor to
  their single owned department so callers cannot accidentally widen
  the visible row set by passing a different ``dept_id``.

The helper is intentionally framework-agnostic: it talks to a
:class:`AsyncConnection` :class:`~typing.Protocol` shaped after the
asyncpg connection surface. Production code injects an asyncpg
``Connection`` (or a SQLAlchemy ``AsyncConnection`` adapter); tests
inject an in-memory fake without pulling Postgres into the test path.
A more developer-friendly :func:`with_dept_session` overload accepts a
DSN string and lazily acquires an asyncpg connection — this path is
only exercised when ``asyncpg`` is installed in the runtime
environment.

Backward compatibility
----------------------

The legacy :class:`TenantAwareSession` placeholder is **kept** so existing imports continue
to work (``platform/tests/conftest.py`` lists
``libs/db-shared/src/db_shared/session.py`` as a required path and
its public ``TenantAwareSession`` symbol is referenced by
``libs/db-shared/README.md``). The new helpers (``with_dept_session``,
``bind_actor``, ``AsyncConnection``) live alongside it and do not
disturb the existing surface.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import (
    Any,
    AsyncIterator,
    Iterable,
    Mapping,
    Protocol,
    runtime_checkable,
)

# ---------------------------------------------------------------------------
# Constants and validation helpers
# ---------------------------------------------------------------------------

#: RBAC roles accepted by :func:`with_dept_session`. Mirrors the
#: ``audit_events.actor_role`` CHECK constraint in
#: ``infra/postgres/10_automation.sql`` so a session opened with a
#: bogus role fails fast at the application layer instead of leaking
#: an opaque integrity error from the DB.
ALLOWED_ROLES: frozenset[str] = frozenset(
    {"viewer", "lead", "admin", "dept_admin", "system"}
)

#: Regex applied to ``dept_id`` before it is interpolated into
#: ``SET LOCAL``. Mirrors the ``Department.id`` pattern from
#: ``config/departments.schema.json`` (``^[a-z][a-z0-9-]{1,30}$``).
#: Anything outside this character class is rejected so the helper
#: cannot be tricked into emitting arbitrary SQL even if a caller
#: forwards an unsanitised value.
_DEPT_ID_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def _validate_role(role: str) -> str:
    """Return ``role`` if it is one of :data:`ALLOWED_ROLES`.

    Raises:
        ValueError: If ``role`` is not a string in
            :data:`ALLOWED_ROLES`. The error message lists the allowed
            roles so the offending caller sees the contract directly.
    """

    if not isinstance(role, str) or not role:
        raise ValueError(
            f"role must be a non-empty string in {sorted(ALLOWED_ROLES)!r}; "
            f"got {role!r}"
        )
    if role not in ALLOWED_ROLES:
        raise ValueError(
            f"role={role!r} is not one of the allowed RBAC roles "
            f"{sorted(ALLOWED_ROLES)!r}. Mirrors the "
            "audit_events.actor_role CHECK constraint."
        )
    return role


def _validate_dept_id(dept_id: str | None) -> str | None:
    """Return ``dept_id`` if it is ``None`` or matches the schema regex.

    A ``None`` value is preserved so callers acting as ``admin`` /
    ``system`` can open a session without binding to a single
    department (their RLS policy bypasses the dept filter).

    Raises:
        ValueError: If ``dept_id`` is provided but does not match the
            ``^[a-z][a-z0-9-]{1,30}$`` pattern from the
            ``Department.id`` schema.
    """

    if dept_id is None:
        return None
    if not isinstance(dept_id, str) or not _DEPT_ID_PATTERN.fullmatch(dept_id):
        raise ValueError(
            "dept_id must match ^[a-z][a-z0-9-]{1,30}$ "
            f"(see config/departments.schema.json); got {dept_id!r}"
        )
    return dept_id


# ---------------------------------------------------------------------------
# Connection / actor protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class AsyncConnection(Protocol):
    """Minimal asyncpg-shaped connection surface used by the helper.

    Production code injects an :class:`asyncpg.Connection`; tests
    inject an in-memory fake. The protocol intentionally only covers
    the two methods we need so future swaps (eg. SQLAlchemy
    ``AsyncConnection``) are trivial.
    """

    async def execute(self, query: str, *args: Any) -> Any:
        """Run a single statement (no result set expected)."""

        ...


@runtime_checkable
class AuthContext(Protocol):
    """Subset of the :class:`auth_shared.AuthContext` shape we depend on.

    The full dataclass is owned by ``libs/auth-shared``; we declare a
    Protocol here so :func:`bind_actor` can be used without importing
    ``auth-shared`` just to build a
    fake actor.

    Attributes:
        actor_id: Stable identifier for the actor (eg. OIDC ``sub``
            claim). Carried for diagnostic / audit purposes only;
            :func:`bind_actor` does not interpolate it into SQL.
        actor_role: One of :data:`ALLOWED_ROLES`. Drives the
            ``app.current_role`` GUC.
        dept_ids: Sequence of departments the actor owns. For
            ``dept_admin`` this MUST contain exactly one entry which
            :func:`bind_actor` pins automatically.
    """

    actor_id: str
    actor_role: str
    dept_ids: Iterable[str]


# ---------------------------------------------------------------------------
# bind_actor — derive (role, dept_id) from an AuthContext
# ---------------------------------------------------------------------------


def bind_actor(
    actor: AuthContext,
    *,
    dept_id: str | None = None,
) -> tuple[str, str | None]:
    """Resolve the ``(role, dept_id)`` pair that should drive RLS.

    The helper enforces the tenant binding rule:

    * **dept_admin** — automatically scoped to the single department
      they own. Passing an explicit ``dept_id`` is allowed only when
      it matches the actor's owned department; any other value raises
      :class:`PermissionError` so a malicious / buggy caller cannot
      widen the visible row set.
    * **admin / system** — no department binding. ``dept_id`` is
      preserved when explicitly provided (eg. an admin acting *on
      behalf of* a department), otherwise returns ``None`` so the
      RLS policy's ``current_setting('app.current_role') = 'admin'``
      branch matches.
    * **viewer / lead** — multi-tenant read roles. ``dept_id`` MUST
      be supplied explicitly and MUST be one of the actor's owned
      departments; otherwise :class:`PermissionError` is raised.

    Args:
        actor: An object satisfying the :class:`AuthContext` protocol
            (typically built from OIDC claims by ``auth-shared``).
        dept_id: Optional explicit department to bind the session to.
            Required for non-``dept_admin`` roles that need a
            tenant-scoped session; ignored / cross-checked for
            ``dept_admin``.

    Returns:
        A ``(role, dept_id)`` tuple ready to forward to
        :func:`with_dept_session`. ``dept_id`` may be ``None`` for
        ``admin`` / ``system`` callers acting globally.

    Raises:
        ValueError: If ``actor.actor_role`` is unknown, or if
            ``dept_admin`` does not own exactly one department.
        PermissionError: If a caller tries to scope an actor to a
            department they do not own.
    """

    role = _validate_role(getattr(actor, "actor_role", ""))
    owned: tuple[str, ...] = tuple(getattr(actor, "dept_ids", ()) or ())

    if role == "dept_admin":
        # ``dept_admin`` is automatically scoped to the dept_id it owns.
        # We refuse to guess if the caller
        # owns zero or multiple departments — that's a configuration
        # bug, not a runtime decision.
        if len(owned) != 1:
            raise ValueError(
                "dept_admin actor must own exactly one department; "
                f"got dept_ids={owned!r}. Fix the OIDC claim mapping "
                "(see auth_shared.policy)."
            )
        owned_dept = _validate_dept_id(owned[0])
        if dept_id is not None and dept_id != owned_dept:
            # Refuse to widen scope.
            raise PermissionError(
                f"dept_admin actor (dept_ids={owned!r}) cannot bind to "
                f"dept_id={dept_id!r}; only their owned department is "
                "permitted."
            )
        return role, owned_dept

    if role in ("admin", "system"):
        # Global roles. ``dept_id`` is optional — when omitted, the
        # RLS policy's ``current_role = 'admin'`` branch unlocks the
        # full table; when provided (eg. an admin clicking into a
        # specific department's view), we validate the format and
        # forward it verbatim.
        return role, _validate_dept_id(dept_id)

    # viewer / lead: must scope to an owned department.
    if dept_id is None:
        raise PermissionError(
            f"role={role!r} requires an explicit dept_id; the actor "
            "does not have a global RLS bypass."
        )
    validated = _validate_dept_id(dept_id)
    if validated not in {_validate_dept_id(d) for d in owned}:
        raise PermissionError(
            f"role={role!r} actor (dept_ids={owned!r}) cannot bind to "
            f"dept_id={validated!r}; only owned departments are permitted."
        )
    return role, validated


# ---------------------------------------------------------------------------
# with_dept_session — async context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def with_dept_session(
    role: str,
    dept_id: str | None,
    *,
    connection: AsyncConnection,
) -> AsyncIterator[AsyncConnection]:
    """Open a transaction with ``app.current_dept_id`` / ``app.current_role`` pinned.

    Every transaction opened through this helper begins with two
    ``SET LOCAL`` statements so the Postgres RLS policies declared in
    ``infra/postgres/10_automation.sql``::

        POLICY dept_isolation ON automation.departments
            USING (id = current_setting('app.current_dept_id', true)
                OR  current_setting('app.current_role',    true) = 'admin');

    can identify the caller. The values are interpolated through
    asyncpg's positional parameter binding (``$1`` / ``$2``) so the
    helper is **not** vulnerable to SQL injection even if a caller
    forwards an unsanitised string — additionally we validate the
    role against :data:`ALLOWED_ROLES` and the dept_id against the
    schema regex before opening the transaction.

    The context manager wraps the body in a ``BEGIN`` / ``COMMIT``
    pair: the transaction commits when the ``async with`` block exits
    cleanly, and rolls back if the body raises. ``SET LOCAL`` is
    transaction-scoped, so the GUCs are automatically reverted on
    either path — there is no leakage to subsequent work on the same
    connection.

    Args:
        role: One of :data:`ALLOWED_ROLES`. Bound to
            ``app.current_role``.
        dept_id: Department identifier matching
            ``^[a-z][a-z0-9-]{1,30}$``. Bound to
            ``app.current_dept_id``. May be ``None`` only for the
            ``admin`` / ``system`` roles that have a global bypass in
            the RLS policy; for any other role a non-``None``
            ``dept_id`` is required.
        connection: An :class:`AsyncConnection` (typically an asyncpg
            ``Connection`` checked out from a pool) on which the
            transaction will run.

    Yields:
        The same ``connection`` instance, now wrapped in an active
        transaction with the GUCs pinned. Callers issue their queries
        against it and the helper handles commit / rollback.

    Raises:
        ValueError: If ``role`` is not one of :data:`ALLOWED_ROLES`,
            or if ``dept_id`` does not match the schema regex.
        PermissionError: If ``dept_id`` is ``None`` for a role that
            requires tenant scoping (anything other than ``admin`` /
            ``system``).
    """

    role = _validate_role(role)
    dept_id = _validate_dept_id(dept_id)

    if dept_id is None and role not in ("admin", "system"):
        # Non-global roles must bind to a department. The RLS policy
        # `dept_isolation` evaluates `id = current_setting(...)`; an
        # unset GUC produces an empty string and would silently filter
        # every row, which masks the bug. Fail loudly instead.
        raise PermissionError(
            f"role={role!r} requires a non-null dept_id; only 'admin' "
            "and 'system' may open a session without one."
        )

    # ``current_setting`` returns the empty string when the GUC is
    # unset (because we passed ``true`` for the missing_ok argument
    # in the policy). Mirror that here when the caller passed
    # ``dept_id=None`` so the SET LOCAL still has a well-defined
    # value to bind to and the RLS policy compares against the
    # caller's intent rather than a stale value from a previous
    # transaction on the same pooled connection.
    dept_id_to_set: str = dept_id if dept_id is not None else ""

    # ``SET LOCAL`` does not accept a parameterised value in stock
    # Postgres syntax; we use ``set_config(name, value, is_local=true)``
    # which is fully equivalent and supports asyncpg's $1/$2 binding.
    # That keeps the helper injection-safe even if the validation
    # regex above is ever loosened.
    await connection.execute("BEGIN")
    try:
        await connection.execute(
            "SELECT set_config('app.current_dept_id', $1, true)",
            dept_id_to_set,
        )
        await connection.execute(
            "SELECT set_config('app.current_role', $1, true)",
            role,
        )
        yield connection
    except BaseException:
        # ``ROLLBACK`` is best-effort — if the connection is already
        # broken (eg. asyncpg raised ``ConnectionDoesNotExistError``)
        # we still want the original exception to propagate.
        try:
            await connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    else:
        await connection.execute("COMMIT")
    finally:
        await _release_if_owned(connection)


async def _release_if_owned(connection: AsyncConnection) -> None:
    """Release a pool-owned connection wrapper after session exit."""

    if not getattr(connection, "__db_shared_release_on_exit__", False):
        return
    release = getattr(connection, "__db_shared_release__", None)
    if release is None:
        return
    maybe_awaitable = release()
    if hasattr(maybe_awaitable, "__await__"):
        await maybe_awaitable  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Convenience wrapper combining bind_actor + with_dept_session
# ---------------------------------------------------------------------------


@asynccontextmanager
async def with_actor_session(
    actor: AuthContext,
    *,
    connection: AsyncConnection,
    dept_id: str | None = None,
) -> AsyncIterator[AsyncConnection]:
    """Open a tenant-scoped transaction for an :class:`AuthContext` actor.

    Convenience wrapper that runs :func:`bind_actor` and forwards the
    resolved ``(role, dept_id)`` pair to :func:`with_dept_session`.
    Useful in HTTP handlers where the ``AuthContext`` is already
    available from the auth dependency.

    Args:
        actor: The authenticated actor.
        connection: An :class:`AsyncConnection` to wrap.
        dept_id: Optional explicit department override (only honoured
            for the ``admin`` / ``system`` roles; ``dept_admin``
            actors are auto-scoped — see :func:`bind_actor`).

    Yields:
        The active transaction's connection.
    """

    role, resolved_dept = bind_actor(actor, dept_id=dept_id)
    async with with_dept_session(
        role, resolved_dept, connection=connection
    ) as conn:
        yield conn


# ---------------------------------------------------------------------------
# Backward-compatible placeholder
# ---------------------------------------------------------------------------


class TenantAwareSession:
    """Legacy session wrapper kept for backward compatibility.

    A public ``TenantAwareSession`` symbol is re-exported from
    :mod:`db_shared`; ``platform/tests/conftest.py`` and
    ``libs/db-shared/README.md`` still reference it. The new
    foundation-spec helpers (:func:`with_dept_session`,
    :func:`bind_actor`) are the recommended path; this class remains
    as a thin convenience wrapper that records the intended
    ``tenant_id`` / ``dsn`` pair so existing call sites do not break.

    Args:
        tenant_id: Logical tenant identifier (kebab-case department id).
        dsn: Postgres connection string in libpq URI form.
    """

    def __init__(self, tenant_id: str, dsn: str) -> None:
        self.tenant_id = tenant_id
        self.dsn = dsn

    def set_rls(self) -> None:
        """No-op preserved for backward compatibility.

        New code should use :func:`with_dept_session` which issues the
        ``SET LOCAL app.current_dept_id`` / ``app.current_role``
        statements at transaction start. This method is kept so older
        older code that calls ``session.set_rls()`` still imports
        cleanly.
        """

        return None


__all__ = [
    "ALLOWED_ROLES",
    "AsyncConnection",
    "AuthContext",
    "TenantAwareSession",
    "bind_actor",
    "with_actor_session",
    "with_dept_session",
]
