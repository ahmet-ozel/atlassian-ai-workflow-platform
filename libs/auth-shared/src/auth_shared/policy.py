"""RBAC policy helpers for the admin control plane.

This module implements the four-role RBAC matrix and the
``requires(role, dept_id=None)`` guard helper, together with the
cross-cutting policy decisions baked into those rules:

* **Four roles** — ``viewer``, ``lead``, ``admin``, ``dept_admin`` and
  no others.
* **Hierarchy among the global roles** — ``viewer < lead < admin``;
  any guarded operation that admits ``viewer`` must also admit
  ``lead`` and ``admin``.
* **Dept-scoping** — ``dept_admin`` is an orthogonal dept-scoped role
  that can perform admin-like actions **only within its own
  department**; cross-dept access is denied.
* **Global-only operations** — adding a new department, changing the
  global prompt, configuring SSH runners are admin-only; ``dept_admin``
  is rejected with HTTP 403 + ``rbac_denied`` audit.
* **Self-service rotation** — ``dept_admin`` may rotate its **own**
  department's credentials; the call site passes the
  target ``dept_id`` to :func:`requires` and the guard does the
  matching.

The guard supports two surfaces:

* **Decorator** — ``@requires("admin")`` wraps a sync or async callable
  whose first ``AuthContext`` argument (positional or keyword
  ``actor``) is checked. Failures raise :class:`PermissionDenied`,
  which the FastAPI middleware in ``admin-dashboard-api`` translates
  into HTTP 403 plus an ``rbac_denied`` audit row.
* **Imperative guard** — ``check(actor, role, dept_id)`` and the
  thin :func:`requires_role` wrapper let call sites that need
  contextual data (eg. the path-derived ``dept_id`` in a FastAPI
  router) make the same decision without a decorator.

The module is intentionally pure: no FastAPI / Starlette / framework
imports, no audit-writing side effects. The caller logs the
``rbac_denied`` event after catching :class:`PermissionDenied` so
:func:`requires` stays usable from non-HTTP contexts (the future
Streamlit per-user flow reuses the same primitives).
"""

from __future__ import annotations

import functools
import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Literal,
    TypeVar,
)

from .oidc import AuthContext

# ---------------------------------------------------------------------------
# Role enumeration
# ---------------------------------------------------------------------------

#: The four RBAC roles.
#:
#: ``Role`` deliberately mirrors :data:`auth_shared.oidc.AuthRole` —
#: callers may use either alias. The duplicated definition is the
#: explicit spelling: ``Role = Literal["viewer", "lead", "admin",
#: "dept_admin"]``. The ``test_role_aliases`` unit test asserts the two aliases
#: stay in lock-step.
Role = Literal["viewer", "lead", "admin", "dept_admin"]

#: Runtime mirror of :data:`Role` for set-membership checks. Kept in
#: lock-step with :data:`Role` by ``test_role_runtime_mirror``.
ROLES: Final[frozenset[str]] = frozenset({"viewer", "lead", "admin", "dept_admin"})

# Linear precedence among the *global* roles. ``dept_admin`` is
# intentionally **absent** from this mapping: it is not a point on the
# viewer→lead→admin ladder but an orthogonal dept-scoped role that
# can perform admin-like operations within its own dept. The
# :func:`_rank` helper handles ``dept_admin`` separately.
_GLOBAL_ROLE_RANK: Final[dict[str, int]] = {
    "viewer": 1,
    "lead": 2,
    "admin": 3,
}

