"""Unit tests for ``src.proxy.AdminProxy``.

Covers the proxy behaviour matrix:

* Path classification (``classify_admin_path``) maps every documented
  ``/admin/*`` route to the correct ``(required_role, dept_id)``
  shape.
* RBAC denial returns HTTP 403 + emits a single ``rbac_denied`` audit
  row carrying ``actor_id`` / ``actor_role`` / ``dept_id``.
* ``dept_admin`` reaching outside its own ``dept_ids`` is denied; the
  same actor reaching its own dept is allowed.
* Global actions (``/admin/departments``, ``/admin/probe-artifacts``,
  ``/admin/ssh-runners``, ``/admin/prompts/global``,
  ``/admin/departments/{id}/disable``) admit only ``role=admin``.
* Successful forwards reach automation-service with hop-by-hop /
  ``Authorization`` / ``Cookie`` headers stripped, and with
  ``X-Actor-*`` headers stamped by the proxy.
* Inbound ``X-Actor-*`` headers are dropped before forwarding so a
  malicious caller cannot spoof admin identity.

The tests use an in-process :class:`httpx.MockTransport` for the
upstream channel and a list-backed audit sink for assertions; no
network, no Postgres, no FastAPI.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

# Bootstrap sys.path so ``import src.proxy`` resolves (mirrors
# test_main.py / test_require_admin.py).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for lib_dir in (
    _WORKSPACE_ROOT / "libs" / "auth-shared" / "src",
    _WORKSPACE_ROOT / "libs" / "audit_logger" / "src",
):
    if lib_dir.is_dir() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

from audit_logger import AuditEvent  # noqa: E402
from auth_shared import AuthContext  # noqa: E402

from src.proxy import (  # noqa: E402
    AdminProxy,
    PathPolicy,
    classify_admin_path,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _ListAuditSink:
    """List-backed audit sink for unit tests.

    Records every event written via ``write(event)`` so assertions can
    inspect the wire shape. The sink never raises - failures are
    caught by the proxy's best-effort wrapper, but in tests we only
    care about successful writes.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RaisingAuditSink:
    """Audit sink that raises on every write.

    Used to verify that audit-write failures do NOT mask the
    underlying HTTP 403 fail-soft semantics.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def write(self, event: AuditEvent) -> None:
        self.calls += 1
        raise RuntimeError("simulated audit DB outage")


def _make_actor(
    *,
    actor_id: str = "user-123",
    role: str = "admin",
    dept_ids: tuple[str, ...] = (),
) -> AuthContext:
    """Build an :class:`AuthContext` for tests."""

    return AuthContext(
        actor_id=actor_id,
        actor_role=role,  # type: ignore[arg-type]
        dept_ids=frozenset(dept_ids),
        raw_claims={"sub": actor_id, "role": role},
    )


def _make_proxy(
    *,
    handler,
    audit_sink=None,
) -> tuple[AdminProxy, httpx.AsyncClient, list[httpx.Request]]:
    """Wire an :class:`AdminProxy` against a mock-transport upstream.

    Returns the proxy, the underlying client (so the caller can close
    it) and a list that the handler can append requests to (mirrors
    the convention used by ``test_health_probe.py``).
    """

    captured_requests: list[httpx.Request] = []

    def _wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return handler(request)

    transport = httpx.MockTransport(_wrapped_handler)
    client = httpx.AsyncClient(transport=transport)

    sink = audit_sink if audit_sink is not None else _ListAuditSink()
    proxy = AdminProxy(
        automation_service_url="http://automation-service:8080",
        http_client=client,
        audit_sink=sink,
    )
    return proxy, client, captured_requests


# ---------------------------------------------------------------------------
# classify_admin_path - pure path classification
# ---------------------------------------------------------------------------


class TestClassifyAdminPath:
    """Path classifier matrix (design.md `automation-service HTTP API`)."""

    def test_create_department_is_admin_only(self) -> None:
        policy = classify_admin_path("POST", "/admin/departments")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_setup_wizard_is_admin_only(self) -> None:
        policy = classify_admin_path("POST", "/admin/departments/wizard")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_credentials_rotate_is_dept_admin_self_service(self) -> None:
        policy = classify_admin_path(
            "POST", "/admin/departments/payments/credentials/rotate"
        )
        assert policy == PathPolicy(
            required_role="dept_admin", dept_id="payments"
        )

    def test_credentials_rotate_with_trailing_slash(self) -> None:
        policy = classify_admin_path(
            "POST", "/admin/departments/payments/credentials/rotate/"
        )
        assert policy == PathPolicy(
            required_role="dept_admin", dept_id="payments"
        )

    def test_disable_department_is_admin_only_global(self) -> None:
        # Even though the path carries a dept_id, disabling is a
        # platform-level lifecycle action in the admin-only bucket.
        policy = classify_admin_path(
            "POST", "/admin/departments/payments/disable"
        )
        assert policy == PathPolicy(required_role="admin", dept_id="payments")

    def test_other_dept_subpath_is_dept_admin_scoped(self) -> None:
        # A read-only or write subpath under /admin/departments/<id>/
        # admits dept_admin for the matching dept.
        policy = classify_admin_path(
            "GET", "/admin/departments/payments/audit"
        )
        assert policy == PathPolicy(
            required_role="dept_admin", dept_id="payments"
        )

    def test_probe_artifacts_is_admin_only(self) -> None:
        policy = classify_admin_path("GET", "/admin/probe-artifacts")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

        policy = classify_admin_path(
            "DELETE", "/admin/probe-artifacts/abc-123"
        )
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_ssh_runners_is_admin_only(self) -> None:
        # SSH runner config is explicitly admin-only.
        policy = classify_admin_path("POST", "/admin/ssh-runners")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

        policy = classify_admin_path(
            "PATCH", "/admin/ssh-runners/runner-1"
        )
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_global_prompt_is_admin_only(self) -> None:
        # Global prompt change is admin-only.
        policy = classify_admin_path("PUT", "/admin/prompts/global")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

        policy = classify_admin_path("GET", "/admin/prompts/global/v3")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_unknown_admin_path_defaults_to_admin(self) -> None:
        # Fail-closed: a route the classifier does not recognise is
        # still admin-only.
        policy = classify_admin_path("GET", "/admin/something-new")
        assert policy == PathPolicy(required_role="admin", dept_id=None)

    def test_non_admin_path_raises_value_error(self) -> None:
        # The proxy is scoped to /admin/* - non-admin paths must
        # never reach the classifier.
        with pytest.raises(ValueError, match="/admin/\\*"):
            classify_admin_path("GET", "/healthz")

    def test_query_string_is_ignored(self) -> None:
        # FastAPI strips query strings before the path reaches the
        # classifier, but the helper should be defensive too.
        policy = classify_admin_path(
            "GET", "/admin/departments?include=disabled"
        )
        assert policy == PathPolicy(required_role="admin", dept_id=None)


# ---------------------------------------------------------------------------
# Successful forward - happy path
# ---------------------------------------------------------------------------


class TestSuccessfulForward:
    """The proxy reaches automation-service and returns its response verbatim."""

    @pytest.mark.asyncio
    async def test_admin_can_create_department(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                201,
                content=b'{"id":"payments","mode":"active"}',
                headers={"content-type": "application/json"},
            )

        proxy, client, captured = _make_proxy(handler=handler)
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b'{"id":"payments"}',
                headers={"content-type": "application/json"},
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        assert response.status_code == 201
        assert response.body == b'{"id":"payments","mode":"active"}'
        assert response.headers["content-type"] == "application/json"
        assert len(captured) == 1
        assert captured[0].url == httpx.URL(
            "http://automation-service:8080/admin/departments"
        )
        assert captured[0].method == "POST"
        # Body forwarded byte-for-byte.
        assert captured[0].content == b'{"id":"payments"}'

    @pytest.mark.asyncio
    async def test_dept_admin_can_rotate_own_credentials(self) -> None:
        # Self-service rotation.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, content=b'{"status":"queued"}')

        proxy, client, captured = _make_proxy(handler=handler)
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments/payments/credentials/rotate",
                body=b"",
                headers={},
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 202
        assert len(captured) == 1
        assert "/admin/departments/payments/credentials/rotate" in str(
            captured[0].url
        )

    @pytest.mark.asyncio
    async def test_admin_always_satisfies_dept_scoped_route(self) -> None:
        # ``admin`` does not need ``dept_ids`` membership.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b'{}')

        proxy, client, _ = _make_proxy(handler=handler)
        try:
            response = await proxy.forward(
                method="GET",
                path="/admin/departments/payments/audit",
                body=b"",
                headers={},
                actor=_make_actor(role="admin", dept_ids=()),
            )
        finally:
            await client.aclose()

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_query_string_is_forwarded_verbatim(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        proxy, client, captured = _make_proxy(handler=handler)
        try:
            await proxy.forward(
                method="GET",
                path="/admin/probe-artifacts",
                body=b"",
                headers={},
                actor=_make_actor(role="admin"),
                query_string="state=partial_orphan&limit=50",
            )
        finally:
            await client.aclose()

        assert captured[0].url.query == b"state=partial_orphan&limit=50"


# ---------------------------------------------------------------------------
# RBAC denial - HTTP 403 + audit row
# ---------------------------------------------------------------------------


class TestRbacDenial:
    """RBAC enforcement matrix."""

    @pytest.mark.asyncio
    async def test_dept_admin_cannot_create_new_department(self) -> None:
        # Global admin actions are admin-only.
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={},
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        # Upstream MUST NOT be reached on a denial.
        assert captured == []
        # Single audit row written.
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.action == "rbac_denied"
        assert event.result == "denied"
        assert event.actor_role == "dept_admin"
        assert event.actor_id == "user-123"
        assert event.dept_id is None
        assert "POST" in event.resource
        assert "/admin/departments" in event.resource
        assert isinstance(event.timestamp, datetime)
        assert event.payload is not None
        assert event.payload["required_role"] == "admin"
        assert event.payload["actor_role"] == "dept_admin"

    @pytest.mark.asyncio
    async def test_dept_admin_cannot_disable_own_department(self) -> None:
        # Disabling is admin-only - even the
        # dept's own admin cannot retire it.
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments/payments/disable",
                body=b"",
                headers={},
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        assert len(sink.events) == 1
        event = sink.events[0]
        # The dept_id is still surfaced on the audit row even though
        # the policy is admin-only - that lets the admin see who
        # tried to disable what.
        assert event.dept_id == "payments"

    @pytest.mark.asyncio
    async def test_dept_admin_cannot_reach_other_dept(self) -> None:
        # dept_admin is scoped to own dept_ids.
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments/marketing/credentials/rotate",
                body=b"",
                headers={},
                # Actor is dept_admin for ``payments`` only.
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        assert len(sink.events) == 1
        event = sink.events[0]
        assert event.dept_id == "marketing"
        assert event.payload is not None
        assert "marketing" not in event.payload["actor_dept_ids"]

    @pytest.mark.asyncio
    async def test_viewer_cannot_reach_admin_endpoints(self) -> None:
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="GET",
                path="/admin/probe-artifacts",
                body=b"",
                headers={},
                actor=_make_actor(role="viewer"),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        assert sink.events[0].actor_role == "viewer"

    @pytest.mark.asyncio
    async def test_lead_cannot_reach_admin_endpoints(self) -> None:
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/ssh-runners",
                body=b"{}",
                headers={},
                actor=_make_actor(role="lead"),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_mask_403(self) -> None:
        # A transient audit-DB outage MUST NOT
        # convert the 403 into a 500. Best-effort audit emit.
        sink = _RaisingAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={},
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        # The sink was called; it raised; the proxy still returned 403.
        assert sink.calls == 1

    @pytest.mark.asyncio
    async def test_global_prompt_change_admin_only(self) -> None:
        # Global prompt change is admin-only.
        sink = _ListAuditSink()
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(599),
            audit_sink=sink,
        )
        try:
            response = await proxy.forward(
                method="PUT",
                path="/admin/prompts/global",
                body=b'{"prompt":"..."}',
                headers={},
                actor=_make_actor(role="dept_admin", dept_ids=("payments",)),
            )
        finally:
            await client.aclose()

        assert response.status_code == 403
        assert captured == []
        assert sink.events[0].payload["required_role"] == "admin"


# ---------------------------------------------------------------------------
# Header filtering
# ---------------------------------------------------------------------------


class TestHeaderFiltering:
    """Hop-by-hop / Authorization / X-Actor headers are stripped."""

    @pytest.mark.asyncio
    async def test_authorization_header_is_stripped(self) -> None:
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(200),
        )
        try:
            await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={
                    "authorization": "Bearer secret-token-12345",
                    "content-type": "application/json",
                },
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        forwarded = captured[0]
        assert "authorization" not in {k.lower() for k in forwarded.headers}
        # Application headers survive.
        assert forwarded.headers.get("content-type") == "application/json"

    @pytest.mark.asyncio
    async def test_cookie_header_is_stripped(self) -> None:
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(200),
        )
        try:
            await proxy.forward(
                method="GET",
                path="/admin/probe-artifacts",
                body=b"",
                headers={"cookie": "session=abc123"},
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        forwarded = captured[0]
        assert "cookie" not in {k.lower() for k in forwarded.headers}

    @pytest.mark.asyncio
    async def test_inbound_x_actor_headers_are_overwritten(self) -> None:
        # Spoof attempt: a malicious caller sends X-Actor-Role: admin.
        # The proxy MUST drop it before forwarding and stamp the
        # validated identity instead.
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(200),
        )
        try:
            await proxy.forward(
                method="POST",
                path="/admin/departments/payments/credentials/rotate",
                body=b"",
                headers={
                    "x-actor-role": "admin",  # spoofed
                    "x-actor-id": "evil-user",
                    "x-actor-dept-id": "marketing",
                },
                actor=_make_actor(
                    actor_id="real-user",
                    role="dept_admin",
                    dept_ids=("payments",),
                ),
            )
        finally:
            await client.aclose()

        forwarded = captured[0]
        assert forwarded.headers["x-actor-id"] == "real-user"
        assert forwarded.headers["x-actor-role"] == "dept_admin"
        assert forwarded.headers["x-actor-dept-id"] == "payments"

    @pytest.mark.asyncio
    async def test_hop_by_hop_headers_are_stripped(self) -> None:
        # ``httpx`` adds its own transport-level ``connection`` and
        # ``host`` headers, so we cannot simply assert the absence of
        # those keys on the upstream side. Instead we send unusual
        # values for the hop-by-hop headers and verify the upstream
        # did *not* see those values - i.e. the proxy stripped them
        # before httpx re-added its own.
        proxy, client, captured = _make_proxy(
            handler=lambda req: httpx.Response(200),
        )
        try:
            await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={
                    "connection": "close",
                    "te": "trailers",
                    "host": "evil.example.com",
                    "content-type": "application/json",
                },
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        forwarded = captured[0]
        # ``connection: close`` would have closed the proxy's keep-alive
        # connection; httpx's default is ``keep-alive``. If our filter
        # leaked the inbound value, the upstream would see ``close``.
        assert forwarded.headers.get("connection") != "close"
        # ``te: trailers`` is hop-by-hop; httpx does not re-add it.
        assert "te" not in {k.lower() for k in forwarded.headers}
        # ``host`` should never be the spoofed value - httpx rebuilds
        # this from the upstream URL.
        assert forwarded.headers.get("host") != "evil.example.com"
        # Application headers survive.
        assert forwarded.headers.get("content-type") == "application/json"


# ---------------------------------------------------------------------------
# Upstream error mapping
# ---------------------------------------------------------------------------


class TestUpstreamErrors:
    """Network / timeout failures map to 502 / 504 without leaking detail."""

    @pytest.mark.asyncio
    async def test_upstream_timeout_returns_504(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("simulated timeout")

        proxy, client, _ = _make_proxy(handler=handler)
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={},
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        assert response.status_code == 504
        assert b"timeout" in response.body

    @pytest.mark.asyncio
    async def test_upstream_connect_error_returns_502(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        proxy, client, _ = _make_proxy(handler=handler)
        try:
            response = await proxy.forward(
                method="POST",
                path="/admin/departments",
                body=b"{}",
                headers={},
                actor=_make_actor(role="admin"),
            )
        finally:
            await client.aclose()

        assert response.status_code == 502


# ---------------------------------------------------------------------------
# Constructor argument validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    """Defensive guards on AdminProxy.__init__."""

    def test_empty_url_rejected(self) -> None:
        client = httpx.AsyncClient()
        with pytest.raises(ValueError, match="must not be empty"):
            AdminProxy(
                automation_service_url="",
                http_client=client,
                audit_sink=_ListAuditSink(),
            )

    def test_non_http_url_rejected(self) -> None:
        client = httpx.AsyncClient()
        with pytest.raises(ValueError, match="http://"):
            AdminProxy(
                automation_service_url="ftp://automation-service",
                http_client=client,
                audit_sink=_ListAuditSink(),
            )

    def test_trailing_slash_is_normalised(self) -> None:
        client = httpx.AsyncClient()
        proxy = AdminProxy(
            automation_service_url="http://automation-service:8080/",
            http_client=client,
            audit_sink=_ListAuditSink(),
        )
        # Internal state is private; the easier assertion is that a
        # forwarded URL doesn't have a double slash.
        assert proxy._upstream == "http://automation-service:8080"  # noqa: SLF001
