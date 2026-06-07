"""FastAPI router - per-service department credential CRUD + probe.

The router is the **thin shim** layer for department and bot
credential management: every endpoint parses the request, dispatches
to :class:`services.dept_credential_service.DeptCredentialService`,
and translates the orchestrator's exception ladder into deterministic
HTTP responses.

Endpoints
---------

* ``GET /admin/departments`` - list every department with its bot
  credential refs (mask edilmiş).
* ``GET /admin/departments/{id}`` - full detail for a single
  department (bots, project keys, space keys, mode).
* ``POST /admin/departments/{id}/credentials/{service}`` - atomic
  add or update of the ``(dept_id, service)`` bot credential.
* ``DELETE /admin/departments/{id}/credentials/{service}`` -
  idempotent removal.
* ``POST /admin/departments/{id}/probe`` - re-run the connectivity
  probe for one (``?service=...``) or all of the dept's bots.

Wiring contract (``app.state.dept_credentials``)
------------------------------------------------

The :func:`automation_service.app.create_app` factory populates
``request.app.state.dept_credentials`` with a single
:class:`DeptCredentialEndpointDeps` instance.  It carries:

* ``service`` - the :class:`DeptCredentialService` instance.
* ``connection_factory`` - async factory returning a fresh
  :class:`db_shared.AsyncConnection`.  Used only by the read-side
  endpoints (list / detail) - the mutating endpoints delegate
  ownership of the SQL session to the orchestrator.
* ``clock`` - optional UTC-now factory; defaults to
  :func:`datetime.now(timezone.utc)`.  Tests inject a deterministic
  clock to exercise audit timestamps.

Authentication / authorization
------------------------------

The router sits **behind** the ``admin-dashboard-api`` ``AdminProxy``.
The proxy performs the OIDC + RBAC pre-check and stamps three headers
on every forwarded request:

* ``X-Actor-Id`` - the OIDC ``sub`` of the human admin (or the bot
  ``account_id`` for a system caller).
* ``X-Actor-Role`` - one of ``"admin"``, ``"dept_admin"``,
  ``"lead"``, ``"viewer"``, ``"system"``.
* ``X-Actor-Dept-Id`` - populated only for dept-scoped routes;
  contains the dept_id parsed by
  :func:`admin_dashboard_api.proxy.classify_admin_path`.

Direct (un-proxied) requests fall back to the ``"system"`` actor -
production deploys mark ``admin-dashboard-api`` as the only ingress
so this fallback is only reachable by integration tests that bypass
the proxy.

The router still enforces a **defence-in-depth** RBAC check on top
of the proxy:

* ``admin`` and ``system`` may operate on any dept.
* ``dept_admin`` may only mutate / probe / read a dept whose id
  matches ``X-Actor-Dept-Id``.  Mismatches return HTTP 403.
* ``lead`` / ``viewer`` are denied on every mutating endpoint
  (POST / DELETE).  They may issue read-only ``GET`` calls because
  the AdminProxy classifier already maps the catch-all dept-scoped
  read path to ``required_role="dept_admin"`` (so a viewer never
  reaches the router); the router-side check is a safety net for
  direct calls that bypass the proxy.

Audit
-----

The orchestrator owns the canonical audit emission for every
mutation (``dept_credential_added``, ``_updated``, ``_removed``,
``_probed``, ``_add_failed``).  The router writes a single
``rbac_denied`` row when the dept-scope mismatch fires so the
denial trail is symmetrical across all admin endpoints.

"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Literal,
    Mapping,
    Sequence,
)

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from db_shared import AsyncConnection, with_dept_session

from automation_service.probe import ProbeService, ProbeTargets
from automation_service.staging import VALID_SERVICES, validate_dept_id
from services.dept_bulk_import_service import (
    BulkImportResult,
    BulkImportService,
    DeptImportOutcome,
    SchemaValidationError,
)
from services.dept_credential_service import (
    AddCredentialRequest,
    AddCredentialResult,
    DepartmentNotFoundError,
    DeptCredentialOperationError,
    DeptCredentialService,
    ProbeRunOutcome,
    RemoveCredentialResult,
)

__all__ = ["DeptCredentialEndpointDeps", "router"]

_LOG = logging.getLogger(__name__)

#: Roles that are *always* allowed to mutate dept credentials.
_PRIVILEGED_ROLES: frozenset[str] = frozenset({"admin", "system"})

#: Roles that may read dept credentials.  ``dept_admin`` is gated on
#: a matching ``X-Actor-Dept-Id`` for dept-scoped reads; the global
#: list endpoint filters to the caller's own depts in that case.
_READ_ROLES: frozenset[str] = frozenset(
    {"admin", "system", "dept_admin"}
)


# ---------------------------------------------------------------------------
# Dependency container - injected via ``app.state.dept_credentials``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptCredentialEndpointDeps:
    """Collaborators the router pulls from ``app.state.dept_credentials``.

    Production wiring builds one of these in
    :func:`automation_service.app.create_app`; tests construct an
    instance with hand-built fakes.

    Attributes:
        service: The credential CRUD orchestrator.
        connection_factory: Async factory returning a fresh
            :class:`db_shared.AsyncConnection`.  Used by the read
            endpoints for SELECTs against
            ``automation.departments`` /
            ``automation.department_bots``; the orchestrator owns
            the session for every mutation.
        audit_logger: Used by the router *only* for the
            ``rbac_denied`` row written on a dept-scope mismatch.
            All success-path audit emission is owned by the
            orchestrator.
        clock: UTC-now factory.  Defaults to
            :func:`datetime.now(timezone.utc)`.
    """

    service: DeptCredentialService
    connection_factory: Callable[[], Awaitable[AsyncConnection]]
    audit_logger: AuditLogger
    clock: Callable[[], datetime] | None = None


def _deps(request: Request) -> DeptCredentialEndpointDeps:
    """Pull the :class:`DeptCredentialEndpointDeps` off ``app.state``.

    Surfaces a 500 if the application factory neglected to wire the
    collaborators - making the deployment misconfiguration explicit
    rather than letting a downstream attribute access throw a less
    helpful error.
    """

    deps = getattr(request.app.state, "dept_credentials", None)
    if not isinstance(deps, DeptCredentialEndpointDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="dept_credentials router is not wired "
            "(app.state.dept_credentials missing)",
        )
    return deps


def _now(deps: DeptCredentialEndpointDeps) -> datetime:
    """Return ``deps.clock()`` when set, otherwise wall-clock UTC."""

    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc)


def _load_config_departments() -> list[dict[str, Any]]:
    """Return departments from mounted config/departments.json."""

    for parent in Path(__file__).resolve().parents:
        path = parent / "config" / "departments.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
        return [
            item for item in data.get("departments", []) if isinstance(item, dict)
        ]
    return []


def _load_config_department(dept_id: str) -> dict[str, Any] | None:
    """Return a department from mounted config/departments.json if present."""

    for item in _load_config_departments():
        if item.get("id") == dept_id:
            return item
    return None


async def _ensure_department_seeded_from_config(
    deps: DeptCredentialEndpointDeps,
    dept_id: str,
) -> bool:
    """Mirror a config-only department into automation Postgres.

    The admin dashboard stores the operator-facing department catalog in
    config/departments.json. Credential/probe endpoints use Postgres as their
    runtime source. If the config has already been updated but the runtime row
    is not present yet, seed the runtime tables before credential mutation.
    """

    configured_departments = _load_config_departments()
    dept = next(
        (item for item in configured_departments if item.get("id") == dept_id),
        None,
    )
    if dept is None:
        return False
    configured_ids = [
        str(item.get("id"))
        for item in configured_departments
        if str(item.get("id") or "").strip()
    ]

    mode = str(dept.get("mode") or "active")
    if mode not in {"active", "shadow", "disabled"}:
        mode = "active"

    connection = await deps.connection_factory()
    async with with_dept_session("admin", dept_id, connection=connection) as conn:
        if configured_ids:
            # Runtime bot rows for departments removed from config can keep
            # unique bot identities reserved. Drop only the runtime bindings;
            # keep the department rows themselves so historic workflow/audit
            # references remain resolvable.
            await conn.execute(
                """
                DELETE FROM automation.department_bots
                 WHERE NOT (department_id = ANY($1::text[]))
                """,
                configured_ids,
            )
            await conn.execute(
                """
                DELETE FROM automation.department_project_keys
                 WHERE NOT (department_id = ANY($1::text[]))
                """,
                configured_ids,
            )
            await conn.execute(
                """
                DELETE FROM automation.department_space_keys
                 WHERE NOT (department_id = ANY($1::text[]))
                """,
                configured_ids,
            )
            await conn.execute(
                """
                DELETE FROM automation.repo_mappings
                 WHERE NOT (department_id = ANY($1::text[]))
                """,
                configured_ids,
            )

        await conn.execute(
            """
            INSERT INTO automation.departments (
                id, display_name, default_language, web_search_enabled,
                mode, config_json, created_at, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                default_language = EXCLUDED.default_language,
                web_search_enabled = EXCLUDED.web_search_enabled,
                mode = EXCLUDED.mode,
                config_json = EXCLUDED.config_json,
                updated_at = NOW()
            """,
            dept_id,
            str(dept.get("display_name") or dept_id),
            str(dept.get("default_language") or "tr"),
            bool(dept.get("web_search_enabled", True)),
            mode,
            json.dumps(dept),
        )

        for key in dept.get("jira_project_keys", []) or []:
            key_text = str(key).strip()
            if key_text:
                await conn.execute(
                    """
                    INSERT INTO automation.department_project_keys
                        (department_id, project_key)
                    VALUES ($1, $2)
                    ON CONFLICT (project_key) DO UPDATE SET
                        department_id = EXCLUDED.department_id
                    """,
                    dept_id,
                    key_text,
                )

        for key in dept.get("confluence_space_keys", []) or []:
            key_text = str(key).strip()
            if key_text:
                await conn.execute(
                    """
                    INSERT INTO automation.department_space_keys
                        (department_id, space_key)
                    VALUES ($1, $2)
                    ON CONFLICT (space_key) DO UPDATE SET
                        department_id = EXCLUDED.department_id
                    """,
                    dept_id,
                    key_text,
                )

        bot = dept.get("bot") if isinstance(dept.get("bot"), dict) else {}
        for service_name in ("jira", "bitbucket", "confluence"):
            entry = bot.get(service_name) if isinstance(bot, dict) else None
            if not isinstance(entry, dict):
                continue
            deployment = entry.get("deployment") or "cloud"
            if deployment == "server":
                deployment = "dc"
            if deployment not in {"cloud", "dc"}:
                deployment = "cloud"
            await conn.execute(
                """
                INSERT INTO automation.department_bots (
                    department_id, service, credential_ref,
                    account_id, username, deployment
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (department_id, service) DO UPDATE SET
                    credential_ref = EXCLUDED.credential_ref,
                    account_id = COALESCE(
                        NULLIF(EXCLUDED.account_id, ''),
                        automation.department_bots.account_id
                    ),
                    username = COALESCE(
                        NULLIF(EXCLUDED.username, ''),
                        automation.department_bots.username
                    ),
                    deployment = EXCLUDED.deployment
                """,
                dept_id,
                service_name,
                str(
                    entry.get("credential_ref")
                    or f"vault:atlassian/{dept_id}/{service_name}"
                ),
                entry.get("account_id") or None,
                entry.get("username") or None,
                deployment,
            )

    return True


# ---------------------------------------------------------------------------
# Actor extraction (proxy-emitted headers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Actor:
    """Minimal actor projection reconstructed from proxy headers.

    Attributes:
        actor_id: The ``X-Actor-Id`` header (or ``"system"`` if
            absent - only reachable by integration tests that
            bypass the proxy).
        actor_role: One of the recognised RBAC roles, normalised to
            ``"system"`` for unknown values so a malformed proxy
            header never escalates privilege.
        dept_id: The ``X-Actor-Dept-Id`` header.  ``None`` when the
            proxy did not stamp it (global / non-dept-scoped
            routes).
    """

    actor_id: str
    actor_role: Literal["admin", "system", "dept_admin", "lead", "viewer"]
    dept_id: str | None


_KNOWN_ROLES: frozenset[str] = frozenset(
    {"admin", "system", "dept_admin", "lead", "viewer"}
)


def _extract_actor(request: Request) -> _Actor:
    """Build an :class:`_Actor` from the proxy-emitted headers.

    ``admin-dashboard-api`` is the only ingress that populates the
    ``X-Actor-*`` headers (after running its OIDC + RBAC
    pre-checks).  Direct requests (integration tests, smoke probes)
    fall back to the ``"system"`` actor - production deployments
    enforce the proxy via Compose-level network isolation so the
    fallback is never reachable in real traffic.
    """

    actor_id = request.headers.get("x-actor-id") or "system"
    actor_role_raw = (request.headers.get("x-actor-role") or "system").lower()
    if actor_role_raw not in _KNOWN_ROLES:
        # Unknown role: treat as the lowest-privilege fallback so a
        # malformed proxy header cannot escalate.
        actor_role_raw = "system"
    dept_id_header = request.headers.get("x-actor-dept-id")
    return _Actor(
        actor_id=actor_id,
        actor_role=actor_role_raw,  # type: ignore[arg-type]
        dept_id=dept_id_header or None,
    )


def _audit_role(
    role: Literal["admin", "system", "dept_admin", "lead", "viewer"],
) -> Literal["admin", "system", "dept_admin"]:
    """Coerce a proxy-emitted role into one the orchestrator accepts.

    The orchestrator's audit writer only knows ``admin``,
    ``dept_admin`` and ``system``.  ``lead`` / ``viewer`` are
    rejected by the router-level RBAC guard before they reach the
    orchestrator, but the type narrowing here keeps mypy happy.
    """

    if role in ("admin", "dept_admin", "system"):
        return role  # type: ignore[return-value]
    # ``lead`` / ``viewer`` are denied earlier; this branch is
    # defensive - fall back to ``system`` so the audit row is still
    # well-formed if a caller bypasses the guard.
    return "system"


# ---------------------------------------------------------------------------
# RBAC helpers
# ---------------------------------------------------------------------------


async def _enforce_dept_scope(
    deps: DeptCredentialEndpointDeps,
    actor: _Actor,
    target_dept_id: str,
    *,
    require_mutating_role: bool,
    method: str,
    path: str,
) -> None:
    """Raise HTTPException(403) when *actor* may not access *target_dept_id*.

    Rules (mirror the design's RBAC matrix):

    * ``admin`` / ``system`` - allowed for any dept.
    * ``dept_admin`` - allowed only when ``actor.dept_id`` matches
      ``target_dept_id``.  Used for both reads and mutations.
    * ``lead`` / ``viewer`` - denied on mutating endpoints; allowed
      on read endpoints (the AdminProxy classifier already restricts
      these paths to ``dept_admin``+, so this branch is a defence-in-
      depth guard for direct calls).

    On denial the helper writes a single ``rbac_denied`` audit row
    and raises :class:`HTTPException` with status 403.
    """

    role = actor.actor_role

    if role in _PRIVILEGED_ROLES:
        return

    if require_mutating_role:
        # Mutating endpoints require ``dept_admin`` or higher.
        if role not in ("dept_admin",):
            await _audit_rbac_denied(
                deps,
                actor=actor,
                target_dept_id=target_dept_id,
                method=method,
                path=path,
                reason="role_not_privileged",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="role not permitted for this action",
            )
    else:
        # Read endpoints are open to ``dept_admin`` (own dept).
        if role not in _READ_ROLES:
            await _audit_rbac_denied(
                deps,
                actor=actor,
                target_dept_id=target_dept_id,
                method=method,
                path=path,
                reason="role_not_permitted_for_read",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="role not permitted for this action",
            )

    # ``dept_admin`` must match the target dept_id.
    if role == "dept_admin":
        if actor.dept_id is None or actor.dept_id != target_dept_id:
            await _audit_rbac_denied(
                deps,
                actor=actor,
                target_dept_id=target_dept_id,
                method=method,
                path=path,
                reason="dept_scope_mismatch",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="dept_admin scope mismatch",
            )


async def _audit_rbac_denied(
    deps: DeptCredentialEndpointDeps,
    *,
    actor: _Actor,
    target_dept_id: str | None,
    method: str,
    path: str,
    reason: str,
) -> None:
    """Write a single ``rbac_denied`` audit row.

    The router never raises from this helper - audit writes are
    best-effort so a transient sink failure does not turn a clean
    403 into a 500.
    """

    role: Literal["admin", "system", "dept_admin"] = _audit_role(
        actor.actor_role
    )
    try:
        await deps.audit_logger.write(
            AuditEvent(
                actor_id=actor.actor_id,
                actor_role=role,
                dept_id=target_dept_id,
                action="rbac_denied",
                resource=f"{method} {path}",
                result="denied",
                timestamp=_now(deps),
                payload={
                    "reason": reason,
                    "actor_role": actor.actor_role,
                    "actor_dept_id": actor.dept_id,
                    "target_dept_id": target_dept_id,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 - audit failure is non-fatal
        _LOG.warning(
            "dept_credentials.rbac_denied_audit_failed err=%s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/admin", tags=["dept-credentials"])


# ---------------------------------------------------------------------------
# Body validation helpers
# ---------------------------------------------------------------------------


def _parse_credential_body(
    body: Mapping[str, Any],
    *,
    dept_id: str,
    service: ProbeService,
) -> AddCredentialRequest:
    """Translate a JSON body into an :class:`AddCredentialRequest`.

    Enforces only the structural pre-conditions the orchestrator
    does not itself validate - the orchestrator runs its own
    ``_validate_request`` guard before any side-effects.
    """

    if not isinstance(body, Mapping):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body must be a JSON object",
        )

    url = body.get("url")
    username = body.get("username")
    token = body.get("personal_token")
    account_id = body.get("account_id")
    deployment = body.get("deployment")

    if not isinstance(url, str) or not url:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="url is required and must be a non-empty string",
        )
    if not isinstance(username, str) or not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="username is required and must be a non-empty string",
        )
    if not isinstance(token, (str, bytes, bytearray)) or not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="personal_token is required and must be non-empty",
        )

    if isinstance(token, str):
        token_buf = bytearray(token.encode("utf-8"))
    elif isinstance(token, bytes):
        token_buf = bytearray(token)
    else:
        token_buf = bytearray(token)

    if account_id is not None and not isinstance(account_id, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="account_id, when present, must be a string",
        )

    if deployment is not None:
        if deployment not in ("cloud", "server", "dc"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="deployment must be one of 'cloud', 'server', 'dc'",
            )

    return AddCredentialRequest(
        dept_id=dept_id,
        service=service,
        url=url,
        username=username,
        personal_token=token_buf,
        account_id=account_id,
        deployment=deployment,  # type: ignore[arg-type]
        probe_targets=_parse_probe_targets(body, url=url),
    )


def _parse_probe_targets(
    body: Mapping[str, Any],
    *,
    url: str,
) -> ProbeTargets | None:
    raw_targets = body.get("probe_targets")
    target_map = raw_targets if isinstance(raw_targets, Mapping) else {}

    bitbucket_workspace = _first_str(
        body.get("bitbucket_workspace"),
        body.get("workspace"),
        target_map.get("bitbucket_workspace"),
        target_map.get("workspace"),
    )
    bitbucket_repo = _first_str(
        body.get("bitbucket_repo"),
        body.get("repo"),
        body.get("repo_slug"),
        target_map.get("bitbucket_repo"),
        target_map.get("repo"),
        target_map.get("repo_slug"),
    )
    if not bitbucket_workspace or not bitbucket_repo:
        parsed_workspace, parsed_repo = _parse_bitbucket_url(url)
        bitbucket_workspace = bitbucket_workspace or parsed_workspace
        bitbucket_repo = bitbucket_repo or parsed_repo

    confluence_space_key = _first_str(
        body.get("confluence_space_key"),
        body.get("space_key"),
        target_map.get("confluence_space_key"),
        target_map.get("space_key"),
    )

    if not any((bitbucket_workspace, bitbucket_repo, confluence_space_key)):
        return None
    return ProbeTargets(
        bitbucket_workspace=bitbucket_workspace,
        bitbucket_repo=bitbucket_repo,
        confluence_space_key=confluence_space_key,
    )


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_bitbucket_url(url: str) -> tuple[str | None, str | None]:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def _validate_service_path_param(service: str) -> ProbeService:
    """Validate the ``{service}`` path segment.

    The AdminProxy classifier admits any dept-scoped path, so the
    service segment reaches the router unchecked - the closed set
    must be enforced here.
    """

    if service not in VALID_SERVICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"service must be one of {sorted(VALID_SERVICES)!r}; "
                f"got {service!r}"
            ),
        )
    return service  # type: ignore[return-value]


def _validate_dept_path_param(dept_id: str) -> str:
    """Validate the ``{id}`` path segment via the staging helper."""

    try:
        return validate_dept_id(dept_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# GET /admin/departments
# ---------------------------------------------------------------------------


@router.get("/departments")
async def list_departments(
    request: Request,
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """List every department with mask edilmiş bot credential refs.

    ``admin`` / ``system`` see every department; ``dept_admin`` sees
    only the dept whose id matches ``X-Actor-Dept-Id``.  ``lead`` /
    ``viewer`` are denied at the AdminProxy level for ``/admin/*``;
    the router-side check is a defence-in-depth guard.

    Response shape:

    .. code-block:: json

        {
          "departments": [
            {
              "id": "...",
              "display_name": "...",
              "mode": "active",
              "services": ["jira", "confluence"],
              "credential_refs": {
                "jira": "vault:atlassian/<id>/jira",
                "confluence": "vault:atlassian/<id>/confluence"
              },
              "last_probe_at": null
            }
          ]
        }

    """

    actor = _extract_actor(request)
    if actor.actor_role not in _READ_ROLES:
        await _audit_rbac_denied(
            deps,
            actor=actor,
            target_dept_id=None,
            method="GET",
            path="/admin/departments",
            reason="role_not_permitted_for_read",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="role not permitted for this action",
        )

    rows = await _select_departments(
        deps,
        actor=actor,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"departments": rows},
    )


# ---------------------------------------------------------------------------
# GET /admin/departments/{id}
# ---------------------------------------------------------------------------


@router.get("/departments/{dept_id}")
async def get_department(
    dept_id: str,
    request: Request,
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Return the full detail for a single department.

    Response shape mirrors :func:`list_departments` plus the
    per-dept ``jira_project_keys`` / ``confluence_space_keys`` /
    ``bots`` arrays.

    """

    validated = _validate_dept_path_param(dept_id)
    actor = _extract_actor(request)
    await _enforce_dept_scope(
        deps,
        actor,
        validated,
        require_mutating_role=False,
        method="GET",
        path=f"/admin/departments/{validated}",
    )

    detail = await _select_department_detail(deps, dept_id=validated)
    if detail is None and await _ensure_department_seeded_from_config(
        deps, validated
    ):
        detail = await _select_department_detail(deps, dept_id=validated)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department {validated!r} not found",
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=detail)


# ---------------------------------------------------------------------------
# POST /admin/departments/{id}/credentials/{service}
# ---------------------------------------------------------------------------


@router.post(
    "/departments/{dept_id}/credentials/{service}",
    status_code=status.HTTP_200_OK,
)
async def add_or_update_credential(
    dept_id: str,
    service: str,
    request: Request,
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Atomic add or update for the ``(dept_id, service)`` bot credential.

    Body shape:

    .. code-block:: json

        {
          "url": "https://...",
          "username": "...",
          "personal_token": "<plain-text>",
          "account_id": "...",      // optional
          "deployment": "cloud"     // optional, bitbucket only
        }

    Returns 200 with the orchestrator's
    :class:`AddCredentialResult` projection on success; 502 with a
    ``{"error": "...", "detail": "..."}`` body when any step fails
    (the orchestrator has already cleaned up staging Vault keys
    and rolled the SQL transaction back).

    """

    validated_dept = _validate_dept_path_param(dept_id)
    validated_service = _validate_service_path_param(service)

    actor = _extract_actor(request)
    await _enforce_dept_scope(
        deps,
        actor,
        validated_dept,
        require_mutating_role=True,
        method="POST",
        path=(
            f"/admin/departments/{validated_dept}/"
            f"credentials/{validated_service}"
        ),
    )

    await _ensure_department_seeded_from_config(deps, validated_dept)

    body = await request.json()
    create_request = _parse_credential_body(
        body, dept_id=validated_dept, service=validated_service
    )

    try:
        result = await deps.service.add_or_update(
            create_request,
            actor_id=actor.actor_id,
            actor_role=_audit_role(actor.actor_role),
        )
    except DepartmentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department {validated_dept!r} not found",
        )
    except DeptCredentialOperationError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": exc.reason,
                "service": exc.service,
                "detail": exc.detail,
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_serialise_add_result(result),
    )


# ---------------------------------------------------------------------------
# DELETE /admin/departments/{id}/credentials/{service}
# ---------------------------------------------------------------------------


@router.delete(
    "/departments/{dept_id}/credentials/{service}",
    status_code=status.HTTP_200_OK,
)
async def remove_credential(
    dept_id: str,
    service: str,
    request: Request,
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Idempotent remove of the ``(dept_id, service)`` bot credential.

    Returns 200 ``{"status": "removed", "existed": <bool>}`` on
    success.  Returns 404 when the department itself does not
    exist; returns 502 + ``{"error": "...", "detail": "..."}`` on
    unexpected DB or Vault failures (the orchestrator has already
    written a ``dept_credential_add_failed`` audit row carrying
    the reason).

    """

    validated_dept = _validate_dept_path_param(dept_id)
    validated_service = _validate_service_path_param(service)

    actor = _extract_actor(request)
    await _enforce_dept_scope(
        deps,
        actor,
        validated_dept,
        require_mutating_role=True,
        method="DELETE",
        path=(
            f"/admin/departments/{validated_dept}/"
            f"credentials/{validated_service}"
        ),
    )

    try:
        result: RemoveCredentialResult = await deps.service.remove(
            dept_id=validated_dept,
            service=validated_service,
            actor_id=actor.actor_id,
            actor_role=_audit_role(actor.actor_role),
        )
    except DepartmentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department {validated_dept!r} not found",
        )
    except DeptCredentialOperationError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": exc.reason,
                "service": exc.service,
                "detail": exc.detail,
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "removed",
            "dept_id": result.dept_id,
            "service": result.service,
            "existed": result.existed,
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/departments/{id}/probe
# ---------------------------------------------------------------------------


@router.post("/departments/{dept_id}/probe", status_code=status.HTTP_200_OK)
async def probe_credentials(
    dept_id: str,
    request: Request,
    service: str | None = Query(default=None),
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Re-run the connectivity probe for one or all dept bots.

    Query parameter ``service`` is optional; when omitted the probe
    is run against every ``(dept_id, service)`` row registered for
    the department.

    Response shape:

    .. code-block:: json

        {
          "results": [
            {"service": "jira", "status": "ok", "account_id": "..."},
            {"service": "confluence", "status": "failed",
             "error": "401 unauthorised"}
          ],
          "probed_at": "2025-01-01T00:00:00+00:00"
        }

    """

    validated_dept = _validate_dept_path_param(dept_id)
    target_service: ProbeService | None = None
    if service is not None:
        target_service = _validate_service_path_param(service)

    actor = _extract_actor(request)
    await _enforce_dept_scope(
        deps,
        actor,
        validated_dept,
        require_mutating_role=True,
        method="POST",
        path=f"/admin/departments/{validated_dept}/probe",
    )

    await _ensure_department_seeded_from_config(deps, validated_dept)

    try:
        outcome: ProbeRunOutcome = await deps.service.probe(
            dept_id=validated_dept,
            service=target_service,
            actor_id=actor.actor_id,
            actor_role=_audit_role(actor.actor_role),
        )
    except DepartmentNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department {validated_dept!r} not found",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_serialise_probe_outcome(outcome),
    )


# ---------------------------------------------------------------------------
# POST /admin/departments/bulk-import
# ---------------------------------------------------------------------------


def _get_bulk_import_service(request: Request) -> BulkImportService:
    """Pull the :class:`BulkImportService` off ``app.state``.

    Surfaces a 500 if the application factory neglected to wire the
    service - making the deployment misconfiguration explicit.
    """

    svc = getattr(request.app.state, "bulk_import_service", None)
    if not isinstance(svc, BulkImportService):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="bulk_import_service is not wired "
            "(app.state.bulk_import_service missing)",
        )
    return svc


@router.post("/departments/bulk-import")
async def bulk_import_departments(
    request: Request,
    file: UploadFile = File(..., description="JSON file conforming to departments.schema.json"),
    dry_run: bool = Form(default=True, description="When true, validate only without committing"),
    deps: DeptCredentialEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Bulk-import departments from a JSON file upload.

    Accepts a multipart form with:
    - ``file``: JSON file conforming to ``departments.schema.json``.
    - ``dry_run``: Boolean flag (default ``true``). When true, only
      validates and simulates probes without writing state.

    Response shape:

    .. code-block:: json

        {
          "txn_id": "...",
          "total": 5,
          "validated": [...],
          "imported": [...],
          "failed": [...],
          "probe_results": [...],
          "dry_run": true
        }

    Returns HTTP 200 when all departments succeed (or dry-run).
    Returns HTTP 207 Multi-Status when some departments fail and
    others succeed (partial import).
    Returns HTTP 422 when the JSON file fails schema validation.

    Plain-text tokens are **never** written to audit; the existing
    :class:`RedactionFilter` masks them in log output.

    """

    # RBAC: only admin / system may bulk-import
    actor = _extract_actor(request)
    if actor.actor_role not in _PRIVILEGED_ROLES:
        await _audit_rbac_denied(
            deps,
            actor=actor,
            target_dept_id=None,
            method="POST",
            path="/admin/departments/bulk-import",
            reason="role_not_privileged",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only admin or system roles may perform bulk import",
        )

    # Read the uploaded file content
    file_content = await file.read()
    if not file_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="uploaded file is empty",
        )

    # Resolve the bulk import service
    bulk_svc = _get_bulk_import_service(request)

    # Execute the bulk import
    try:
        result: BulkImportResult = await bulk_svc.bulk_import(
            file_content=file_content,
            dry_run=dry_run,
            actor_id=actor.actor_id,
            actor_role=_audit_role(actor.actor_role),  # type: ignore[arg-type]
        )
    except SchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "schema_validation_failed",
                "validation_errors": exc.errors,
            },
        )

    # Build the response payload
    response_body = _serialise_bulk_import_result(result)

    # Determine HTTP status: 200 for full success / dry-run, 207 for partial
    if result.dry_run:
        http_status = status.HTTP_200_OK
    elif result.failed and result.imported:
        # Partial success: some imported, some failed
        http_status = 207  # Multi-Status
    elif result.failed and not result.imported:
        # All failed - still return 207 so the caller can inspect per-dept errors
        http_status = 207
    else:
        # All succeeded
        http_status = status.HTTP_200_OK

    return JSONResponse(
        status_code=http_status,
        content=response_body,
    )


# ---------------------------------------------------------------------------
# Read-side SQL helpers
# ---------------------------------------------------------------------------


async def _select_departments(
    deps: DeptCredentialEndpointDeps,
    *,
    actor: _Actor,
) -> list[dict[str, Any]]:
    """Read every (or the actor's) department + bot rows.

    For ``dept_admin`` callers we narrow the result set to a single
    dept whose id matches ``actor.dept_id`` - keeping the API
    consistent with the per-dept RBAC model rather than exposing
    the whole catalogue and relying on the UI to filter.
    """

    role_for_session: Literal["admin", "dept_admin", "system"] = (
        "admin" if actor.actor_role in _PRIVILEGED_ROLES else "dept_admin"
    )
    scope_dept_id: str = (
        actor.dept_id
        if actor.actor_role == "dept_admin" and actor.dept_id is not None
        else "system"
    )

    connection = await deps.connection_factory()
    async with with_dept_session(
        role_for_session, scope_dept_id, connection=connection
    ) as conn:
        if actor.actor_role == "dept_admin" and actor.dept_id is not None:
            dept_rows = await _fetch(
                conn,
                """
                SELECT id, display_name, mode
                  FROM automation.departments
                 WHERE id = $1
                 ORDER BY id
                """,
                actor.dept_id,
            )
            bot_rows = await _fetch(
                conn,
                """
                SELECT department_id, service, credential_ref,
                       account_id, username, deployment
                  FROM automation.department_bots
                 WHERE department_id = $1
                 ORDER BY department_id, service
                """,
                actor.dept_id,
            )
        else:
            dept_rows = await _fetch(
                conn,
                """
                SELECT id, display_name, mode
                  FROM automation.departments
                 ORDER BY id
                """,
            )
            bot_rows = await _fetch(
                conn,
                """
                SELECT department_id, service, credential_ref,
                       account_id, username, deployment
                  FROM automation.department_bots
                 ORDER BY department_id, service
                """,
            )

    bots_by_dept: dict[str, list[Mapping[str, Any]]] = {}
    for row in bot_rows:
        bots_by_dept.setdefault(row["department_id"], []).append(row)

    out: list[dict[str, Any]] = []
    for dept in dept_rows:
        bots = bots_by_dept.get(dept["id"], [])
        out.append(
            {
                "id": dept["id"],
                "display_name": dept["display_name"],
                "mode": dept["mode"],
                "services": sorted(b["service"] for b in bots),
                "credential_refs": {
                    b["service"]: b["credential_ref"] for b in bots
                },
                "last_probe_at": None,  # populated after a successful probe
            }
        )
    return out


async def _select_department_detail(
    deps: DeptCredentialEndpointDeps,
    *,
    dept_id: str,
) -> dict[str, Any] | None:
    """Read a single department + its bots / project keys / space keys."""

    connection = await deps.connection_factory()
    async with with_dept_session(
        "admin", dept_id, connection=connection
    ) as conn:
        dept = await _fetchrow(
            conn,
            """
            SELECT id, display_name, default_language,
                   web_search_enabled, mode, created_at, updated_at
              FROM automation.departments
             WHERE id = $1
            """,
            dept_id,
        )
        if dept is None:
            return None

        bots = await _fetch(
            conn,
            """
            SELECT department_id, service, credential_ref,
                   account_id, username, deployment
              FROM automation.department_bots
             WHERE department_id = $1
             ORDER BY service
            """,
            dept_id,
        )

        project_keys = await _fetch(
            conn,
            """
            SELECT project_key
              FROM automation.department_project_keys
             WHERE department_id = $1
             ORDER BY project_key
            """,
            dept_id,
        )

        space_keys = await _fetch(
            conn,
            """
            SELECT space_key
              FROM automation.department_space_keys
             WHERE department_id = $1
             ORDER BY space_key
            """,
            dept_id,
        )

    return {
        "id": dept["id"],
        "display_name": dept["display_name"],
        "default_language": dept["default_language"],
        "web_search_enabled": bool(dept["web_search_enabled"]),
        "mode": dept["mode"],
        "created_at": _iso(dept["created_at"]),
        "updated_at": _iso(dept["updated_at"]),
        "jira_project_keys": [r["project_key"] for r in project_keys],
        "confluence_space_keys": [r["space_key"] for r in space_keys],
        "bots": [
            {
                "service": b["service"],
                "credential_ref": b["credential_ref"],
                "account_id": b["account_id"],
                "username": b["username"],
                "deployment": b["deployment"],
            }
            for b in bots
        ],
    }


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialise_add_result(result: AddCredentialResult) -> dict[str, Any]:
    return {
        "dept_id": result.dept_id,
        "service": result.service,
        "account_id": result.account_id,
        "last_probe_at": _iso(result.last_probe_at),
        "vault_path": result.vault_path,
        "outcome": result.outcome,
    }


def _serialise_probe_outcome(outcome: ProbeRunOutcome) -> dict[str, Any]:
    return {
        "dept_id": outcome.dept_id,
        "results": [
            {
                "service": r.service,
                "status": r.status,
                "error": r.error,
                "account_id": r.account_id,
            }
            for r in outcome.results
        ],
        "probed_at": _iso(outcome.probed_at),
    }


def _serialise_bulk_import_result(result: BulkImportResult) -> dict[str, Any]:
    """Serialise a :class:`BulkImportResult` to a JSON-safe dict.

    The response shape matches the design contract:
    ``{txn_id, total, validated, imported, failed, probe_results, dry_run}``.

    Plain-text tokens are never included in the response - the service
    layer already strips them; this serialiser only exposes dept_id,
    status, error, and per-service probe outcomes.
    """

    def _dept_outcome(o: DeptImportOutcome) -> dict[str, Any]:
        return {
            "dept_id": o.dept_id,
            "status": o.status,
            "error": o.error,
            "probe_results": [
                {
                    "service": p.service,
                    "status": p.status,
                    "error": p.error,
                }
                for p in o.probe_results
            ],
        }

    # Aggregate all probe results across all departments for the
    # top-level ``probe_results`` field.
    all_probe_results: list[dict[str, Any]] = []
    for outcomes in (result.validated, result.imported, result.failed):
        for o in outcomes:
            for p in o.probe_results:
                all_probe_results.append(
                    {
                        "dept_id": o.dept_id,
                        "service": p.service,
                        "status": p.status,
                        "error": p.error,
                    }
                )

    return {
        "txn_id": result.txn_id,
        "total": result.total,
        "validated": [_dept_outcome(o) for o in result.validated],
        "imported": [_dept_outcome(o) for o in result.imported],
        "failed": [_dept_outcome(o) for o in result.failed],
        "probe_results": all_probe_results,
        "dry_run": result.dry_run,
    }


def _iso(value: Any) -> str | None:
    """Return ``value.isoformat()`` when possible, else ``str(value)`` / ``None``."""

    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Async DB helpers - protocol-agnostic shims
# ---------------------------------------------------------------------------


async def _fetchrow(
    conn: AsyncConnection,
    query: str,
    *args: Any,
) -> Any:
    """Compatibility shim - see :meth:`DeptCredentialService._fetchrow`."""

    fetchrow = getattr(conn, "fetchrow", None)
    if fetchrow is None:
        await conn.execute(query, *args)
        return None
    return await fetchrow(query, *args)


async def _fetch(
    conn: AsyncConnection,
    query: str,
    *args: Any,
) -> Sequence[Mapping[str, Any]]:
    """Compatibility shim - see :meth:`DeptCredentialService._fetch`."""

    fetch = getattr(conn, "fetch", None)
    if fetch is None:
        await conn.execute(query, *args)
        return ()
    return await fetch(query, *args)
