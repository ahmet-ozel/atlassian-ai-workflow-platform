"""``AdminProxy`` — Backend-For-Frontend forwarder to automation-service.

This module implements admin proxy wiring of ``platform foundation``. The
``admin-dashboard-api`` service is a thin BFF that **owns no business
logic of its own**: every ``/admin/*`` route is forwarded to
``automation-service``, which is the single source of truth for
department CRUD, credential rotation, probe artifact cleanup, etc.
Endpoint ownership belongs to automation-service; admin-dashboard-api handles
auth and proxying.

Decision matrix:

* **Global admin actions** — adding a new department
  (``POST /admin/departments``), the setup wizard
  (``POST /admin/departments/wizard``), disabling a department
  (``POST /admin/departments/{id}/disable``), probe-artifact listing /
  cleanup (``/admin/probe-artifacts[/{id}]``), SSH runner configuration
  (``/admin/ssh-runners[/...]``) and global prompt changes
  (``/admin/prompts/global[/...]``) require ``role=admin``. ``dept_admin``
  is rejected with HTTP 403 + ``rbac_denied`` audit.

* **Dept-scoped self-service** — credential rotation for a specific
  department (``POST /admin/departments/{id}/credentials/rotate``)
  admits ``admin`` (always) and ``dept_admin`` whose ``dept_ids`` contain
  ``{id}``.

* **Per-service dept credential CRUD + probe** — the credential management
  endpoints
  (``POST|DELETE /admin/departments/{id}/credentials/{service}`` and
  ``POST /admin/departments/{id}/probe``) admit ``admin`` (always)
  and ``dept_admin`` whose ``dept_ids`` contain ``{id}``. They are
  matched by the generic dept-scoped catch-all
  (:data:`_RE_DEPT_SUB`) and emit the following audit actions on
  the ``automation-service`` side, all of which carry
  ``dept_id={id}`` and ``actor_role`` of the forwarded caller:

  * ``dept_credential_added`` — successful first-time write
    on successful first-time write.
  * ``dept_credential_updated`` — successful overwrite of an
    existing ``(dept_id, service)`` row.
  * ``dept_credential_removed`` — successful idempotent delete
    on successful idempotent delete.
  * ``dept_credential_probed`` — read+write connectivity probe
    with one row per probed service.
  * ``dept_credential_add_failed`` — staging / probe / DB / Vault
    failure with a stable ``reason`` marker; also written
    on remove-path failures.

  The proxy itself does **not** translate these actions — they are
  emitted by
  :class:`services.dept_credential_service.DeptCredentialService` —
  but they round-trip through the proxy's ``X-Actor-*`` header
  stamping so the audit trail keeps the caller's actual identity
  rather than the proxy's service account.

* **Other dept-scoped reads / writes** under
  ``/admin/departments/{id}/...`` admit ``admin`` and dept-matching
  ``dept_admin`` by default. ``viewer`` / ``lead`` are denied at the
  proxy boundary; downstream automation-service may further refine
  this for its own endpoint-specific policies.

* **Unknown / unclassified ``/admin/*`` paths** default to
  ``role=admin`` (fail-closed). New endpoints must be classified
  explicitly in :func:`classify_admin_path` before they can be
  reached by lower roles.

* **Non-``/admin/*`` paths** are rejected with HTTP 404 — the proxy is
  scoped strictly to the admin surface.

The proxy emits a single :class:`audit_logger.AuditEvent` with
``action="rbac_denied"``, ``result="denied"`` and the violating
``actor_id`` / ``actor_role`` / ``dept_id`` whenever it rejects a
request for RBAC reasons. Every audit row carries ``actor_role``. The audit
write is a *best-effort fire-and-forget* operation: a transient audit-DB outage
MUST NOT mask the underlying HTTP 403, so failures inside the audit emit are
caught and logged locally rather than re-raised.

The audit logger is injected through the constructor instead of
imported globally so unit tests can substitute an in-memory fake.
The same goes for the outbound :class:`httpx.AsyncClient` — production
wiring shares the process-wide client opened in
:func:`src.main.lifespan`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Protocol, runtime_checkable

import httpx
from auth_shared import AuthContext, PermissionDenied, check
from auth_shared.policy import Role
from audit_logger import AuditEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Headers — what we forward, what we strip
# ---------------------------------------------------------------------------

#: Hop-by-hop / connection-management headers that MUST NOT be forwarded
#: across a proxy hop (RFC 7230 §6.1). ``transfer-encoding`` is
#: re-computed by httpx based on the body shape, ``content-length``
#: similarly. Stripping ``host`` is essential — the upstream needs to
#: see its own host, not the dashboard's.
_HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    h.lower()
    for h in (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    )
)

#: ``Authorization`` is intentionally stripped at the proxy boundary —
#: the dashboard validated the OIDC token already and downstream
#: automation-service trusts the proxy via a separate service-mesh /
#: mTLS / shared secret channel configured separately via
#: ``AUTOMATION_SERVICE_URL`` and future mTLS wiring.
#: Forwarding the user's bearer token would let automation-service
#: re-authenticate on the dashboard's behalf, which is **not** the
#: trust model we want.
_AUTH_HEADERS_TO_STRIP: frozenset[str] = frozenset({"authorization", "cookie"})


# ---------------------------------------------------------------------------
# Audit action map — actions emitted *downstream* (by automation-service)
# in response to forwarded ``/admin/*`` calls. The proxy does not write
# these rows itself (it only writes ``rbac_denied`` on a 403); the map
# exists so reviewers can see at a glance which audit actions ride
# through the BFF + which dept-scope they carry, and so the test suite
# can assert that every documented action is covered by a route policy
# in :func:`classify_admin_path`.
# ---------------------------------------------------------------------------

#: Audit actions emitted by ``automation-service`` for the per-service
#: department credential CRUD + probe endpoints. All five are dept-scoped
#: (carry ``dept_id={id}``) and
#: ride through :data:`_RE_DEPT_SUB` (``dept_admin`` self-service).
DEPT_CREDENTIAL_AUDIT_ACTIONS: frozenset[str] = frozenset(
    {
        "dept_credential_added",
        "dept_credential_updated",
        "dept_credential_removed",
        "dept_credential_probed",
        "dept_credential_add_failed",
    }
)

#: All audit actions documented as flowing through ``AdminProxy`` for
#: the proxied credential endpoints. Extend this set when a new router on
#: ``automation-service`` lands; tests can import it to assert the
#: classification matrix stays in sync with the audit emission shape.
PROXIED_AUDIT_ACTIONS: frozenset[str] = (
    DEPT_CREDENTIAL_AUDIT_ACTIONS
)


# ---------------------------------------------------------------------------
# Path classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathPolicy:
    """Outcome of :func:`classify_admin_path`.

    Attributes:
        required_role: The minimum :data:`auth_shared.policy.Role` that
            satisfies the policy. ``"admin"`` for global actions,
            ``"dept_admin"`` for dept-scoped routes (``admin`` always
            satisfies that too — see :func:`auth_shared.policy.check`).
        dept_id: When the route is dept-scoped, the department id
            extracted from the URL path. ``None`` for global routes.
    """

    required_role: Role
    dept_id: str | None


# ``/admin/departments/<id>/credentials/rotate`` — dept_admin self-service.
_RE_DEPT_ROTATE = re.compile(
    r"^/admin/departments/(?P<dept_id>[a-z][a-z0-9_-]{0,63})/credentials/rotate/?$"
)

# ``/admin/departments/<id>/disable`` — admin only because disabling a
# department is an organization-level lifecycle action.
_RE_DEPT_DISABLE = re.compile(
    r"^/admin/departments/(?P<dept_id>[a-z][a-z0-9_-]{0,63})/disable/?$"
)

# ``/admin/departments/<id>[/anything]`` — dept-scoped catch-all (other
# than rotate/disable handled above). dept_admin allowed for own dept.
_RE_DEPT_SUB = re.compile(
    r"^/admin/departments/(?P<dept_id>[a-z][a-z0-9_-]{0,63})(?:/.*)?$"
)

# Paths that are global-admin-only by name.
_GLOBAL_ADMIN_PREFIXES: tuple[str, ...] = (
    "/admin/probe-artifacts",
    "/admin/ssh-runners",
    "/admin/prompts/global",
)


def _normalise_path(path: str) -> str:
    """Return ``path`` without trailing slashes (except root) and without query."""

    if "?" in path:
        path = path.split("?", 1)[0]
    if "#" in path:
        path = path.split("#", 1)[0]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def classify_admin_path(method: str, path: str) -> PathPolicy:
    """Classify ``method path`` into a :class:`PathPolicy`.

    The classifier is deterministic and side-effect free; tests cover
    the matrix exhaustively. New routes added to automation-service
    MUST be classified here before they can be reached through the
    proxy — the default branch is fail-closed at ``role=admin``.

    Args:
        method: HTTP method (``"GET"``, ``"POST"``, etc.). Currently
            unused by the matrix but retained on the signature so a
            future ``GET`` / ``POST`` split is a non-breaking change.
        path: The HTTP path as seen by the proxy (already stripped of
            query / fragment by FastAPI). Trailing slashes are
            tolerated.

    Returns:
        A :class:`PathPolicy` describing the role required to reach
        the route and the dept_id (when dept-scoped).

    Raises:
        ValueError: If ``path`` does not start with ``/admin/``. The
            proxy is restricted to the admin surface; non-admin paths
            never reach this classifier.
    """

    del method  # method-specific branching reserved for future use
    if not path.startswith("/admin/") and path != "/admin":
        raise ValueError(
            f"AdminProxy can only forward /admin/* paths; got {path!r}"
        )

    canonical = _normalise_path(path)

    # 0. Special-case ``/admin/departments`` (no id) and the
    #    ``wizard`` sub-route. These are global admin actions; they
    #    must be matched **before** the generic dept-scoped regex
    #    would otherwise grab ``wizard`` as a dept_id.
    if canonical == "/admin/departments" or canonical == "/admin/departments/wizard":
        return PathPolicy(required_role="admin", dept_id=None)

    # 1. Specific dept-scoped routes ----------------------------------
    match = _RE_DEPT_ROTATE.match(canonical)
    if match is not None:
        # Self-service rotation.
        return PathPolicy(
            required_role="dept_admin",
            dept_id=match.group("dept_id"),
        )

    match = _RE_DEPT_DISABLE.match(canonical)
    if match is not None:
        # Disabling a department is a global lifecycle action —
        # ``admin`` only. The dept_id is still surfaced so the audit
        # row carries the target.
        return PathPolicy(
            required_role="admin",
            dept_id=match.group("dept_id"),
        )

    # 2. Global-admin-only prefixes -----------------------------------
    for prefix in _GLOBAL_ADMIN_PREFIXES:
        if canonical == prefix or canonical.startswith(prefix + "/"):
            return PathPolicy(required_role="admin", dept_id=None)

    # 3. Generic dept-scoped catch-all under /admin/departments/<id>/ -
    match = _RE_DEPT_SUB.match(canonical)
    if match is not None:
        return PathPolicy(
            required_role="dept_admin",
            dept_id=match.group("dept_id"),
        )

    # 4. Default: fail-closed at admin. New routes MUST extend the
    #    classifier above before they can be reached by lower roles.
    return PathPolicy(required_role="admin", dept_id=None)


# ---------------------------------------------------------------------------
# Audit sink protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _AuditSink(Protocol):
    """Minimal audit-write surface used by :class:`AdminProxy`.

    The production wiring binds this to
    :class:`audit_logger.AuditLogger`, but the protocol is duck-typed
    on purpose: tests can pass an in-memory list-backed fake, and
    future call sites that go through a different transport (eg.
    Streamlit per-user audit) need not extend the inheritance graph.
    """

    async def write(self, event: AuditEvent) -> None:
        """Persist ``event``. Implementations may queue / retry / drop."""

        ...


# ---------------------------------------------------------------------------
# AdminProxy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyResponse:
    """Plain HTTP response shape returned by :meth:`AdminProxy.forward`.

    Returned as a dataclass (not a FastAPI :class:`Response`) so the
    proxy stays usable from non-FastAPI call sites — the FastAPI
    router adapter wraps this into a ``Response`` at the HTTP edge.
    The body is opaque bytes; the dashboard must not interpret or
    re-encode the upstream payload.
    """

    status_code: int
    headers: Mapping[str, str]
    body: bytes


class AdminProxy:
    """Forwards ``/admin/*`` requests to automation-service.

    The proxy performs three steps in order:

    1. **Path classification** — :func:`classify_admin_path` resolves
       the route to a :class:`PathPolicy` (required role, optional
       dept_id).
    2. **RBAC check** — :func:`auth_shared.policy.check` decides
       whether ``actor`` satisfies the policy. Failures emit a single
       ``rbac_denied`` audit row and return :class:`ProxyResponse`
       with ``status_code=403``.
    3. **Forwarding** — the request is reissued against
       ``{automation_service_url}{path}`` via the injected
       :class:`httpx.AsyncClient`. Hop-by-hop headers are stripped;
       ``Authorization`` / ``Cookie`` are stripped because the
       proxy-to-upstream channel uses a separate trust mechanism (see
       module docstring).

    Args:
        automation_service_url: Base URL of automation-service (eg.
            ``http://automation-service:8080``). Trailing slashes are
            stripped. Must be HTTP(S).
        http_client: Outbound :class:`httpx.AsyncClient`. Production
            wiring shares the process-wide client; tests inject one
            backed by :class:`httpx.MockTransport`.
        audit_sink: Object satisfying :class:`_AuditSink`. Production
            wiring binds this to :class:`audit_logger.AuditLogger`.
        request_timeout_seconds: Per-request timeout applied to the
            outbound call. Defaults to 30s.
    """

    def __init__(
        self,
        *,
        automation_service_url: str,
        http_client: httpx.AsyncClient,
        audit_sink: _AuditSink,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        if not automation_service_url:
            raise ValueError("automation_service_url must not be empty")
        if not (
            automation_service_url.startswith("http://")
            or automation_service_url.startswith("https://")
        ):
            raise ValueError(
                "automation_service_url must start with http:// or https:// "
                f"(got {automation_service_url!r})"
            )
        self._upstream = automation_service_url.rstrip("/")
        self._http = http_client
        self._audit = audit_sink
        self._timeout = request_timeout_seconds

    async def forward(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str],
        actor: AuthContext,
        query_string: str = "",
    ) -> ProxyResponse:
        """Authorise + forward a single ``/admin/*`` request.

        Args:
            method: HTTP method.
            path: The ``/admin/...`` path (no query / fragment).
            body: Raw request body. Forwarded byte-for-byte.
            headers: Inbound headers; hop-by-hop and auth headers are
                stripped before forwarding.
            actor: Authenticated :class:`AuthContext` for the caller.
            query_string: Optional raw query string (without leading
                ``?``) to append to the upstream URL. Empty by default.

        Returns:
            :class:`ProxyResponse` carrying the upstream status code,
            headers (with hop-by-hop entries stripped) and body. On
            an RBAC denial returns ``status_code=403`` *without*
            calling the upstream.
        """

        # --- 1. Classify -------------------------------------------------
        try:
            policy = classify_admin_path(method, path)
        except ValueError:
            # Non-admin paths must never reach the proxy — surface
            # this as 404 so an accidental mount does not leak
            # behaviour. We still emit a structured log for the
            # operator.
            logger.warning(
                "AdminProxy received non-admin path %s; returning 404",
                path,
            )
            return ProxyResponse(
                status_code=404,
                headers={"content-type": "application/json"},
                body=b'{"detail":"not found"}',
            )

        # --- 2. RBAC check ----------------------------------------------
        try:
            check(actor, policy.required_role, policy.dept_id)
        except PermissionDenied as denial:
            await self._emit_rbac_denied(
                actor=actor,
                method=method,
                path=path,
                policy=policy,
                denial=denial,
            )
            return ProxyResponse(
                status_code=403,
                headers={"content-type": "application/json"},
                body=b'{"detail":"forbidden"}',
            )

        # --- 3. Forward to automation-service ---------------------------
        upstream_url = f"{self._upstream}{path}"
        if query_string:
            upstream_url = f"{upstream_url}?{query_string}"

        forwarded_headers = _filter_headers(headers)
        # Stamp the actor on the forwarded request so automation-service
        # can write its own audit rows without re-validating the OIDC
        # token. The downstream service trusts ``X-Actor-*`` because
        # the only ingress for these headers is this proxy (the values
        # are stripped from the inbound side via ``_filter_headers``,
        # which knows nothing about ``X-Actor-*`` so a malicious caller
        # cannot spoof them — see ``test_x_actor_headers_overwritten``).
        forwarded_headers["x-actor-id"] = actor.actor_id
        forwarded_headers["x-actor-role"] = actor.actor_role
        if policy.dept_id is not None:
            forwarded_headers["x-actor-dept-id"] = policy.dept_id

        try:
            response = await self._http.request(
                method=method,
                url=upstream_url,
                headers=forwarded_headers,
                content=body,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            logger.warning(
                "AdminProxy upstream timeout: method=%s path=%s err=%s",
                method,
                path,
                exc,
            )
            return ProxyResponse(
                status_code=504,
                headers={"content-type": "application/json"},
                body=b'{"detail":"upstream timeout"}',
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "AdminProxy upstream error: method=%s path=%s err=%s",
                method,
                path,
                exc,
            )
            return ProxyResponse(
                status_code=502,
                headers={"content-type": "application/json"},
                body=b'{"detail":"upstream error"}',
            )

        return ProxyResponse(
            status_code=response.status_code,
            headers=_strip_response_hop_headers(response.headers),
            body=response.content,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _emit_rbac_denied(
        self,
        *,
        actor: AuthContext,
        method: str,
        path: str,
        policy: PathPolicy,
        denial: PermissionDenied,
    ) -> None:
        """Best-effort write of a single ``rbac_denied`` audit row.

        Failures inside the audit emit are *swallowed* — an audit DB
        outage MUST NOT convert the underlying 403 into a 500. The
        operator notices the missing audit row through the
        higher-level ``audit_write_deferred`` / drainer telemetry that
        sits behind :class:`audit_logger.AuditLogger`.
        """

        # ``actor.actor_role`` is one of the four AUTH_ROLES; the audit
        # table accepts the same four plus ``"system"``. The cast is
        # safe — the OIDC layer rejects unknown roles before we ever
        # see them here.
        event = AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            dept_id=policy.dept_id,
            action="rbac_denied",
            resource=f"{method.upper()} {path}",
            result="denied",
            timestamp=datetime.now(timezone.utc),
            payload={
                "required_role": policy.required_role,
                "actor_role": actor.actor_role,
                "actor_dept_ids": sorted(actor.dept_ids),
                "reason": str(denial),
            },
        )
        try:
            await self._audit.write(event)
        except Exception as exc:  # noqa: BLE001 — best-effort write
            logger.warning(
                "AdminProxy: rbac_denied audit write failed (HTTP 403 still "
                "returned to caller): actor=%s path=%s err=%s",
                actor.actor_id,
                path,
                exc,
            )


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _filter_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a forwardable copy of ``headers``.

    Drops:

    * **Hop-by-hop headers** (RFC 7230 §6.1) — these are between two
      adjacent peers only.
    * **Authorization / Cookie** — the proxy and automation-service
      establish trust through a separate channel; forwarding the
      user's bearer token would change the trust model.
    * **Any inbound ``X-Actor-*`` header** — those are *output* only.
      A malicious caller cannot inject ``X-Actor-Role: admin`` because
      the proxy overwrites the slot after this filter runs.
    """

    out: dict[str, str] = {}
    for raw_key, value in headers.items():
        key = raw_key.lower()
        if key in _HOP_BY_HOP_HEADERS:
            continue
        if key in _AUTH_HEADERS_TO_STRIP:
            continue
        if key.startswith("x-actor-"):
            # Actor identity is reasserted by the proxy; drop any
            # inbound spoof attempt.
            continue
        out[key] = value
    return out


def _strip_response_hop_headers(
    headers: Iterable[tuple[str, str]] | Mapping[str, str],
) -> dict[str, str]:
    """Return a forwardable copy of upstream response headers.

    httpx already decodes ``transfer-encoding`` for us, so re-emitting
    that header would corrupt the body framing on the dashboard's
    response. ``content-length`` is similarly recomputed by FastAPI /
    starlette when the response is returned.
    """

    if isinstance(headers, Mapping):
        items: Iterable[tuple[str, str]] = headers.items()
    else:
        items = headers
    out: dict[str, str] = {}
    for raw_key, value in items:
        key = raw_key.lower()
        if key in _HOP_BY_HOP_HEADERS:
            continue
        out[key] = value
    return out


__all__ = [
    "AdminProxy",
    "DEPT_CREDENTIAL_AUDIT_ACTIONS",
    "PROXIED_AUDIT_ACTIONS",
    "PathPolicy",
    "ProxyResponse",
    "classify_admin_path",
]