# Roles that may satisfy a guard requiring ``"admin"`` privileges. Only
# the ``admin`` role passes
# global admin checks. ``dept_admin`` is **not** in this set. When a
# call site instead asks for ``"dept_admin"`` privileges with a
# matching ``dept_id``, that case is handled directly in :func:`check`
# rather than via this map.
_ROLES_THAT_SATISFY: Final[dict[str, frozenset[str]]] = {
    "viewer": frozenset({"viewer", "lead", "admin", "dept_admin"}),
    "lead": frozenset({"lead", "admin", "dept_admin"}),
    "admin": frozenset({"admin"}),
    "dept_admin": frozenset({"admin", "dept_admin"}),
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PermissionDenied(Exception):
    """Raised by :func:`requires` / :func:`check` when access is denied.

    The FastAPI exception handler in ``admin-dashboard-api`` converts
    this into ``HTTP 403`` and emits a single
    ``audit_logger.AuditEvent(action="rbac_denied", result="denied")``
    row carrying ``actor_id`` / ``actor_role`` / ``dept_id`` from the
    rejected context.

    The default message is intentionally generic so we do not leak
    the specific role required to an unauthenticated caller — leaving
    that detail to the audit log instead. Callers may pass a custom
    ``reason`` for diagnostic logs but it should not be propagated
    verbatim into HTTP responses.
    """

    def __init__(
        self,
        *,
        actor_role: str | None,
        required_role: str,
        dept_id: str | None,
        reason: str | None = None,
    ) -> None:
        message = reason or (
            f"actor with role={actor_role!r} cannot perform action requiring "
            f"role={required_role!r}"
            + (f" on dept={dept_id!r}" if dept_id else "")
        )
        super().__init__(message)
        self.actor_role = actor_role
        self.required_role = required_role
        self.dept_id = dept_id


class MissingActorError(PermissionDenied):
    """Raised when no :class:`AuthContext` could be located.

    A subclass of :class:`PermissionDenied` so ``except
    PermissionDenied`` catches both the "no actor" and "wrong role"
    cases — the FastAPI handler in admin-dashboard-api converts both
    into HTTP 403 with an ``rbac_denied`` audit event.
    """

    def __init__(self, required_role: str, dept_id: str | None) -> None:
        super().__init__(
            actor_role=None,
            required_role=required_role,
            dept_id=dept_id,
            reason="no AuthContext was provided to a guarded callable",
        )


# ---------------------------------------------------------------------------
# Guard core
# ---------------------------------------------------------------------------


def check(
    actor: AuthContext | None,
    required_role: Role,
    dept_id: str | None = None,
) -> None:
    """Raise :class:`PermissionDenied` if ``actor`` cannot satisfy the guard.

    This is the single source of truth for RBAC decisions in the
    platform. Every other helper in this module — including the
    :func:`requires` decorator — eventually delegates here.

    Decision matrix:

    * ``required_role="admin"`` — only the ``admin`` role passes;
      ``dept_admin`` and below are denied. ``dept_id`` is ignored
      because admin actions are global by definition.
    * ``required_role="dept_admin"`` — admits ``admin`` (always) and
      ``dept_admin`` (only when ``dept_id`` is ``None`` or matches
      the actor's ``dept_ids``).
    * ``required_role="lead"`` — admits ``lead`` and ``admin``
      globally, plus ``dept_admin`` for matching dept (or no dept).
    * ``required_role="viewer"`` — admits everyone; ``dept_admin``
      still must match ``dept_id`` if one is supplied.

    Args:
        actor: The :class:`AuthContext` extracted from the OIDC
            token. ``None`` raises :class:`MissingActorError`.
        required_role: The minimum role the operation requires.
        dept_id: Optional dept identifier. When supplied, the actor
            must either be ``admin`` (global) or be in the actor's
            ``dept_ids`` (dept-scoped roles).

    Raises:
        PermissionDenied: When the actor's role / dept membership
            does not satisfy the requirement.
        MissingActorError: When ``actor`` is ``None``.
        ValueError: When ``required_role`` is not a member of
            :data:`ROLES`.
    """

    if required_role not in ROLES:
        # Surfacing this as ``ValueError`` (not ``PermissionDenied``)
        # is intentional: a caller passing an unknown role is a
        # programmer error, not an authorisation outcome.
        raise ValueError(
            f"unknown role {required_role!r}; expected one of {sorted(ROLES)}"
        )

    if actor is None:
        raise MissingActorError(required_role, dept_id)

    if actor.actor_role not in ROLES:
        # Defence in depth: ``extract_auth_context`` already
        # validates this, but a hand-built :class:`AuthContext` (eg.
        # in unit tests) could still slip through with a typo.
        raise PermissionDenied(
            actor_role=actor.actor_role,
            required_role=required_role,
            dept_id=dept_id,
        )

    # ------------------------------------------------------------------
    # Role-class check
    # ------------------------------------------------------------------
    allowed_roles = _ROLES_THAT_SATISFY[required_role]
    if actor.actor_role not in allowed_roles:
        raise PermissionDenied(
            actor_role=actor.actor_role,
            required_role=required_role,
            dept_id=dept_id,
        )

    # ------------------------------------------------------------------
    # Dept-scope check
    # ------------------------------------------------------------------
    # ``admin`` always satisfies the dept check. Everyone else must
    # have the dept in their ``dept_ids`` set when one is supplied.
    if dept_id is not None and actor.actor_role != "admin":
        if dept_id not in actor.dept_ids:
            raise PermissionDenied(
                actor_role=actor.actor_role,
                required_role=required_role,
                dept_id=dept_id,
                reason=(
                    f"actor with role={actor.actor_role!r} is not a member of "
                    f"dept={dept_id!r}"
                ),
            )


def is_allowed(
    actor: AuthContext | None,
    required_role: Role,
    dept_id: str | None = None,
) -> bool:
    """Boolean variant of :func:`check`.

    Useful for UI affordances ("hide this button when the actor
    cannot click it") where raising an exception would be awkward.
    Mirrors the exact decision matrix of :func:`check` — any
    invariant that holds for one holds for the other.
    """

    try:
        check(actor, required_role, dept_id)
    except PermissionDenied:
        return False
    return True


# ---------------------------------------------------------------------------
# Decorator surface
# ---------------------------------------------------------------------------


F = TypeVar("F", bound=Callable[..., Any])


def requires(
    role: Role,
    dept_id: str | None = None,
    *,
    actor_arg: str = "actor",
    dept_id_arg: str | None = None,
) -> Callable[[F], F]:
    """Return a decorator that enforces RBAC on the wrapped callable.

    The decorator works on both synchronous and asynchronous
    callables; ``inspect.iscoroutinefunction`` selects the wrapper at
    decoration time.

    Resolution rules (in order of precedence):

    1. The :class:`AuthContext` is read from the keyword argument
       named by ``actor_arg`` (default: ``"actor"``). If absent, the
       decorator falls back to the **first** positional argument of
       type :class:`AuthContext`.
    2. The optional ``dept_id`` is read from the keyword argument
       named by ``dept_id_arg`` if that name is provided; otherwise
       the static ``dept_id`` argument to :func:`requires` is used.
       This lets call sites pin a dept at decoration time *or* read
       it from a path/body parameter at call time::

           @requires("dept_admin", dept_id_arg="dept_id")
           async def rotate(actor: AuthContext, dept_id: str) -> None: ...

    Args:
        role: The minimum role required.
        dept_id: When set, the actor's ``dept_ids`` must contain this
            value (or the actor must be ``admin``). Mutually
            exclusive with a non-``None`` ``dept_id_arg`` — a
            :class:`ValueError` is raised at decoration time if both
            are supplied.
        actor_arg: Keyword-argument name carrying the
            :class:`AuthContext`. Defaults to ``"actor"``.
        dept_id_arg: When set, the dept_id is taken from this keyword
            argument of the wrapped call instead of the static
            ``dept_id`` parameter.

    Returns:
        A decorator that returns the wrapped callable unchanged in
        type but which now performs the RBAC check on every
        invocation. Failures raise :class:`PermissionDenied`.
    """

    if dept_id is not None and dept_id_arg is not None:
        raise ValueError(
            "requires() accepts either a static dept_id or dept_id_arg, "
            "not both"
        )
    if role not in ROLES:
        raise ValueError(
            f"unknown role {role!r}; expected one of {sorted(ROLES)}"
        )

    def _decorator(func: F) -> F:
        # Resolve the actor and dept_id from the call's bound args.
        signature = inspect.signature(func)

        def _resolve(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[
            AuthContext | None, str | None
        ]:
            # ``Signature.bind_partial`` tolerates missing required
            # arguments so we can still extract whatever is present
            # before the function-level error fires.
            try:
                bound = signature.bind_partial(*args, **kwargs)
            except TypeError:
                bound = inspect.BoundArguments(signature, {})

            actor: AuthContext | None = bound.arguments.get(actor_arg)
            if actor is None:
                # Fallback: scan positional args for the first
                # AuthContext value. This makes ``requires`` ergonomic
                # for free functions that pass the actor positionally
                # without a fixed parameter name.
                for value in args:
                    if isinstance(value, AuthContext):
                        actor = value
                        break

            resolved_dept_id: str | None
            if dept_id_arg is not None:
                resolved_dept_id = bound.arguments.get(dept_id_arg)
            else:
                resolved_dept_id = dept_id
            return actor, resolved_dept_id

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
                actor, target_dept = _resolve(args, kwargs)
                check(actor, role, target_dept)
                # ``func`` is an ``Awaitable`` factory after binding.
                coro: Awaitable[Any] = func(*args, **kwargs)
                return await coro

            return _async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            actor, target_dept = _resolve(args, kwargs)
            check(actor, role, target_dept)
            return func(*args, **kwargs)

        return _sync_wrapper  # type: ignore[return-value]

    return _decorator


def requires_role(
    actor: AuthContext | None,
    role: Role,
    dept_id: str | None = None,
) -> None:
    """Imperative wrapper around :func:`check`.

    Provided as the ergonomic spelling for FastAPI route handlers
    that need to read the dept_id from the request path before
    deciding whom to admit::

        @router.post("/admin/departments/{dept_id}/credentials/rotate")
        async def rotate(dept_id: str, actor: AuthContext = ...) -> None:
            requires_role(actor, "dept_admin", dept_id=dept_id)
            ...

    The function is identical to :func:`check` except for the
    parameter ordering, which matches the natural reading of "this
    actor requires this role on this dept".
    """

    check(actor, role, dept_id)


__all__ = [
    "Role",
    "ROLES",
    "PermissionDenied",
    "MissingActorError",
    "check",
    "is_allowed",
    "requires",
    "requires_role",
]
