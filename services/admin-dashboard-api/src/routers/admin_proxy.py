"""FastAPI router that mounts :class:`src.proxy.AdminProxy`.

This router wires the BFF proxy from admin proxy wiring of
``platform foundation`` into the admin-dashboard-api FastAPI
app. It registers a catch-all handler on the admin endpoints that are
**owned by automation-service**:

* ``/admin/departments`` and ``/admin/departments/...`` — atomic dept
  create, setup wizard, credential rotation, dept disable.
* ``/admin/probe-artifacts`` and ``/admin/probe-artifacts/...`` —
  partial-orphan listing / cleanup.
* ``/admin/ssh-runners`` / ``/admin/ssh-runners/...`` — SSH runner
  configuration for admins only.
* ``/admin/prompts/global`` / ``/admin/prompts/global/...`` — global
  prompt change for admins only.

The router deliberately does **not** mount under bare ``/admin`` —
the existing ``/admin/services`` surface from the
admin-dashboard-api is owned locally by admin-dashboard-api (Compose
orchestration on the local Docker socket) and must keep its routes.
``/admin/services`` traffic is served by ``src.routers.services_lifecycle``
and never reaches this proxy.

Authentication
--------------
``Depends(require_admin)`` on the router validates the inbound OIDC
token *and* asserts an admin claim. The dependency rejects anonymous
requests with HTTP 401 before the proxy gets a chance to run. After
that gate, :class:`AdminProxy` performs its own RBAC check against
:func:`auth_shared.policy.check` because the path-derived dept_id
needs to be matched against the actor's ``dept_ids``.

Note that ``require_admin`` (from ``src.auth.dependencies``) currently
returns an :class:`AuthClaims` object with only ``sub`` + ``groups``;
:class:`auth_shared.AuthContext` carries the richer ``actor_role`` /
``dept_ids`` shape needed by the proxy. We bridge the two by extracting
an :class:`AuthContext` from the validated JWT claim dict via
:func:`auth_shared.extract_auth_context` — the dependency reads the
raw token once, validates it, and then derives both projections.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response

from auth_shared import (
    AuthContext,
    InvalidTokenError,
    OIDCValidator,
    extract_auth_context,
)

from ..auth.dependencies import get_validator
from ..proxy import AdminProxy

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Bearer token parsing — duplicated from ``src.auth.dependencies`` so this
# router does not depend on its private constants.
# ---------------------------------------------------------------------------

_BEARER_SCHEME = "bearer"
_BEARER_PREFIX_LEN = len(_BEARER_SCHEME) + 1  # ``"bearer "`` (7 chars)


# ---------------------------------------------------------------------------
# Auth dependency: produce an AuthContext from the validated JWT
# ---------------------------------------------------------------------------


async def require_auth_context(
    request: Request,
    validator: Annotated[OIDCValidator, Depends(get_validator)],
) -> AuthContext:
    """FastAPI dependency that returns an :class:`AuthContext`.

    Mirrors the bearer-token handling of :func:`require_admin` but
    returns the richer :class:`AuthContext` shape (carrying
    ``actor_role`` and ``dept_ids``) instead of the simpler
    :class:`AuthClaims`. The proxy needs the dept_ids to perform
    self-service RBAC matching for ``dept_admin`` rotation calls.

    Failure modes:

    * Missing / non-Bearer ``Authorization`` header → ``401``.
    * Empty token after ``Bearer`` prefix → ``401``.
    * Validator raises :class:`InvalidTokenError` → ``401``.
    * Token validates but :func:`extract_auth_context` rejects the
      claim shape (eg. unknown role, missing ``sub``) → ``401`` (the
      missing-claim case is already a subclass of
      :class:`InvalidTokenError` so a single ``except`` clause covers
      both).
    """

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith(_BEARER_SCHEME + " "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    token = auth_header[_BEARER_PREFIX_LEN:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )

    try:
        claims = validator.validate(token)
        return extract_auth_context(claims)
    except InvalidTokenError:
        # Both signature failures and missing-claim cases land here —
        # ``MissingClaimError`` is a subclass of ``InvalidTokenError``.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )


# ---------------------------------------------------------------------------
# Proxy resolution
# ---------------------------------------------------------------------------


def get_admin_proxy(request: Request) -> AdminProxy:
    """Return the process-wide :class:`AdminProxy` singleton.

    The instance is constructed during :func:`src.main.lifespan` and
    stashed on ``app.state.admin_proxy``. When that wiring has not
    been performed (eg. during a manifest-load failure or before
    metrics client wiring has completed) we surface a
    ``503`` matching the readiness probe's shape.
    """

    proxy: AdminProxy | None = getattr(request.app.state, "admin_proxy", None)
    if proxy is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "admin_proxy_unavailable"},
        )
    return proxy


# ---------------------------------------------------------------------------
# Router — catch-all forwarder
# ---------------------------------------------------------------------------


router = APIRouter(tags=["admin-proxy"])


# Methods we forward. ``HEAD`` and ``OPTIONS`` are *not* in the list —
# CORS preflight (OPTIONS) is handled by FastAPI's CORSMiddleware when
# enabled, and HEAD is rarely used by admin endpoints. Explicitly
# listing methods is preferred over a wildcard so the OpenAPI surface
# stays accurate and so an unsupported verb fails with 405 at the
# router boundary, not at the upstream.
_FORWARDED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


# Path prefixes covered by this proxy. ``/admin/services`` is
# intentionally absent — that surface is owned by
# ``src.routers.services_lifecycle``.
_PROXIED_PREFIXES: tuple[str, ...] = (
    "/admin/departments",
    "/admin/probe-artifacts",
    "/admin/ssh-runners",
    "/admin/prompts/global",
)


def _register_proxy_handlers() -> None:
    """Register catch-all handlers on the proxied prefixes.

    Done in a helper so the registration loop is explicit and so the
    same code path can be re-invoked by tests that need to attach the
    router to a fresh app instance.
    """

    for prefix in _PROXIED_PREFIXES:
        # Two routes per prefix: the bare prefix (``/admin/departments``)
        # and a catch-all subpath (``/admin/departments/{rest:path}``).
        # FastAPI's ``{...:path}`` converter accepts slashes and lets a
        # single handler cover arbitrarily deep nesting.
        for path in (prefix, prefix + "/{rest:path}"):
            for method in _FORWARDED_METHODS:
                router.add_api_route(
                    path,
                    _forward,
                    methods=[method],
                    name=f"admin_proxy_{method.lower()}_{prefix.replace('/', '_').strip('_')}",
                    include_in_schema=False,
                )


async def _forward(
    request: Request,
    actor: Annotated[AuthContext, Depends(require_auth_context)],
    proxy: Annotated[AdminProxy, Depends(get_admin_proxy)],
) -> Response:
    """Catch-all handler that delegates to :meth:`AdminProxy.forward`.

    The handler reads the raw request body once, then hands every
    detail to the proxy. The proxy returns a :class:`ProxyResponse`
    which we translate verbatim into a FastAPI :class:`Response` —
    body, status code and (filtered) headers.

    On RBAC denial the proxy returns ``status_code=403`` with a fixed
    JSON body; the audit row is written by the proxy itself.
    """

    body = await request.body()
    headers = {key: value for key, value in request.headers.items()}

    proxy_response = await proxy.forward(
        method=request.method,
        path=request.url.path,
        body=body,
        headers=headers,
        actor=actor,
        query_string=request.url.query or "",
    )

    return Response(
        content=proxy_response.body,
        status_code=proxy_response.status_code,
        headers=dict(proxy_response.headers),
    )


_register_proxy_handlers()


__all__ = ["router", "require_auth_context", "get_admin_proxy"]
