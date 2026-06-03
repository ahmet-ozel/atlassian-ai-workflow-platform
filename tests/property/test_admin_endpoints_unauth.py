"""Admin endpoints reject unauthenticated and non-admin calls.



Behavior statement
------------------
For *every* Managed_Service in ``config/services.manifest.json`` and
*every* admin lifecycle endpoint exposed by
``services/admin-dashboard-api/src/routers/services_lifecycle.py``:

* A request **without** an ``Authorization`` header MUST receive
 ``401 Unauthorized``.
* A request carrying a *valid* token whose claims do **not** include
 ``"admin"`` (e.g. ``groups=["user"]``) MUST receive ``403 Forbidden``.

These checks ensure no anonymous probe can flip the state of any
Managed_Service and that ordinary authenticated users cannot cross
over into the admin surface.

Implementation notes
--------------------
* The FastAPI app is constructed *once* per test session by mounting
 the lifecycle router onto a bare:class:`fastapi.FastAPI` instance.
 We do not run the real ``src/main.py`` lifespan because it expects
 Postgres, Vault and Temporal to be reachable; instead we override
 ``get_lifecycle_service`` to return ``None`` so any code path that
 bypassed auth (none should, but defence in depth) would crash
 loudly rather than silently 200.
* The dev-mode:class:`OIDCValidator` returns canned admin claims for
 *any* non-empty token, so it cannot exercise the 403 branch. We
 override ``get_validator`` with a stub that returns
 ``{"sub": "user-1", "groups": ["user"]}`` so the bearer token
 validates but ``require_admin`` raises 403.
* Modern ``httpx`` (≥0.28) removed ``AsyncClient(app=...)``; we use
 the documented:class:`httpx.ASGITransport` shim instead.
* Endpoint coverage is enforced by:func:`pytest.mark.parametrize`
 over the eight router endpoints, while Hypothesis ``sampled_from``
 picks the manifest service name within each parametrized run.
 This guarantees every endpoint is exercised on every CI run while
 still letting Hypothesis search the service-name dimension.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Workspace + sys.path bootstrapping
# ---------------------------------------------------------------------------

_WORKSPACE_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# ``auth_shared`` lives in ``libs/auth-shared/src``. ``pytest.ini``'s
# ``pythonpath`` already covers this, but we add it defensively so the
# file works under direct ``python -m pytest tests/property`` invocations.
_AUTH_SHARED_SRC: Path = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))


# ---------------------------------------------------------------------------
# Module loader — admin-dashboard-api isn't a shared library and cannot
# share the literal ``src`` package name with sibling services. Load it
# under a unique alias so the relative imports inside the package
# (``from..config import Settings``, ``from.dependencies import...``)
# resolve against this module rather than another service's ``src/``.
# Pattern mirrors ``tests/property/test_health_contract.py`` and
# ``tests/unit/test_sensitive_env_key.py``.
# ---------------------------------------------------------------------------

_API_PKG_ALIAS = "_msf_admin_dashboard_api"


def _load_admin_api_submodule(submodule: str) -> ModuleType:
    """Import ``services/admin-dashboard-api/src.<submodule>`` lazily.

 Registers the ``src`` package under:data:`_API_PKG_ALIAS` once,
 then defers to:func:`importlib.import_module` so nested submodule
 imports inside the package (``from..config import Settings``)
 are routed through the same alias namespace.
 """

    src_dir = _WORKSPACE_ROOT / "services" / "admin-dashboard-api" / "src"
    if _API_PKG_ALIAS not in sys.modules:
        if not src_dir.is_dir():  # pragma: no cover - integrity check
            raise FileNotFoundError(f"Expected admin-dashboard-api src/ at {src_dir}")
        spec = importlib.util.spec_from_file_location(
            _API_PKG_ALIAS,
            str(src_dir / "__init__.py"),
            submodule_search_locations=[str(src_dir)],
        )
        assert spec is not None and spec.loader is not None
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_API_PKG_ALIAS] = pkg
        spec.loader.exec_module(pkg)
    return importlib.import_module(f"{_API_PKG_ALIAS}.{submodule}")


# Pre-load both modules at collection time so the FastAPI app construction
# below sees the same singleton dependency callables that the router uses.
_router_mod = _load_admin_api_submodule("routers.services_lifecycle")
_auth_mod = _load_admin_api_submodule("auth.dependencies")


# ---------------------------------------------------------------------------
# Manifest discovery — drives the ``service_name`` axis of the test.
# ---------------------------------------------------------------------------

_MANIFEST_PATH: Path = _WORKSPACE_ROOT / "config" / "services.manifest.json"
_MANIFEST_DOC: dict[str, Any] = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
_SERVICE_NAMES: tuple[str, ...] = tuple(s["name"] for s in _MANIFEST_DOC["services"])
assert _SERVICE_NAMES, (
    "config/services.manifest.json must declare at least one Managed_Service "
    "for endpoint coverage to be meaningful"
)


# ---------------------------------------------------------------------------
# Endpoint matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Endpoint:
    """One row of the lifecycle endpoint matrix.

 ``template`` is appended to the ``/admin/services`` prefix and may
 contain a ``{name}`` placeholder. ``body`` is the JSON payload
 (or ``None`` to skip). Bodies are picked so the request would
 otherwise validate cleanly — auth is the only failure path being
 exercised.
 """

    method: str
    template: str  # appended to /admin/services
    body: dict[str, Any] | None = None


#: All eight admin endpoints declared by ``services_lifecycle.router``
#:
#: * ``GET /`` → list summaries.
#: * ``GET /{name}`` → service detail.
#: * ``POST /{name}/start`` → start — body has the
#: ``env_overrides`` key set to ``{}`` so the Pydantic model
#: validates regardless of when body parsing runs relative to
#::func:`require_admin`.
#: * ``POST /{name}/stop`` → stop — body optional;
#: we still send ``{}`` so the StopRequest path matches its
#: ``remove_volumes`` default.
#: * ``POST /{name}/restart`` → restart — no body.
#: * ``POST /{name}/test`` → run tests — no body
#: (only the ``sectionstream=`` query parameter, defaulted).
#: * ``GET /{name}/logs`` → tail logs.
#: * ``GET /{name}/health`` → health snapshot.
_ENDPOINTS: tuple[_Endpoint, ...] = (
    _Endpoint("GET", "", None),
    _Endpoint("GET", "/{name}", None),
    _Endpoint("POST", "/{name}/start", {"env_overrides": {}}),
    _Endpoint("POST", "/{name}/stop", {}),
    _Endpoint("POST", "/{name}/restart", None),
    _Endpoint("POST", "/{name}/test", None),
    _Endpoint("GET", "/{name}/logs", None),
    _Endpoint("GET", "/{name}/health", None),
)


def _endpoint_id(ep: _Endpoint) -> str:
    """Render a stable pytest parametrize ID for one endpoint."""

    return f"{ep.method}{ep.template or '/'}"


def _resolve_path(template: str, name: str) -> str:
    """Substitute ``{name}`` in ``template`` and prefix ``/admin/services``."""

    return f"/admin/services{template.replace('{name}', name)}"


# ---------------------------------------------------------------------------
# Stub validator for the 403 branch
# ---------------------------------------------------------------------------


class _NonAdminValidator:
    """Stand-in:class:`OIDCValidator` that emits non-admin claims.

 The dev-mode validator that ships in
 ``libs/auth-shared/src/auth_shared/oidc.py`` returns canned admin
 claims for any non-empty token, so it can only ever exercise the
 401-then-200 path through:func:`require_admin`. To prove the
 403 branch we override ``get_validator``
 with this class, which validates the token (anything non-empty
 succeeds) but returns ``groups=["user"]`` — explicitly *not* an
 admin claim — so:func:`require_admin` rejects with 403.
 """

    def validate(self, token: str) -> dict[str, Any]:
        if not token:
            # Mirror the real validator's behaviour: empty tokens are
            # treated as outright invalid. The router converts the
            # ``InvalidTokenError`` into a 401 anyway, so this branch
            # is never exercised in this test (we always send a
            # non-empty token in the 403 path) but we keep parity for
            # safety.
            raise _auth_mod.InvalidTokenError("empty token")
        return {"sub": "user-1", "groups": ["user"]}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(*, with_user_validator: bool) -> FastAPI:
    """Construct a minimal FastAPI app exposing only the lifecycle router.

 The bare app skips the real ``src.main`` lifespan (which requires
 Postgres / Vault / Temporal), and the only dependencies it needs
 to override are:

 * ``get_lifecycle_service`` — return ``None``. Auth always runs
 first and short-circuits with 401 / 403, so this dependency
 should never produce a value the path operation actually
 consumes. Returning ``None`` makes any accidental fall-through
 crash loudly via ``AttributeError`` rather than silently 200.
 * ``get_validator`` (optional) — replace with:class:`_NonAdminValidator` so the 403 branch can be reached.
 """

    app = FastAPI()
    app.include_router(_router_mod.router)
    app.dependency_overrides[_router_mod.get_lifecycle_service] = lambda: None
    if with_user_validator:
        app.dependency_overrides[_auth_mod.get_validator] = _NonAdminValidator
    return app


# Build the two app instances exactly once per test session — Hypothesis
# samples thousands of times across the eight parametrised cases and
# spinning up a new FastAPI app per sample would dominate the runtime.
_APP_NO_AUTH: FastAPI = _build_app(with_user_validator=False)
_APP_USER_TOKEN: FastAPI = _build_app(with_user_validator=True)


# ---------------------------------------------------------------------------
# httpx helper
# ---------------------------------------------------------------------------


async def _async_request(
    app: FastAPI,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> int:
    """Issue a single request through the ASGI transport, return status code.

 Uses:class:`httpx.ASGITransport` because modern ``httpx`` (≥0.28)
 removed the deprecated ``AsyncClient(app=...)`` constructor. The
 base URL is a synthetic ``http://testserver`` which the transport
 short-circuits — no real socket is opened.
 """

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        kwargs: dict[str, Any] = {}
        if headers is not None:
            kwargs["headers"] = headers
        if body is not None:
            kwargs["json"] = body
        response = await client.request(method, path, **kwargs)
    return response.status_code


def _call(
    app: FastAPI,
    *,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> int:
    """Synchronous wrapper around:func:`_async_request`.

 Hypothesis test functions are sync; we drive the async ASGI
 request through a fresh event loop per example. The overhead is
 negligible compared to the actual handler execution and keeps
 Hypothesis's shrinker happy (no leaked task state across runs).
 """

    return asyncio.run(
        _async_request(app, method=method, path=path, body=body, headers=headers)
    )


# ---------------------------------------------------------------------------
# Anonymous request rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[_endpoint_id(e) for e in _ENDPOINTS])
@hyp_settings(
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(name=st.sampled_from(_SERVICE_NAMES))
def test_admin_endpoints_reject_anonymous(
    endpoint: _Endpoint,
    name: str,
) -> None:
    """For every (Managed_Service × admin endpoint) combination, an
 HTTP request that omits the ``Authorization`` header MUST be
 rejected with ``401 Unauthorized`` *before* any claim inspection
 or business logic runs.
 """

    path = _resolve_path(endpoint.template, name)
    status_code = _call(
        _APP_NO_AUTH,
        method=endpoint.method,
        path=path,
        body=endpoint.body,
        headers=None,
    )

    assert status_code == 401, (
        f"{endpoint.method} {path}: expected 401 for anonymous call, "
        f"got {status_code}"
    )


# ---------------------------------------------------------------------------
# Non-admin token rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=[_endpoint_id(e) for e in _ENDPOINTS])
@hyp_settings(
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(name=st.sampled_from(_SERVICE_NAMES))
def test_admin_endpoints_reject_non_admin_token(
    endpoint: _Endpoint,
    name: str,
) -> None:
    """For every (Managed_Service × admin endpoint) combination, a
 *valid* bearer token whose claims carry ``groups=["user"]``
 instead of ``["admin"]`` MUST be rejected with
 ``403 Forbidden``. Read-only access is **not** granted to
 authenticated non-admin users.
 """

    path = _resolve_path(endpoint.template, name)
    headers = {"Authorization": "Bearer non-admin-token"}
    status_code = _call(
        _APP_USER_TOKEN,
        method=endpoint.method,
        path=path,
        body=endpoint.body,
        headers=headers,
    )

    assert status_code == 403, (
        f"{endpoint.method} {path}: expected 403 for non-admin token, "
        f"got {status_code}"
    )


# ===========================================================================
# RBAC isolation
# ===========================================================================
#
#
# Behavior statement:
#
# For all (actor_role ∈ {viewer, lead, admin, dept_admin}, dept_id_target,
# endpoint) triples:
# (a) A dept_admin actor accessing a resource outside its own
# dept_ids set MUST be denied (HTTP 403 surface; here:
# PermissionDenied) and the failure carries actor_role=
# "dept_admin".
# (b) Global actions (new department, global prompt change, SSH
# runner config) admit ONLY the "admin" role; viewer / lead /
# dept_admin all receive 403.
# (c) Every admin endpoint call without an Authorization header or
# with an invalid token returns 401 or 403; missing
# AuthContext (None actor) is treated as PermissionDenied via
# MissingActorError.
# (d) A dept_admin acting on its OWN dept_id is admitted; viewer /
# lead WITH dept_id membership are admitted on dept-scoped
# viewer/lead checks (the standard role precedence).
#
# These properties are validated against ``auth_shared.policy.check``
# — the single source of truth for RBAC decisions in the platform.
# The HTTP layer ``require_admin`` in
# ``services/admin-dashboard-api/src/auth/dependencies.py`` is the
# admin-only specialisation of the same matrix; the existing test
# functions above pin the HTTP 401/403 surface for the admin-dashboard
# lifecycle endpoints. The tests below cover the **role
# matrix** (4 roles × dept-scope combinations) that ``check`` enforces
# and that future ``automation-service`` /admin/* endpoints will
# delegate to.

from auth_shared import (  # noqa: E402 — module-level imports above are heavy
    AuthContext,
    MissingActorError,
    PermissionDenied,
    ROLES,
    check,
    extract_auth_context,
    is_allowed,
)


# ---------------------------------------------------------------------------
# Strategy helpers — sample valid/invalid roles, dept ids, endpoints
# ---------------------------------------------------------------------------


# The four RBAC roles come from the runtime mirror so future role
# changes automatically propagate here.
_ROLE_LIST: tuple[str, ...] = tuple(sorted(ROLES))
assert _ROLE_LIST == ("admin", "dept_admin", "lead", "viewer"), (
    "the operational rule pins the four-role enumeration; if this assertion "
    "fires, audit_logger.AuditRole and auth_shared.AuthRole drifted out "
    "of sync. Update both before relaxing this check."
)

# Synthetic dept-id alphabet — kept short so Hypothesis explores the
# membership matrix densely. Values are lowercase + hyphen to match
# the Department.id shape used by the API (^[a-z][a-z0-9-]{1,30}$).
_DEPT_IDS: tuple[str, ...] = (
    "payments",
    "risk",
    "ops",
    "growth",
    "platform",
)


# Endpoint matrix for authorization behavior. Each entry mirrors a future
# automation-service /admin/* route or an existing lifecycle route;
# the ``required_role`` column encodes the authorization rule so the
# behavior is independent of any specific HTTP framework wiring.
#
# ``required_role="admin"`` rows are global actions: only ``admin`` may
# pass, every other role receives 403.
# Rows with ``required_role="dept_admin"`` and ``dept_scoped=True``
# are dept-scoped self-service actions:
# ``dept_admin`` of the matching dept passes, ``dept_admin`` of any
# other dept is denied.
@dataclass(frozen=True)
class _RbacEndpoint:
    """One row of the (endpoint, required_role, dept_scoped) matrix."""

    label: str
    required_role: str  # "viewer" | "lead" | "admin" | "dept_admin"
    dept_scoped: bool   # True ⇒ dept_id check applies
    rationale: str      # which the operational rule clause the row pins


_RBAC_ENDPOINTS: tuple[_RbacEndpoint, ...] = (
    # ---- Global-only --------------------------------
    _RbacEndpoint(
        label="POST /admin/departments",
        required_role="admin",
        dept_scoped=False,
        rationale="the operational rule: new department creation is admin-only",
    ),
    _RbacEndpoint(
        label="POST /admin/global-prompt",
        required_role="admin",
        dept_scoped=False,
        rationale="the operational rule: global prompt change is admin-only",
    ),
    _RbacEndpoint(
        label="POST /admin/ssh-runners",
        required_role="admin",
        dept_scoped=False,
        rationale="the operational rule: SSH runner config is admin-only",
    ),
    # ---- Dept-scoped self-service --------------
    _RbacEndpoint(
        label="POST /admin/departments/{dept_id}/credentials/rotate",
        required_role="dept_admin",
        dept_scoped=True,
        rationale="the operational rule: dept_admin rotates own dept's credentials",
    ),
    _RbacEndpoint(
        label="GET /admin/departments/{dept_id}/audit-events",
        required_role="dept_admin",
        dept_scoped=True,
        rationale="the operational rule: dept_admin sees only own dept rows",
    ),
    # ---- Dept-scoped read access --------------------
    _RbacEndpoint(
        label="GET /admin/departments/{dept_id}/probe-artifacts",
        required_role="viewer",
        dept_scoped=True,
        rationale=(
            "the operational rule: viewer/lead/dept_admin/admin can read "
            "their own dept's probe artifacts; cross-dept access by "
            "non-admin is denied"
        ),
    ),
    _RbacEndpoint(
        label="GET /admin/departments/{dept_id}/workflows",
        required_role="lead",
        dept_scoped=True,
        rationale="the operational rule: lead+ may inspect own dept workflows",
    ),
)


# ---------------------------------------------------------------------------
# AuthContext factory used inside Hypothesis strategies
# ---------------------------------------------------------------------------


def _ctx(role: str, *dept_ids: str, actor_id: str = "user-rbac") -> AuthContext:
    """Build a minimal:class:`AuthContext` with the given role + depts.

 Mirrors the helper in ``libs/auth-shared/tests/test_policy.py`` so
 behaviour stays in lock-step with the policy unit tests. Kept
 private to this module so the tests own their fixtures.
 """

    return AuthContext(
        actor_id=actor_id,
        actor_role=role,  # type: ignore[arg-type] # Literal erased at runtime
        dept_ids=frozenset(dept_ids),
        raw_claims={"sub": actor_id, "role": role},
    )


# ---------------------------------------------------------------------------
# Global admin-only endpoints
# ---------------------------------------------------------------------------


_GLOBAL_ENDPOINTS: tuple[_RbacEndpoint, ...] = tuple(
    e for e in _RBAC_ENDPOINTS if e.required_role == "admin" and not e.dept_scoped
)


@pytest.mark.parametrize(
    "endpoint",
    _GLOBAL_ENDPOINTS,
    ids=[e.label for e in _GLOBAL_ENDPOINTS],
)
@hyp_settings(
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    actor_role=st.sampled_from(_ROLE_LIST),
    actor_dept_ids=st.frozensets(st.sampled_from(_DEPT_IDS), min_size=0, max_size=3),
)
def test_global_admin_endpoint_admits_only_admin_role(
    endpoint: _RbacEndpoint,
    actor_role: str,
    actor_dept_ids: frozenset[str],
) -> None:
    """For every global admin endpoint and every (role, dept_ids)
 combination, ``check(actor, "admin")`` admits the call IFF the
 actor's role is exactly ``"admin"``. Any other role — including
 ``dept_admin`` even when its ``dept_ids`` is non-empty — is
 rejected with:class:`PermissionDenied` (HTTP 403 surface).

 The:func:`auth_shared.policy.check` decision matrix encodes this
 rule via ``_ROLES_THAT_SATISFY["admin"] = frozenset({"admin"})``.
 This test pins the externally-observable behaviour.
 """

    actor = _ctx(actor_role, *actor_dept_ids)

    if actor_role == "admin":
        # ``admin`` MUST pass — no exception raised.
        check(actor, "admin")
        assert is_allowed(actor, "admin"), (
            f"endpoint {endpoint.label!r}: admin role MUST be allowed, "
            f"is_allowed returned False (rationale: {endpoint.rationale})"
        )
    else:
        # Every other role MUST be denied with PermissionDenied.
        # MissingActorError is a subclass; we don't expect it here
        # because the actor is non-None.
        with pytest.raises(PermissionDenied) as exc_info:
            check(actor, "admin")
        # The exception must carry the rejected role so the audit log
        # row records ``actor_role=<rejected_role>``; the rejected
        # request is itself audit-worthy.
        assert exc_info.value.actor_role == actor_role, (
            f"PermissionDenied.actor_role={exc_info.value.actor_role!r} "
            f"does not match the rejected actor's role={actor_role!r}; "
            f"endpoint={endpoint.label!r}"
        )
        assert exc_info.value.required_role == "admin"


# ---------------------------------------------------------------------------
# Dept-admin cross-dept isolation
# ---------------------------------------------------------------------------


_DEPT_SCOPED_ENDPOINTS: tuple[_RbacEndpoint, ...] = tuple(
    e for e in _RBAC_ENDPOINTS if e.dept_scoped
)


@pytest.mark.parametrize(
    "endpoint",
    _DEPT_SCOPED_ENDPOINTS,
    ids=[e.label for e in _DEPT_SCOPED_ENDPOINTS],
)
@hyp_settings(
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    actor_dept_ids=st.frozensets(
        st.sampled_from(_DEPT_IDS), min_size=1, max_size=3
    ),
    target_dept_id=st.sampled_from(_DEPT_IDS),
)
def test_dept_admin_isolated_from_other_depts(
    endpoint: _RbacEndpoint,
    actor_dept_ids: frozenset[str],
    target_dept_id: str,
) -> None:
    """A ``dept_admin`` actor accessing a dept-scoped endpoint passes
 IFF ``target_dept_id ∈ actor.dept_ids``; cross-dept access is
 rejected with:class:`PermissionDenied` even when the role-class
 check would otherwise admit (i.e. ``dept_admin`` is in
 ``_ROLES_THAT_SATISFY[required_role]``).

 The exception preserves ``actor_role="dept_admin"`` and
 ``dept_id=<target_dept_id>`` so the audit row written by the
 proxy layer carries the rejected dept via ``audit_logger.write(action='proxy',
 result='rbac_denied', dept_id=target)``).
 """

    actor = _ctx("dept_admin", *actor_dept_ids)

    if target_dept_id in actor_dept_ids:
        # Own-dept access passes.
        check(actor, endpoint.required_role, dept_id=target_dept_id)
        assert is_allowed(actor, endpoint.required_role, dept_id=target_dept_id)
    else:
        with pytest.raises(PermissionDenied) as exc_info:
            check(actor, endpoint.required_role, dept_id=target_dept_id)
        assert exc_info.value.actor_role == "dept_admin", (
            f"PermissionDenied.actor_role={exc_info.value.actor_role!r}, "
            "expected 'dept_admin' so the audit row carries the rejected "
            "actor's role (the operational rule)"
        )
        assert exc_info.value.dept_id == target_dept_id, (
            f"PermissionDenied.dept_id={exc_info.value.dept_id!r}, "
            f"expected {target_dept_id!r} so the audit row records the "
            "specific dept the dept_admin tried to cross into"
        )


# ---------------------------------------------------------------------------
# Admin always passes any dept-scoped check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    _DEPT_SCOPED_ENDPOINTS,
    ids=[e.label for e in _DEPT_SCOPED_ENDPOINTS],
)
@hyp_settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    target_dept_id=st.sampled_from(_DEPT_IDS),
    # Even with an empty dept_ids set, admin must pass — the role is
    # global. The dept_ids argument is irrelevant for admin so we let
    # Hypothesis pick a random size.
    actor_dept_ids=st.frozensets(st.sampled_from(_DEPT_IDS), min_size=0, max_size=3),
)
def test_admin_passes_every_dept_scoped_endpoint(
    endpoint: _RbacEndpoint,
    target_dept_id: str,
    actor_dept_ids: frozenset[str],
) -> None:
    """The ``admin`` role bypasses dept-scope membership: regardless of
 ``actor.dept_ids`` and ``target_dept_id``, an ``admin`` actor
 passes every dept-scoped endpoint. Admin sees every department
 without requiring explicit dept membership.
 """

    actor = _ctx("admin", *actor_dept_ids)
    # No exception expected — admin passes unconditionally.
    check(actor, endpoint.required_role, dept_id=target_dept_id)
    assert is_allowed(actor, endpoint.required_role, dept_id=target_dept_id)


# ---------------------------------------------------------------------------
# Missing or invalid actor rejection surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    _RBAC_ENDPOINTS,
    ids=[e.label for e in _RBAC_ENDPOINTS],
)
@hyp_settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    target_dept_id=st.sampled_from(_DEPT_IDS),
)
def test_missing_actor_is_denied_for_every_endpoint(
    endpoint: _RbacEndpoint,
    target_dept_id: str,
) -> None:
    """For every (endpoint, dept_id) pair, calling ``check`` with
 ``actor=None`` MUST raise:class:`MissingActorError` (a:class:`PermissionDenied` subclass). The FastAPI middleware in
 ``admin-dashboard-api`` converts a missing token to an:class:`auth_shared.InvalidTokenError` upstream, which becomes
 HTTP 401; once a token is decoded but lacks claims (the operational rule),:func:`extract_auth_context` raises and the request also
 gets 401. A None actor reaching ``check`` is the safety-net and
 surfaces as PermissionDenied → 403.
 """

    target = target_dept_id if endpoint.dept_scoped else None
    with pytest.raises(PermissionDenied) as exc_info:
        check(None, endpoint.required_role, dept_id=target)
    # MissingActorError is the dedicated subclass for the no-actor
    # case; the wider PermissionDenied catch ensures the FastAPI
    # exception handler treats both with the same audit + status path.
    assert isinstance(exc_info.value, MissingActorError), (
        "None actor must surface as MissingActorError so the HTTP "
        "layer can pick the 401 vs 403 status without re-running the "
        "check; got "
        f"{type(exc_info.value).__name__}"
    )
    assert exc_info.value.actor_role is None
    assert exc_info.value.required_role == endpoint.required_role


# ---------------------------------------------------------------------------
# Missing claims from extract_auth_context
# ---------------------------------------------------------------------------


# A claim dict that omits ``sub`` or carries no recognised role MUST
# raise:class:`MissingClaimError` — a subclass of
#:class:`InvalidTokenError` so the FastAPI dependency translates it
# into HTTP 401 "eksik bilgi → HTTP 401").
@hyp_settings(
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    # Sample the missing-claim shape: drop ``sub``, drop ``role``,
    # drop both, or carry an unknown role. Each case maps to the
    # MissingClaimError code path.
    drop_sub=st.booleans(),
    drop_role=st.booleans(),
    bogus_role=st.sampled_from(("", "superuser", "Admin\x00", "owner")),
)
def test_extract_auth_context_rejects_malformed_claims(
    drop_sub: bool,
    drop_role: bool,
    bogus_role: str,
) -> None:
    """For every (drop_sub, drop_role, bogus_role) combination of the
 OIDC claim dict,:func:`extract_auth_context` MUST raise:class:`MissingClaimError` whenever ``sub`` is absent OR no
 recognised role is present. The error is a subclass of:class:`InvalidTokenError`, which the
 ``admin-dashboard-api`` ``require_admin`` dependency catches
 and converts into HTTP 401.
 """

    # Import locally so the module-level import block stays tightly
    # focused on the existing test surface; this lets the authorization
    # block be deleted as one unit if the policy changes.
    from auth_shared import InvalidTokenError, MissingClaimError

    claims: dict[str, Any] = {
        "iss": "https://idp.test",
        "aud": "platform-admin",
    }
    if not drop_sub:
        claims["sub"] = "user-42"
    if not drop_role:
        # ``bogus_role`` is intentionally NOT in AUTH_ROLES so the
        # role-extraction step still raises MissingClaimError. The
        # empty-string case is also covered: the claim is present
        # but trivially fails the lookup.
        claims["role"] = bogus_role

    # The claim dict is malformed regardless of which fields we
    # dropped: dropping sub fails on the sub check, dropping role
    # falls through to no-known-role, keeping a bogus role still
    # raises because it doesn't match AUTH_ROLES.
    with pytest.raises(MissingClaimError) as exc_info:
        extract_auth_context(claims)
    # Subclass relationship is part of the contract — admin-dashboard-
    # api's ``require_admin`` catches InvalidTokenError to surface a
    # 401, and MissingClaimError MUST be caught by the same handler.
    assert isinstance(exc_info.value, InvalidTokenError)


# ---------------------------------------------------------------------------
# Concrete regression anchor for AdminProxy authorization
# ---------------------------------------------------------------------------


def test_dept_admin_self_service_rotation_is_admitted() -> None:
    """Concrete anchor for dept-admin self-service.

 A ``dept_admin`` actor rotating its OWN dept's credentials is
 admitted by ``check``. When ``actor.actor_role == 'dept_admin'``
 and ``request_dept_id ∈ actor.dept_ids``, the proxy forwards
 the request to automation-service (no 403).
 """

    actor = _ctx("dept_admin", "payments")
    # Should not raise.
    check(actor, "dept_admin", dept_id="payments")
    assert is_allowed(actor, "dept_admin", dept_id="payments")


def test_dept_admin_global_action_is_denied() -> None:
    """Concrete anchor for global admin-only actions.

 Even a ``dept_admin`` of the affected dept is denied for the
 GLOBAL ``POST /admin/departments`` action because creating a new
 department is admin-only.
 """

    actor = _ctx("dept_admin", "payments")
    with pytest.raises(PermissionDenied) as exc_info:
        check(actor, "admin")
    assert exc_info.value.actor_role == "dept_admin"
    assert exc_info.value.required_role == "admin"
