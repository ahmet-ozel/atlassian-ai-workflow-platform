"""Property test: Dept credential RBAC forward determinism.

For any combination of ``(role, viewer_dept_ids, target_dept_id,
action)`` the RBAC decision matrix is **deterministic** across the two
boundaries that guard the dept-credential CRUD + probe surface:

1. **AdminProxy boundary** (``admin-dashboard-api/src/proxy.py``).
   :func:`classify_admin_path` resolves the path's
   ``(required_role, dept_id)`` tuple; :func:`auth_shared.policy.check`
   then admits or denies the actor.  Denials emit a single
   ``rbac_denied`` :class:`AuditEvent` row carrying ``actor_id``,
   ``actor_role`` and the path-derived ``dept_id``.

2. **dept_credentials router defence-in-depth**
   (``automation-service/src/routers/dept_credentials.py``).  Even if
   the proxy were bypassed, the router re-checks the same matrix
   against the proxy-stamped ``X-Actor-*`` headers and writes its own
   ``rbac_denied`` audit row before returning HTTP 403.

The decision matrix this test pins:

  * ``admin``  → forwarded for **every** dept_id (no membership check).
  * ``system`` → forwarded for **every** dept_id (router only — the
    proxy never sees ``system`` actors because OIDC tokens never carry
    that role; it is reserved for the bot identity that calls the
    router directly without going through the proxy).
  * ``dept_admin`` → forwarded **only** when ``target_dept_id`` is in
    the actor's ``dept_ids`` set; otherwise denied.
  * ``lead`` / ``viewer`` → denied at the proxy boundary for **every**
    mutating dept-scoped credential endpoint; the router rejects them
    with 403 if reached directly.

Every denial path emits exactly one ``rbac_denied`` audit event so the
denial trail is symmetrical with the success trail.

The test deliberately avoids real Postgres / Vault / Temporal — every
collaborator is a hand-built fake.  The orchestrator
:class:`DeptCredentialService` is replaced by a recording double so we
can assert that **no mutating call leaks past a denial** (the contract
is "denied at the proxy boundary OR router defence-in-depth";
either branch must cleanly stop before any side-effect).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Iterable, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — mirrors the sibling property tests under this dir
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
_PLATFORM_ROOT = _AUTOMATION_ROOT.parent.parent
_LIB_SRC_DIRS = tuple(
    _PLATFORM_ROOT / "libs" / lib / "src"
    for lib in (
        "audit_logger",
        "vault_client",
        "db-shared",
        "http-shared",
        "auth-shared",
        "temporal-shared",
        "mcp_client",
        "messages",
        "prompts",
        "pii-shared",
        "notification",
        "observability",
        "llm-orchestrator",
    )
)
# admin-dashboard-api ships its ``proxy`` module under its own
# ``src/`` package.  When the full property suite collects both
# services together, ``src`` resolves to whichever sibling was
# imported first, which shadows the cross-service load.  We therefore
# load ``proxy.py`` directly from its file path instead of relying on
# the ``src.proxy`` package qualifier — this is the same isolation
# trick used by the multi-service property suite under
# ``platform/tests/property``.
_ADMIN_API_ROOT = _PLATFORM_ROOT / "services" / "admin-dashboard-api"
_BOOTSTRAP_DIRS: tuple[Path, ...] = (
    _AUTOMATION_ROOT,
    _AUTOMATION_SRC,
    *_LIB_SRC_DIRS,
)
for _p in (str(p) for p in _BOOTSTRAP_DIRS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from audit_logger import AuditEvent  # noqa: E402
from auth_shared import AuthContext  # noqa: E402
from auth_shared.policy import PermissionDenied, check  # noqa: E402

# AdminProxy + classifier from admin-dashboard-api.  Load the module
# directly from its file path so the import does not collide with the
# automation-service ``src`` package that shares the same name.
import importlib.util  # noqa: E402

_proxy_path = _ADMIN_API_ROOT / "src" / "proxy.py"
_proxy_spec = importlib.util.spec_from_file_location(
    "_admin_dashboard_api_proxy", _proxy_path
)
assert _proxy_spec is not None and _proxy_spec.loader is not None
_proxy_module = importlib.util.module_from_spec(_proxy_spec)
# Stash on ``sys.modules`` so dataclass-decorated globals inside the
# module can resolve their forward references during exec.
sys.modules["_admin_dashboard_api_proxy"] = _proxy_module
_proxy_spec.loader.exec_module(_proxy_module)
AdminProxy = _proxy_module.AdminProxy
PathPolicy = _proxy_module.PathPolicy
classify_admin_path = _proxy_module.classify_admin_path

# Router + dependency container from automation-service.
from automation_service.app import create_app  # noqa: E402
from routers.dept_credentials import (  # noqa: E402
    DeptCredentialEndpointDeps,
)
from services.dept_credential_service import (  # noqa: E402
    AddCredentialResult,
    DepartmentNotFoundError,
    ProbeOutcome,
    ProbeRunOutcome,
    RemoveCredentialResult,
)

# ---------------------------------------------------------------------------
# Hypothesis profile — deterministic + bounded for CI
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.differing_executors,
    ],
)

# ---------------------------------------------------------------------------
# Domain constants — closed sets the strategies sample from
# ---------------------------------------------------------------------------

#: Valid dept ids — match the ``departments.schema.json`` regex
#: (``[a-z][a-z0-9_-]{0,63}``).  We pin a small closed set so the
#: dept-membership matrix stays tractable.
_DEPT_IDS: tuple[str, ...] = ("payments", "platform", "marketing", "ops")

#: Atlassian services enumerated by :data:`VALID_SERVICES`.
_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")

#: All four OIDC-issuable roles plus ``system`` (router only).
_ALL_ROLES: tuple[str, ...] = ("admin", "dept_admin", "lead", "viewer")

#: Mutating dept-credential actions — every entry produces a path
#: classifiable by :func:`classify_admin_path` as ``required_role=
#: dept_admin`` (dept-scoped catch-all).  ``GET`` is omitted because
#: the spec only constrains the *mutating* surface.
_MUTATING_ACTIONS: tuple[tuple[str, str], ...] = (
    # (method, path-template)
    ("POST", "/admin/departments/{dept_id}/credentials/{service}"),
    ("DELETE", "/admin/departments/{dept_id}/credentials/{service}"),
    ("POST", "/admin/departments/{dept_id}/probe"),
)

#: Body shape for ``POST .../credentials/{service}`` requests.  The
#: router's structural validation runs **before** the RBAC guard for
#: read endpoints but **after** for mutating endpoints (see
#: :func:`add_or_update_credential`); this body keeps the test focused
#: on the RBAC branch by pre-satisfying the validation.
_VALID_CREDENTIAL_BODY: dict[str, str] = {
    "url": "https://example.atlassian.net",
    "username": "bot@example.com",
    "personal_token": "test-token-redacted",
}

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _role_strategy() -> st.SearchStrategy[str]:
    return st.sampled_from(_ALL_ROLES)


def _dept_id_strategy() -> st.SearchStrategy[str]:
    return st.sampled_from(_DEPT_IDS)


def _service_strategy() -> st.SearchStrategy[str]:
    return st.sampled_from(_SERVICES)


def _viewer_dept_ids_strategy() -> st.SearchStrategy[frozenset[str]]:
    """Subset of :data:`_DEPT_IDS` (possibly empty) representing the
    actor's dept memberships.
    """

    return st.sets(st.sampled_from(_DEPT_IDS), max_size=len(_DEPT_IDS)).map(
        frozenset
    )


def _mutating_action_strategy() -> st.SearchStrategy[tuple[str, str]]:
    return st.sampled_from(_MUTATING_ACTIONS)


# ---------------------------------------------------------------------------
# In-memory fakes shared across both boundary suites
# ---------------------------------------------------------------------------


@dataclass
class _ListAuditSink:
    """List-backed audit sink usable as both ``AdminProxy._AuditSink``
    and ``AuditLogger`` (duck-typed in :class:`DeptCredentialEndpointDeps`).
    """

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _ForwardingTracker:
    """Records every successful forward through the proxy."""

    requests: list[httpx.Request] = field(default_factory=list)


@dataclass
class _RecordingService:
    """Stand-in :class:`DeptCredentialService` for the router tests.

    Returns canned success responses for ``admin`` / ``dept_admin``
    callers so we can assert that *only* the privileged calls reach
    the orchestrator.  Sub-privileged callers must be cut off at the
    router's RBAC guard before any of these methods is invoked.
    """

    add_calls: list[Any] = field(default_factory=list)
    remove_calls: list[Any] = field(default_factory=list)
    probe_calls: list[Any] = field(default_factory=list)

    async def add_or_update(
        self, request: Any, *, actor_id: str, actor_role: str
    ) -> AddCredentialResult:
        self.add_calls.append((request, actor_id, actor_role))
        return AddCredentialResult(
            dept_id=request.dept_id,
            service=request.service,
            account_id="account-123",
            last_probe_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            vault_path=f"vault:atlassian/{request.dept_id}/{request.service}",
            outcome="created",
        )

    async def remove(
        self,
        *,
        dept_id: str,
        service: str,
        actor_id: str,
        actor_role: str,
    ) -> RemoveCredentialResult:
        self.remove_calls.append(
            {
                "dept_id": dept_id,
                "service": service,
                "actor_id": actor_id,
                "actor_role": actor_role,
            }
        )
        return RemoveCredentialResult(
            dept_id=dept_id, service=service, existed=True
        )

    async def probe(
        self,
        *,
        dept_id: str,
        service: str | None,
        actor_id: str,
        actor_role: str,
    ) -> ProbeRunOutcome:
        self.probe_calls.append(
            {
                "dept_id": dept_id,
                "service": service,
                "actor_id": actor_id,
                "actor_role": actor_role,
            }
        )
        results = (
            (
                ProbeOutcome(
                    service=service,  # type: ignore[arg-type]
                    status="ok",
                    account_id="account-123",
                ),
            )
            if service is not None
            else tuple(
                ProbeOutcome(svc, "ok", account_id="account-123")  # type: ignore[arg-type]
                for svc in _SERVICES
            )
        )
        return ProbeRunOutcome(
            dept_id=dept_id,
            results=results,
            probed_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )


async def _empty_connection_factory() -> Any:
    """Connection factory the router tests should never invoke.

    Mutating endpoints delegate the SQL session to the orchestrator
    fake; the read endpoints are not exercised in this RBAC-focused
    suite.
    """

    raise AssertionError(
        "connection_factory must not be invoked in dept_credential RBAC tests"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_actor(
    role: str, dept_ids: Iterable[str], *, actor_id: str = "user-test"
) -> AuthContext:
    """Construct an :class:`AuthContext` for the proxy tests."""

    return AuthContext(
        actor_id=actor_id,
        actor_role=role,  # type: ignore[arg-type]
        dept_ids=frozenset(dept_ids),
        raw_claims={"sub": actor_id, "role": role},
    )


def _expected_proxy_decision(
    role: str, viewer_dept_ids: frozenset[str], target_dept_id: str
) -> str:
    """Return ``"forward"`` or ``"deny"`` for the proxy boundary.

    Mirrors the policy in :data:`auth_shared.policy._ROLES_THAT_SATISFY`
    for ``required_role="dept_admin"`` (the role assigned by
    :func:`classify_admin_path` to every dept-scoped sub-path):

      * ``admin``       -> always forwarded.
      * ``dept_admin``  -> forwarded only when the target dept is in
                           the actor's ``dept_ids``.
      * ``lead`` /``viewer`` -> denied (not in
                           :data:`_ROLES_THAT_SATISFY['dept_admin']`).
    """

    if role == "admin":
        return "forward"
    if role == "dept_admin":
        return "forward" if target_dept_id in viewer_dept_ids else "deny"
    # ``lead`` / ``viewer`` — denied for the dept_admin policy.
    return "deny"


def _build_router_client() -> tuple[
    TestClient, _RecordingService, _ListAuditSink
]:
    """Return a TestClient wired with recording fakes for the router."""

    app = create_app()
    service = _RecordingService()
    audit = _ListAuditSink()
    app.state.dept_credentials = DeptCredentialEndpointDeps(
        service=service,  # type: ignore[arg-type]
        connection_factory=_empty_connection_factory,
        audit_logger=audit,  # type: ignore[arg-type]
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    return TestClient(app), service, audit


def _format_path(template: str, *, dept_id: str, service: str) -> str:
    return template.format(dept_id=dept_id, service=service)


def _request_kwargs_for(
    method: str, path: str
) -> dict[str, Any]:
    """Build :class:`TestClient` kwargs for the given mutating action.

    POST credential routes need a JSON body; DELETE / probe do not.
    """

    if method == "POST" and path.endswith("/credentials/{service}".format(service="").rstrip("/")):
        # Should not match — keep deterministic by checking suffix below.
        pass
    if method == "POST" and "/credentials/" in path:
        return {"json": _VALID_CREDENTIAL_BODY}
    return {}


# ---------------------------------------------------------------------------
# Property A — AdminProxy boundary RBAC determinism
# ---------------------------------------------------------------------------


class TestProxyBoundaryRbacDeterminism:
    """The AdminProxy classifier + check() decision is deterministic
    over (role, viewer_dept_ids, target_dept_id, action)."""

    @given(
        role=_role_strategy(),
        viewer_dept_ids=_viewer_dept_ids_strategy(),
        target_dept_id=_dept_id_strategy(),
        action=_mutating_action_strategy(),
        service=_service_strategy(),
    )
    @_PROFILE
    def test_classify_then_check_matches_decision_matrix(
        self,
        role: str,
        viewer_dept_ids: frozenset[str],
        target_dept_id: str,
        action: tuple[str, str],
        service: str,
    ) -> None:
        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )

        # 1. The classifier resolves every mutating dept-credential
        #    path to ``(dept_admin, target_dept_id)``.  This is the
        #    spec contract — ``lead`` / ``viewer`` are denied because
        #    they do not satisfy the ``dept_admin`` policy, and
        #    ``dept_admin`` itself is gated on dept membership.
        policy = classify_admin_path(method, path)
        assert policy.required_role == "dept_admin", (
            f"path {path!r} must be classified as dept-scoped, "
            f"got {policy!r}"
        )
        assert policy.dept_id == target_dept_id, (
            f"path {path!r} must surface dept_id={target_dept_id!r}, "
            f"got {policy.dept_id!r}"
        )

        # 2. The check() outcome must agree with the spec's decision
        #    matrix for every (role, viewer_dept_ids) pair.
        actor = _build_actor(role, viewer_dept_ids)
        expected = _expected_proxy_decision(
            role, viewer_dept_ids, target_dept_id
        )

        if expected == "forward":
            # Should not raise.
            check(actor, policy.required_role, policy.dept_id)
        else:
            with pytest.raises(PermissionDenied):
                check(actor, policy.required_role, policy.dept_id)

    @pytest.mark.asyncio
    @given(
        role=st.sampled_from(("admin", "dept_admin")),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
        action=_mutating_action_strategy(),
    )
    @_PROFILE
    async def test_admin_and_dept_admin_member_are_forwarded(
        self,
        role: str,
        target_dept_id: str,
        service: str,
        action: tuple[str, str],
    ) -> None:
        """``admin`` (always) and ``dept_admin`` (own dept) are
        forwarded with the proxy stamping ``X-Actor-*`` headers."""

        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )
        # ``dept_admin`` must include ``target_dept_id`` in dept_ids.
        dept_ids: tuple[str, ...] = (
            (target_dept_id,) if role == "dept_admin" else ()
        )
        actor = _build_actor(role, dept_ids, actor_id=f"actor-{role}")

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=b'{"ok":true}')

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        sink = _ListAuditSink()
        proxy = AdminProxy(
            automation_service_url="http://automation-service:8080",
            http_client=client,
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method=method,
                path=path,
                body=b"{}",
                headers={"content-type": "application/json"},
                actor=actor,
            )
        finally:
            await client.aclose()

        # The proxy must forward with the documented status code and
        # never emit ``rbac_denied`` on a successful forward.
        assert response.status_code == 200
        assert all(e.action != "rbac_denied" for e in sink.events)
        assert len(captured) == 1, "exactly one upstream request expected"

        forwarded = captured[0]
        assert forwarded.method == method
        # The proxy stamps the actor headers so automation-service can
        # write its audit rows under the human admin's identity.
        assert (
            forwarded.headers.get("x-actor-id") == actor.actor_id
        ), "X-Actor-Id must be stamped"
        assert forwarded.headers.get("x-actor-role") == role, (
            "X-Actor-Role must echo the actor's role"
        )
        assert forwarded.headers.get("x-actor-dept-id") == target_dept_id, (
            "X-Actor-Dept-Id must carry the path-derived dept_id"
        )

    @pytest.mark.asyncio
    @given(
        role=st.sampled_from(("lead", "viewer")),
        viewer_dept_ids=_viewer_dept_ids_strategy(),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
        action=_mutating_action_strategy(),
    )
    @_PROFILE
    async def test_lead_and_viewer_are_denied_at_proxy(
        self,
        role: str,
        viewer_dept_ids: frozenset[str],
        target_dept_id: str,
        service: str,
        action: tuple[str, str],
    ) -> None:
        """``lead`` / ``viewer`` are denied for **every** mutating
        dept-credential endpoint at the proxy boundary, regardless of
        whether the actor is a member of the target dept.  The denial
        emits one ``rbac_denied`` audit row and the upstream is
        **never** contacted.
        """

        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )
        actor = _build_actor(role, viewer_dept_ids, actor_id=f"actor-{role}")

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=b'{"unreachable":true}')

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        sink = _ListAuditSink()
        proxy = AdminProxy(
            automation_service_url="http://automation-service:8080",
            http_client=client,
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method=method,
                path=path,
                body=b"{}",
                headers={"content-type": "application/json"},
                actor=actor,
            )
        finally:
            await client.aclose()

        assert response.status_code == 403, (
            f"role={role!r} must be denied with HTTP 403 for "
            f"{method} {path}"
        )
        # Upstream must never be contacted on a denial.
        assert captured == []
        # Exactly one ``rbac_denied`` audit row carrying the actor +
        # target dept; every denial is auditable.
        denials = [e for e in sink.events if e.action == "rbac_denied"]
        assert len(denials) == 1
        denial = denials[0]
        assert denial.actor_id == actor.actor_id
        assert denial.actor_role == role
        assert denial.dept_id == target_dept_id
        assert denial.result == "denied"

    @pytest.mark.asyncio
    @given(
        viewer_dept_ids=_viewer_dept_ids_strategy(),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
        action=_mutating_action_strategy(),
    )
    @_PROFILE
    async def test_dept_admin_outside_dept_is_denied(
        self,
        viewer_dept_ids: frozenset[str],
        target_dept_id: str,
        service: str,
        action: tuple[str, str],
    ) -> None:
        """``dept_admin`` is denied when ``target_dept_id`` is NOT in
        the actor's dept_ids — symmetrical to the success path proven
        in :meth:`test_admin_and_dept_admin_member_are_forwarded`.
        """

        # Only exercise the denial branch.
        if target_dept_id in viewer_dept_ids:
            return  # falls into the "forward" branch covered above

        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )
        actor = _build_actor(
            "dept_admin", viewer_dept_ids, actor_id="actor-dept_admin"
        )

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, content=b'{"unreachable":true}')

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        sink = _ListAuditSink()
        proxy = AdminProxy(
            automation_service_url="http://automation-service:8080",
            http_client=client,
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method=method,
                path=path,
                body=b"{}",
                headers={"content-type": "application/json"},
                actor=actor,
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        denials = [e for e in sink.events if e.action == "rbac_denied"]
        assert len(denials) == 1
        denial = denials[0]
        assert denial.actor_role == "dept_admin"
        assert denial.dept_id == target_dept_id
        assert denial.result == "denied"


# ---------------------------------------------------------------------------
# Router defence-in-depth RBAC determinism
# ---------------------------------------------------------------------------


class TestRouterDefenseInDepthRbacDeterminism:
    """The dept_credentials router enforces the same matrix on the
    proxy-stamped ``X-Actor-*`` headers, so a bypass of the proxy
    cannot smuggle a ``lead`` / ``viewer`` / cross-dept ``dept_admin``
    request to the orchestrator."""

    @given(
        role=st.sampled_from(("admin", "dept_admin", "system")),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
    )
    @_PROFILE
    def test_admin_system_and_member_dept_admin_reach_orchestrator(
        self,
        role: str,
        target_dept_id: str,
        service: str,
    ) -> None:
        client, fake_service, audit = _build_router_client()

        # ``dept_admin`` must carry a matching X-Actor-Dept-Id; the
        # proxy guarantees this for forwarded requests.
        dept_header = (
            target_dept_id if role == "dept_admin" else target_dept_id
        )
        headers = {
            "X-Actor-Role": role,
            "X-Actor-Id": f"actor-{role}",
            "X-Actor-Dept-Id": dept_header,
        }

        # POST add/update — orchestrator returns AddCredentialResult.
        resp_post = client.post(
            f"/admin/departments/{target_dept_id}/credentials/{service}",
            json=_VALID_CREDENTIAL_BODY,
            headers=headers,
        )
        assert resp_post.status_code == 200, resp_post.text
        assert len(fake_service.add_calls) == 1
        # The router forwards the actor's role to the orchestrator
        # (translated via ``_audit_role`` for ``lead``/``viewer``;
        # ``admin`` / ``dept_admin`` / ``system`` pass through).
        _, _, forwarded_role = fake_service.add_calls[-1]
        assert forwarded_role == role

        # DELETE remove.
        resp_delete = client.delete(
            f"/admin/departments/{target_dept_id}/credentials/{service}",
            headers=headers,
        )
        assert resp_delete.status_code == 200, resp_delete.text
        assert len(fake_service.remove_calls) == 1
        assert fake_service.remove_calls[-1]["dept_id"] == target_dept_id

        # POST probe.
        resp_probe = client.post(
            f"/admin/departments/{target_dept_id}/probe?service={service}",
            headers=headers,
        )
        assert resp_probe.status_code == 200, resp_probe.text
        assert len(fake_service.probe_calls) == 1
        assert fake_service.probe_calls[-1]["service"] == service

        # No ``rbac_denied`` audit on the success path.
        denials = [e for e in audit.events if e.action == "rbac_denied"]
        assert denials == [], (
            f"unexpected rbac_denied audit on success path: "
            f"{[e.payload for e in denials]}"
        )

    @given(
        role=st.sampled_from(("lead", "viewer")),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
        action=_mutating_action_strategy(),
    )
    @_PROFILE
    def test_lead_and_viewer_denied_at_router(
        self,
        role: str,
        target_dept_id: str,
        service: str,
        action: tuple[str, str],
    ) -> None:
        client, fake_service, audit = _build_router_client()
        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )
        headers = {
            "X-Actor-Role": role,
            "X-Actor-Id": f"actor-{role}",
            "X-Actor-Dept-Id": target_dept_id,
        }

        if method == "POST" and "/credentials/" in path:
            resp = client.post(
                path, json=_VALID_CREDENTIAL_BODY, headers=headers
            )
        elif method == "DELETE":
            resp = client.delete(path, headers=headers)
        else:  # POST .../probe
            resp = client.post(path, headers=headers)

        # 403 + no orchestrator side-effect.
        assert resp.status_code == 403, resp.text
        assert fake_service.add_calls == []
        assert fake_service.remove_calls == []
        assert fake_service.probe_calls == []

        # Router writes a single ``rbac_denied`` audit row (best-effort
        # but always emitted on the synchronous path with our fake).
        denials = [e for e in audit.events if e.action == "rbac_denied"]
        assert len(denials) >= 1, (
            "router must emit at least one rbac_denied audit row for "
            f"role={role!r} on {method} {path}"
        )
        # Every denial must point at the rejected actor + target dept
        # so the audit trail can be filtered.
        denial = denials[-1]
        assert denial.actor_id == f"actor-{role}"
        assert denial.dept_id == target_dept_id
        assert denial.result == "denied"

    @given(
        viewer_dept_id=_dept_id_strategy(),
        target_dept_id=_dept_id_strategy(),
        service=_service_strategy(),
        action=_mutating_action_strategy(),
    )
    @_PROFILE
    def test_dept_admin_scope_mismatch_denied_at_router(
        self,
        viewer_dept_id: str,
        target_dept_id: str,
        service: str,
        action: tuple[str, str],
    ) -> None:
        # Only exercise the mismatch branch.
        if viewer_dept_id == target_dept_id:
            return

        client, fake_service, audit = _build_router_client()
        method, path_template = action
        path = _format_path(
            path_template, dept_id=target_dept_id, service=service
        )
        # The proxy would have stamped the actor's own dept_id; the
        # router compares against the URL-derived target.
        headers = {
            "X-Actor-Role": "dept_admin",
            "X-Actor-Id": "actor-dept_admin",
            "X-Actor-Dept-Id": viewer_dept_id,
        }

        if method == "POST" and "/credentials/" in path:
            resp = client.post(
                path, json=_VALID_CREDENTIAL_BODY, headers=headers
            )
        elif method == "DELETE":
            resp = client.delete(path, headers=headers)
        else:
            resp = client.post(path, headers=headers)

        assert resp.status_code == 403, resp.text
        # Orchestrator must not be invoked on a scope mismatch.
        assert fake_service.add_calls == []
        assert fake_service.remove_calls == []
        assert fake_service.probe_calls == []

        denials = [e for e in audit.events if e.action == "rbac_denied"]
        assert len(denials) >= 1
        denial = denials[-1]
        assert denial.actor_role == "dept_admin"
        assert denial.dept_id == target_dept_id

    def test_admin_bypasses_dept_scope_for_any_dept(self) -> None:
        """Concrete regression: ``admin`` reaches the orchestrator
        even when the proxy would have omitted ``X-Actor-Dept-Id``.

        Proves the ``admin`` shortcut in
        :func:`_enforce_dept_scope` is symmetric to the proxy's
        global-admin admittance — a missing dept header must not
        accidentally promote ``admin`` to a denial.
        """

        client, fake_service, _audit = _build_router_client()
        for target_dept_id in _DEPT_IDS:
            for service in _SERVICES:
                resp = client.post(
                    f"/admin/departments/{target_dept_id}/credentials/{service}",
                    json=_VALID_CREDENTIAL_BODY,
                    headers={
                        "X-Actor-Role": "admin",
                        "X-Actor-Id": "alice",
                        # Intentionally omit X-Actor-Dept-Id.
                    },
                )
                assert resp.status_code == 200, resp.text
        assert len(fake_service.add_calls) == len(_DEPT_IDS) * len(_SERVICES)
