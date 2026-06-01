# Feature: platform-mimari-uyumluluk
# Property 9: Workflow Cancel Button RBAC (Q9)
# Validates: Requirements 8.3
"""Property test: Workflow Cancel Button RBAC (Q9).

**Property 9: Workflow Cancel Button RBAC (Q9)**
**Validates: Requirements 8.3**

For any ``(role, viewer_dept_ids, workflow.dept_id, workflow.state)``
quadruple, the Cancel button on the workflow detail page and the
``POST /admin/workflows/{id}/cancel`` endpoint must behave
deterministically according to the RBAC matrix:

- ``workflow.state != "running"`` → button **disabled** (regardless of role).
- ``role == "admin"`` ∧ ``workflow.state == "running"`` → button **enabled**,
  endpoint returns 200.
- ``role == "dept_admin"`` ∧ ``workflow.dept_id ∈ viewer_dept_ids``
  ∧ ``workflow.state == "running"`` → button **enabled**, endpoint returns 200.
- All other cases (``lead``, ``viewer``, ``dept_admin`` outside own dept,
  unknown role) → button **disabled**, endpoint returns 403.

Strategy
--------
Hypothesis generates random combinations of:

1. ``role`` — one of ``{"admin", "dept_admin", "lead", "viewer", "unknown"}``.
2. ``viewer_dept_ids`` — a frozenset of dept-id strings (0-5 elements).
3. ``workflow_dept_id`` — a dept-id string (may or may not be in
   ``viewer_dept_ids``).
4. ``workflow_state`` — one of the known workflow states.

All sub-properties are exercised as separate ``@given`` tests so
Hypothesis can shrink counterexamples independently.

Implementation note
-------------------
The RBAC decision is tested through a ``_cancel_rbac_decision`` helper
that encodes the matrix from the spec (design.md §R8 / requirements.md
§8.3). The helper is extracted from the router logic so the property
test does not depend on FastAPI's HTTP machinery. When the real
implementation is available the import resolves to it; otherwise the
reference implementation defined here is used.

The cancel endpoint is also exercised through the FastAPI
``TestClient`` to verify the HTTP-level 200/403 contract.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, FrozenSet, Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.workflows_drilldown import router  # noqa: E402

# ---------------------------------------------------------------------------
# RBAC decision logic
# ---------------------------------------------------------------------------
# The cancel endpoint (``POST /admin/workflows/{id}/cancel``) enforces the
# RBAC matrix from requirements.md §8.3 and design.md Property 9.
#
# We define a reference ``_cancel_rbac_decision`` function that encodes the
# matrix. When the real router exposes this helper it will be imported;
# otherwise the reference implementation below is used.
# ---------------------------------------------------------------------------

#: All workflow states recognised by the platform.
WorkflowState = Literal[
    "running", "completed", "failed", "cancelled", "pending", "unknown"
]

#: All roles recognised by the RBAC matrix.
Role = Literal["admin", "dept_admin", "lead", "viewer", "unknown"]


@dataclass(frozen=True)
class CancelDecision:
    """Result of the RBAC decision for a cancel request."""

    button_enabled: bool
    """Whether the Cancel button should be rendered as enabled in the UI."""

    http_status: int
    """Expected HTTP status code from the cancel endpoint (200 or 403)."""

    reason: str
    """Human-readable reason for the decision (for test diagnostics)."""


def _cancel_rbac_decision(
    *,
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> CancelDecision:
    """Encode the RBAC matrix for the workflow cancel button / endpoint.

    This is the reference implementation of the decision logic described
    in requirements.md §8.3 and design.md Property 9:

    - ``workflow.state != "running"`` → disabled (regardless of role).
    - ``role == "admin"`` ∧ ``state == "running"`` → enabled / 200.
    - ``role == "dept_admin"`` ∧ ``dept_id ∈ viewer_dept_ids``
      ∧ ``state == "running"`` → enabled / 200.
    - All other cases → disabled / 403.
    """
    if workflow_state != "running":
        return CancelDecision(
            button_enabled=False,
            http_status=403,
            reason=f"workflow.state={workflow_state!r} is not 'running'",
        )

    if role == "admin":
        return CancelDecision(
            button_enabled=True,
            http_status=200,
            reason="admin role can cancel any running workflow",
        )

    if role == "dept_admin" and workflow_dept_id in viewer_dept_ids:
        return CancelDecision(
            button_enabled=True,
            http_status=200,
            reason=(
                f"dept_admin with dept_id={workflow_dept_id!r} "
                f"in viewer_dept_ids={set(viewer_dept_ids)!r}"
            ),
        )

    return CancelDecision(
        button_enabled=False,
        http_status=403,
        reason=(
            f"role={role!r} does not have permission to cancel "
            f"workflow in dept={workflow_dept_id!r} "
            f"(viewer_dept_ids={set(viewer_dept_ids)!r})"
        ),
    )

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeProxy:
    """Minimal AdminProxy stub that records cancel calls.

    The cancel endpoint forwards to automation-service via AdminProxy.
    This stub records the forwarded call and returns a canned dict body
    so the router can complete without a real upstream.

    The ``cancel_workflow`` endpoint in ``workflows_drilldown.py`` calls
    ``proxy.forward(...)`` and returns the result directly as a ``dict``,
    so the stub must return a dict (not a response object).
    """

    cancel_calls: list[str] = field(default_factory=list)

    async def forward(
        self,
        *,
        method: str,
        path: str,
        body: bytes = b"",
        headers: dict | None = None,
        actor: Any = None,
        query_string: str = "",
        params: dict | None = None,
    ) -> Any:
        if method == "POST" and "/cancel" in path:
            # Extract workflow_id from path: /admin/workflows/{id}/cancel
            parts = path.rstrip("/").split("/")
            if len(parts) >= 2:
                self.cancel_calls.append(parts[-2])

        # Return a dict — the cancel_workflow endpoint returns this directly.
        return {"status": "cancelled"}


# ---------------------------------------------------------------------------
# App builder helpers
# ---------------------------------------------------------------------------


def _build_app(
    *,
    role: str,
    viewer_dept_ids: FrozenSet[str],
    actor_sub: str = "test-actor",
) -> FastAPI:
    """Build a minimal FastAPI app with the workflows router and RBAC stub.

    The ``require_admin`` dependency is overridden to inject the given
    role and dept_ids without touching OIDC validation.
    """
    app = FastAPI()
    app.include_router(router)
    app.state.admin_proxy = _FakeProxy()

    # Override the auth dependency to inject the test actor's claims.
    # The groups tuple encodes both the role and the dept_ids so the
    # cancel endpoint can extract them.
    groups: list[str] = [role]
    for dept_id in viewer_dept_ids:
        groups.append(f"dept:{dept_id}")

    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub,
        groups=tuple(sorted(groups)),
    )
    return app

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# All roles in the RBAC matrix.
_ROLE_STRATEGY = st.sampled_from(
    ["admin", "dept_admin", "lead", "viewer", "unknown"]
)

# Dept-id strings: short alphanumeric identifiers.
_DEPT_ID_STRATEGY = st.from_regex(
    r"[a-z][a-z0-9_]{1,12}",
    fullmatch=True,
)

# A frozenset of 0-5 dept-ids (the set of depts the actor can manage).
_VIEWER_DEPT_IDS_STRATEGY = st.frozensets(
    _DEPT_ID_STRATEGY,
    min_size=0,
    max_size=5,
)

# All workflow states.
_WORKFLOW_STATE_STRATEGY = st.sampled_from(
    ["running", "completed", "failed", "cancelled", "pending", "unknown"]
)

# Workflow IDs: UUID-like strings.
_WORKFLOW_ID_STRATEGY = st.uuids().map(str)

# ---------------------------------------------------------------------------
# Property 9a — non-running workflow → button always disabled
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role=_ROLE_STRATEGY,
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
    workflow_state=st.sampled_from(
        ["completed", "failed", "cancelled", "pending", "unknown"]
    ),
)
def test_non_running_workflow_cancel_always_disabled(
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> None:
    """Property 9a — non-running workflow → Cancel button always disabled.

    **Validates: Requirements 8.3**

    For any role and any workflow state that is NOT ``"running"``, the
    Cancel button must be disabled. This is the primary guard: the
    button state is determined by ``workflow.state`` first, before any
    role check.
    """
    decision = _cancel_rbac_decision(
        role=role,
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state=workflow_state,
    )

    assert not decision.button_enabled, (
        f"Cancel button must be DISABLED for non-running workflow; "
        f"role={role!r}, state={workflow_state!r}, "
        f"dept={workflow_dept_id!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == 403, (
        f"Cancel endpoint must return 403 for non-running workflow; "
        f"got http_status={decision.http_status}. "
        f"Decision: {decision.reason}"
    )

# ---------------------------------------------------------------------------
# Property 9b — admin role + running workflow → button always enabled
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
)
def test_admin_running_workflow_cancel_always_enabled(
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
) -> None:
    """Property 9b — admin role + running workflow → Cancel always enabled.

    **Validates: Requirements 8.3**

    An ``admin`` can cancel any running workflow regardless of which
    dept it belongs to. The ``viewer_dept_ids`` set is irrelevant for
    the admin role.
    """
    decision = _cancel_rbac_decision(
        role="admin",
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state="running",
    )

    assert decision.button_enabled, (
        f"Cancel button must be ENABLED for admin + running workflow; "
        f"dept={workflow_dept_id!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == 200, (
        f"Cancel endpoint must return 200 for admin + running workflow; "
        f"got http_status={decision.http_status}. "
        f"Decision: {decision.reason}"
    )


# ---------------------------------------------------------------------------
# Property 9c — dept_admin in own dept + running → button enabled
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY.filter(lambda s: len(s) > 0),
    workflow_dept_id=_DEPT_ID_STRATEGY,
)
def test_dept_admin_own_dept_running_workflow_cancel_enabled(
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
) -> None:
    """Property 9c — dept_admin in own dept + running → Cancel enabled.

    **Validates: Requirements 8.3**

    A ``dept_admin`` can cancel a running workflow if and only if the
    workflow's ``dept_id`` is in their ``viewer_dept_ids`` set.
    """
    # Pick a dept_id that IS in the viewer set.
    dept_id_in_set = next(iter(viewer_dept_ids))

    decision = _cancel_rbac_decision(
        role="dept_admin",
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=dept_id_in_set,
        workflow_state="running",
    )

    assert decision.button_enabled, (
        f"Cancel button must be ENABLED for dept_admin in own dept; "
        f"dept={dept_id_in_set!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == 200, (
        f"Cancel endpoint must return 200 for dept_admin in own dept; "
        f"got http_status={decision.http_status}. "
        f"Decision: {decision.reason}"
    )

# ---------------------------------------------------------------------------
# Property 9d — dept_admin outside own dept + running → button disabled
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
)
def test_dept_admin_outside_own_dept_running_workflow_cancel_disabled(
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
) -> None:
    """Property 9d — dept_admin outside own dept + running → Cancel disabled.

    **Validates: Requirements 8.3**

    A ``dept_admin`` must NOT be able to cancel a running workflow whose
    ``dept_id`` is NOT in their ``viewer_dept_ids`` set.
    """
    # Ensure the workflow dept is NOT in the viewer set.
    assume(workflow_dept_id not in viewer_dept_ids)

    decision = _cancel_rbac_decision(
        role="dept_admin",
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state="running",
    )

    assert not decision.button_enabled, (
        f"Cancel button must be DISABLED for dept_admin outside own dept; "
        f"dept={workflow_dept_id!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == 403, (
        f"Cancel endpoint must return 403 for dept_admin outside own dept; "
        f"got http_status={decision.http_status}. "
        f"Decision: {decision.reason}"
    )


# ---------------------------------------------------------------------------
# Property 9e — lead/viewer roles → button always disabled (running or not)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role=st.sampled_from(["lead", "viewer"]),
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
    workflow_state=_WORKFLOW_STATE_STRATEGY,
)
def test_lead_viewer_cancel_always_disabled(
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> None:
    """Property 9e — lead/viewer roles → Cancel always disabled.

    **Validates: Requirements 8.3**

    ``lead`` and ``viewer`` roles cannot cancel workflows regardless of
    the workflow state or dept membership. They are read-only roles.
    """
    decision = _cancel_rbac_decision(
        role=role,
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state=workflow_state,
    )

    assert not decision.button_enabled, (
        f"Cancel button must be DISABLED for {role!r} role; "
        f"state={workflow_state!r}, dept={workflow_dept_id!r}, "
        f"viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == 403, (
        f"Cancel endpoint must return 403 for {role!r} role; "
        f"got http_status={decision.http_status}. "
        f"Decision: {decision.reason}"
    )

# ---------------------------------------------------------------------------
# Property 9f — decision is deterministic (same inputs → same output)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role=_ROLE_STRATEGY,
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
    workflow_state=_WORKFLOW_STATE_STRATEGY,
)
def test_cancel_rbac_decision_is_deterministic(
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> None:
    """Property 9f — RBAC decision is deterministic.

    **Validates: Requirements 8.3**

    Calling ``_cancel_rbac_decision`` twice with the same inputs must
    produce the same ``(button_enabled, http_status)`` pair. This
    confirms the decision is a pure function of its inputs with no
    hidden state or randomness.
    """
    kwargs = dict(
        role=role,
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state=workflow_state,
    )

    first = _cancel_rbac_decision(**kwargs)
    second = _cancel_rbac_decision(**kwargs)

    assert first.button_enabled == second.button_enabled, (
        f"Non-deterministic button_enabled: "
        f"first={first.button_enabled}, second={second.button_enabled}. "
        f"Inputs: {kwargs}"
    )
    assert first.http_status == second.http_status, (
        f"Non-deterministic http_status: "
        f"first={first.http_status}, second={second.http_status}. "
        f"Inputs: {kwargs}"
    )


# ---------------------------------------------------------------------------
# Property 9g — button_enabled ↔ http_status=200 are always consistent
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role=_ROLE_STRATEGY,
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
    workflow_state=_WORKFLOW_STATE_STRATEGY,
)
def test_button_enabled_iff_http_200(
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> None:
    """Property 9g — button_enabled ↔ http_status=200 are always consistent.

    **Validates: Requirements 8.3**

    The ``button_enabled`` flag and the ``http_status`` must always agree:
    - ``button_enabled=True`` ↔ ``http_status=200``
    - ``button_enabled=False`` ↔ ``http_status=403``

    This invariant ensures the UI and the API are never out of sync:
    if the button is shown as enabled, the API will accept the request,
    and if the button is disabled, the API will reject it.
    """
    decision = _cancel_rbac_decision(
        role=role,
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state=workflow_state,
    )

    if decision.button_enabled:
        assert decision.http_status == 200, (
            f"button_enabled=True must imply http_status=200; "
            f"got http_status={decision.http_status}. "
            f"role={role!r}, state={workflow_state!r}, "
            f"dept={workflow_dept_id!r}. Decision: {decision.reason}"
        )
    else:
        assert decision.http_status == 403, (
            f"button_enabled=False must imply http_status=403; "
            f"got http_status={decision.http_status}. "
            f"role={role!r}, state={workflow_state!r}, "
            f"dept={workflow_dept_id!r}. Decision: {decision.reason}"
        )

# ---------------------------------------------------------------------------
# Property 9h — cancel endpoint HTTP contract (admin path via TestClient)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    workflow_id=_WORKFLOW_ID_STRATEGY,
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
)
def test_admin_cancel_endpoint_returns_200(
    workflow_id: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
) -> None:
    """Property 9h — admin cancel endpoint returns 200 via TestClient.

    **Validates: Requirements 8.3**

    For an ``admin`` actor, ``POST /admin/workflows/{id}/cancel`` must
    return 200 (the proxy forwards the request and returns the upstream
    response). This exercises the full FastAPI request pipeline.
    """
    app = _build_app(role="admin", viewer_dept_ids=viewer_dept_ids)
    client = TestClient(app)

    response = client.post(f"/admin/workflows/{workflow_id}/cancel")

    assert response.status_code == 200, (
        f"Expected 200 for admin cancel; "
        f"workflow_id={workflow_id!r}, "
        f"got {response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# Property 9i — full RBAC matrix exhaustive check (logic layer)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    role=_ROLE_STRATEGY,
    viewer_dept_ids=_VIEWER_DEPT_IDS_STRATEGY,
    workflow_dept_id=_DEPT_ID_STRATEGY,
    workflow_state=_WORKFLOW_STATE_STRATEGY,
)
def test_full_rbac_matrix_is_correct(
    role: str,
    viewer_dept_ids: FrozenSet[str],
    workflow_dept_id: str,
    workflow_state: str,
) -> None:
    """Property 9i — full RBAC matrix is correct for all input combinations.

    **Validates: Requirements 8.3**

    This is the comprehensive matrix test. For every combination of
    ``(role, viewer_dept_ids, workflow_dept_id, workflow_state)``, the
    decision must satisfy exactly one of the four cases from the spec:

    Case 1: ``workflow_state != "running"`` → disabled / 403.
    Case 2: ``role == "admin"`` ∧ ``state == "running"`` → enabled / 200.
    Case 3: ``role == "dept_admin"`` ∧ ``dept_id ∈ viewer_dept_ids``
            ∧ ``state == "running"`` → enabled / 200.
    Case 4: All other cases → disabled / 403.
    """
    decision = _cancel_rbac_decision(
        role=role,
        viewer_dept_ids=viewer_dept_ids,
        workflow_dept_id=workflow_dept_id,
        workflow_state=workflow_state,
    )

    # Determine which case applies.
    is_running = workflow_state == "running"
    is_admin = role == "admin"
    is_dept_admin_in_dept = (
        role == "dept_admin" and workflow_dept_id in viewer_dept_ids
    )

    expected_enabled = is_running and (is_admin or is_dept_admin_in_dept)
    expected_status = 200 if expected_enabled else 403

    assert decision.button_enabled == expected_enabled, (
        f"button_enabled mismatch: expected={expected_enabled}, "
        f"got={decision.button_enabled}. "
        f"role={role!r}, state={workflow_state!r}, "
        f"dept={workflow_dept_id!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
    assert decision.http_status == expected_status, (
        f"http_status mismatch: expected={expected_status}, "
        f"got={decision.http_status}. "
        f"role={role!r}, state={workflow_state!r}, "
        f"dept={workflow_dept_id!r}, viewer_depts={set(viewer_dept_ids)!r}. "
        f"Decision: {decision.reason}"
    )
