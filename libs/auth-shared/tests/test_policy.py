"""Unit tests for :mod:`auth_shared.policy` (RBAC guard helpers).

Covers the four-role decision matrix called out in tasks.md §8.1
("`requires(role, dept_id=None)` decorator/guard helper") and the
acceptance criteria pinned by Requirements 7.1, 7.3, 7.5, 7.6:

* Role enumeration is exactly ``{"viewer", "lead", "admin",
  "dept_admin"}`` (R7.1).
* The viewer→lead→admin precedence holds for guards that only
  require global roles (R7.3).
* ``required_role="admin"`` is global-only — ``dept_admin`` is
  rejected for global actions (R7.5).
* ``dept_admin`` may perform dept-scoped operations on its own
  dept_id but is denied for any other dept (R7.3, R7.6).
* ``admin`` always satisfies any dept-scoped check (R7.5).
* The decorator surface works for both sync and async callables and
  raises :class:`PermissionDenied` rather than letting the wrapped
  function execute.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from auth_shared import (
    AUTH_ROLES,
    AuthContext,
    MissingActorError,
    PermissionDenied,
    ROLES,
    Role,
    check,
    is_allowed,
    requires,
    requires_role,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(role: str, *dept_ids: str, actor_id: str = "user-1") -> AuthContext:
    """Build a minimal :class:`AuthContext` for tests."""

    return AuthContext(
        actor_id=actor_id,
        actor_role=role,  # type: ignore[arg-type]
        dept_ids=frozenset(dept_ids),
        raw_claims={"sub": actor_id, "role": role},
    )


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestRoleEnumeration:
    def test_role_runtime_mirror_matches_literal(self) -> None:
        # Exactly the four roles required by R7.1 — no ``"system"``,
        # which is an audit-only role attached to background events
        # (see audit_logger.event.AuditRole) and is not a valid
        # ``actor_role`` for an authenticated request.
        assert ROLES == {"viewer", "lead", "admin", "dept_admin"}

    def test_role_aliases_are_in_lock_step_with_oidc_module(self) -> None:
        # tasks.md §8.1 names the literal ``Role``; oidc.py uses
        # ``AuthRole`` for the same enumeration. The two must remain
        # one-and-the-same set so audit writes / RBAC decisions /
        # OIDC claim extraction agree on what a "valid role" is.
        assert ROLES == AUTH_ROLES


# ---------------------------------------------------------------------------
# check() — the decision core
# ---------------------------------------------------------------------------


class TestCheckGlobalRoles:
    @pytest.mark.parametrize(
        "actor_role",
        ["viewer", "lead", "admin", "dept_admin"],
    )
    def test_viewer_required_admits_every_role(self, actor_role: str) -> None:
        check(_ctx(actor_role), "viewer")

    @pytest.mark.parametrize(
        "actor_role,allowed",
        [
            ("viewer", False),
            ("lead", True),
            ("admin", True),
            ("dept_admin", True),
        ],
    )
    def test_lead_required_admits_lead_admin_and_dept_admin(
        self, actor_role: str, allowed: bool
    ) -> None:
        if allowed:
            check(_ctx(actor_role), "lead")
        else:
            with pytest.raises(PermissionDenied):
                check(_ctx(actor_role), "lead")

    @pytest.mark.parametrize(
        "actor_role,allowed",
        [
            ("viewer", False),
            ("lead", False),
            ("admin", True),
            ("dept_admin", False),
        ],
    )
    def test_admin_required_is_admin_only(
        self, actor_role: str, allowed: bool
    ) -> None:
        # R7.5: global actions (new dept, global prompt, SSH runner
        # config) are admin-only — dept_admin is denied.
        if allowed:
            check(_ctx(actor_role), "admin")
        else:
            with pytest.raises(PermissionDenied):
                check(_ctx(actor_role), "admin")


class TestCheckDeptScope:
    def test_admin_passes_dept_scoped_check_without_membership(self) -> None:
        # admin always sees every dept (Requirement 7.5).
        check(_ctx("admin"), "dept_admin", dept_id="payments")

    def test_dept_admin_passes_for_own_dept(self) -> None:
        check(_ctx("dept_admin", "payments"), "dept_admin", dept_id="payments")

    def test_dept_admin_is_denied_for_other_dept(self) -> None:
        # R7.3: cross-dept access is denied with HTTP 403.
        with pytest.raises(PermissionDenied):
            check(
                _ctx("dept_admin", "payments"),
                "dept_admin",
                dept_id="risk",
            )

    def test_dept_admin_denied_with_empty_dept_ids(self) -> None:
        with pytest.raises(PermissionDenied):
            check(_ctx("dept_admin"), "dept_admin", dept_id="payments")

    def test_lead_can_see_own_dept_when_required_role_is_viewer(self) -> None:
        check(_ctx("lead", "payments"), "viewer", dept_id="payments")

    def test_lead_denied_for_other_dept_when_required_role_is_viewer(
        self,
    ) -> None:
        with pytest.raises(PermissionDenied):
            check(_ctx("lead", "payments"), "viewer", dept_id="risk")

    def test_dept_id_none_skips_dept_scope_check(self) -> None:
        # No dept_id => purely role-based decision; dept_admin with
        # empty dept_ids still passes a viewer-required guard.
        check(_ctx("dept_admin"), "viewer")


class TestCheckErrors:
    def test_unknown_required_role_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            check(_ctx("admin"), "superuser")  # type: ignore[arg-type]

    def test_no_actor_raises_missing_actor_error(self) -> None:
        with pytest.raises(MissingActorError):
            check(None, "viewer")

    def test_missing_actor_is_subclass_of_permission_denied(self) -> None:
        # Single ``except PermissionDenied`` clause must cover both
        # the no-actor and wrong-role cases (Requirement 7.3, 7.9).
        with pytest.raises(PermissionDenied):
            check(None, "viewer")

    def test_actor_role_typo_is_denied_not_raised_as_value_error(self) -> None:
        # A hand-built AuthContext with an invalid role should be
        # rejected the same way as a wrong-role scenario, not crash
        # with a TypeError inside the guard.
        actor = AuthContext(
            actor_id="u",
            actor_role="superuser",  # type: ignore[arg-type]
            dept_ids=frozenset(),
        )
        with pytest.raises(PermissionDenied):
            check(actor, "viewer")


class TestIsAllowed:
    def test_returns_true_when_check_passes(self) -> None:
        assert is_allowed(_ctx("admin"), "admin") is True

    def test_returns_false_when_check_raises(self) -> None:
        assert is_allowed(_ctx("viewer"), "admin") is False

    def test_returns_false_when_actor_is_none(self) -> None:
        assert is_allowed(None, "viewer") is False


# ---------------------------------------------------------------------------
# requires() — decorator surface
# ---------------------------------------------------------------------------


class TestRequiresDecorator:
    def test_sync_function_runs_when_role_satisfied(self) -> None:
        @requires("lead")
        def handler(actor: AuthContext) -> str:
            return f"hello {actor.actor_id}"

        result = handler(actor=_ctx("admin"))

        assert result == "hello user-1"

    def test_sync_function_blocked_when_role_insufficient(self) -> None:
        calls: list[str] = []

        @requires("admin")
        def handler(actor: AuthContext) -> None:
            calls.append("ran")  # pragma: no cover - must not execute

        with pytest.raises(PermissionDenied):
            handler(actor=_ctx("lead"))
        assert calls == []

    def test_async_function_runs_when_role_satisfied(self) -> None:
        @requires("admin")
        async def handler(actor: AuthContext) -> str:
            return f"hello {actor.actor_id}"

        result = asyncio.run(handler(actor=_ctx("admin")))

        assert result == "hello user-1"

    def test_async_function_blocked_when_role_insufficient(self) -> None:
        calls: list[str] = []

        @requires("admin")
        async def handler(actor: AuthContext) -> None:
            calls.append("ran")  # pragma: no cover - must not execute

        with pytest.raises(PermissionDenied):
            asyncio.run(handler(actor=_ctx("dept_admin")))
        assert calls == []

    def test_decorator_resolves_actor_from_first_positional_arg(self) -> None:
        # Functions that pass ``actor`` positionally without a
        # standardised name (eg. ``ctx``) should still benefit from
        # the guard.
        @requires("lead")
        def handler(ctx: AuthContext, payload: dict[str, Any]) -> dict[str, Any]:
            return {"actor": ctx.actor_id, **payload}

        result = handler(_ctx("admin"), {"value": 1})

        assert result == {"actor": "user-1", "value": 1}

    def test_decorator_static_dept_id_is_enforced(self) -> None:
        @requires("dept_admin", dept_id="payments")
        def rotate(actor: AuthContext) -> str:
            return "rotated"

        # Same dept => OK.
        assert rotate(actor=_ctx("dept_admin", "payments")) == "rotated"

        # Other dept => denied.
        with pytest.raises(PermissionDenied):
            rotate(actor=_ctx("dept_admin", "risk"))

    def test_decorator_dept_id_arg_is_resolved_at_call_time(self) -> None:
        # R7.6: dept_admin rotates *its own* department's credentials
        # — the dept comes from the path parameter, not a static arg.
        @requires("dept_admin", dept_id_arg="dept_id")
        def rotate(actor: AuthContext, dept_id: str) -> str:
            return f"rotated:{dept_id}"

        assert (
            rotate(actor=_ctx("dept_admin", "payments"), dept_id="payments")
            == "rotated:payments"
        )

        with pytest.raises(PermissionDenied):
            rotate(actor=_ctx("dept_admin", "payments"), dept_id="risk")

    def test_static_dept_id_and_dept_id_arg_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError):
            requires("dept_admin", dept_id="payments", dept_id_arg="dept_id")

    def test_unknown_role_at_decoration_time_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            requires("god")  # type: ignore[arg-type]

    def test_decorator_raises_when_actor_argument_missing(self) -> None:
        @requires("viewer")
        def handler(actor: AuthContext) -> None:  # pragma: no cover
            pass

        with pytest.raises(MissingActorError):
            handler()  # type: ignore[call-arg]

    def test_admin_bypasses_dept_id_check(self) -> None:
        # R7.5: admin sees every dept regardless of membership.
        @requires("dept_admin", dept_id_arg="dept_id")
        def rotate(actor: AuthContext, dept_id: str) -> str:
            return "ok"

        # admin actor with no dept membership at all should still
        # pass when the path parameter targets an arbitrary dept.
        assert rotate(actor=_ctx("admin"), dept_id="risk") == "ok"

    def test_decorator_preserves_wrapped_function_metadata(self) -> None:
        @requires("viewer")
        def handler(actor: AuthContext) -> None:
            """do something"""

        assert handler.__name__ == "handler"
        assert handler.__doc__ == "do something"


# ---------------------------------------------------------------------------
# requires_role() — imperative spelling
# ---------------------------------------------------------------------------


class TestRequiresRole:
    def test_passes_when_actor_satisfies(self) -> None:
        requires_role(_ctx("admin"), "admin")

    def test_raises_when_actor_does_not_satisfy(self) -> None:
        with pytest.raises(PermissionDenied):
            requires_role(_ctx("viewer"), "admin")

    def test_dept_scoping_is_applied(self) -> None:
        with pytest.raises(PermissionDenied):
            requires_role(
                _ctx("dept_admin", "payments"),
                "dept_admin",
                dept_id="risk",
            )
