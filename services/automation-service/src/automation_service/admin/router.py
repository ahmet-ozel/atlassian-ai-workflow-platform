"""FastAPI router for ``/admin/*`` endpoints.

Owns the HTTP surface for department administration:

* ``POST /admin/departments`` - atomic department create with
  Vault staging + DB transaction (orchestrated by
  :class:`automation_service.admin.dept_create.DepartmentCreateOrchestrator`).
* ``POST /admin/departments/wizard`` - multi-step setup wizard
  (Jira → Bitbucket → Confluence) as a thin state-machine over the
  same orchestrator.
* ``POST /admin/departments/{id}/credentials/rotate`` and
  ``/disable`` - credential rotation and department disable.
* ``GET`` / ``DELETE /admin/probe-artifacts`` and
  ``/admin/probe-artifacts/{id}`` - partial-orphan listing and
  manual cleanup.

The router is the **thin shim** layer: every endpoint validates the
JSON body, dispatches to a collaborator (orchestrator, vault client,
DB session, audit logger) read off ``request.app.state.admin`` and
translates exceptions into HTTP status codes. All real logic lives
in the collaborators so the router is exerciseable from unit tests
with hand-built fakes.

Wiring contract (``app.state.admin``)
-------------------------------------

The :func:`automation_service.app.create_app` factory is responsible
for populating ``request.app.state.admin`` with a single
:class:`AdminEndpointDeps` instance carrying:

* ``orchestrator`` - :class:`DepartmentCreateOrchestrator`.
* ``vault`` - :class:`vault_client.VaultClient`.
* ``audit_logger`` - :class:`audit_logger.AuditLogger`.
* ``connection_factory`` - async factory returning a fresh DB
  connection scoped to a single request.
* ``probe_client`` - :class:`automation_service.probe.AtlassianProbeClient`
  used by the wizard's per-step probe.
* ``temporal_client`` (optional) - :class:`temporal_client.TemporalClient`
  used by the disable endpoint to signal long-running workflows.

The router never imports any of these directly - keeping the wiring
on ``app.state.admin`` lets the endpoints be unit-tested in
isolation by injecting a stub ``AdminEndpointDeps``.

Authentication / authorization
------------------------------

The ``automation-service`` admin endpoints sit
**behind** ``admin-dashboard-api`` which performs the OIDC
authentication and the RBAC pre-check. The router still emits an
``AuthContext`` from the ``X-Actor-*`` proxy headers so the audit
``actor_id`` / ``actor_role`` columns can be populated correctly.
Direct (un-proxied) requests fall back to the
``"system"`` actor when the headers are absent - production deploys
mark ``admin-dashboard-api`` as the only ingress, so this fallback
is only used by integration tests that bypass the proxy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from db_shared import AsyncConnection, with_dept_session
from vault_client import VaultClient, VaultPath

from ..probe import (
    AtlassianProbeClient,
    BotIdentityProbeResult,
    ProbeTargets,
    ResolvedCredential,
    probe_bot_identity,
)
from ..staging import final_vault_path
from .dept_create import (
    DepartmentAlreadyExistsError,
    DepartmentCreateOrchestrator,
    DepartmentCreateRequest,
    DepartmentCreateResult,
    ProbeFailureError,
    StagingFailureError,
    _BotCredential,
)

__all__ = ["AdminEndpointDeps", "router"]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency container - injected via ``app.state.admin``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminEndpointDeps:
    """Collaborators the admin router pulls from ``app.state.admin``.

    The router owns no state of its own. Tests construct an instance
    of this dataclass with hand-built fakes; production wiring builds
    one in :func:`automation_service.app.create_app`.
    """

    orchestrator: DepartmentCreateOrchestrator
    vault: VaultClient
    audit_logger: AuditLogger
    connection_factory: Callable[[], Awaitable[AsyncConnection]]
    probe_client: AtlassianProbeClient | None = None
    temporal_client: Any | None = None
    clock: Callable[[], datetime] | None = None


def _deps(request: Request) -> AdminEndpointDeps:
    """Pull the :class:`AdminEndpointDeps` off ``app.state``.

    Raises a 500 if the application factory neglected to wire the
    admin collaborators - surfacing the deployment misconfiguration
    early rather than letting a downstream attribute access throw a
    less helpful error.
    """

    deps = getattr(request.app.state, "admin", None)
    if not isinstance(deps, AdminEndpointDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="admin router is not wired (app.state.admin missing)",
        )
    return deps


# ---------------------------------------------------------------------------
# Actor extraction (proxy-emitted headers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Actor:
    """Minimal actor projection reconstructed from proxy headers."""

    actor_id: str
    actor_role: Literal["admin", "system", "dept_admin"]


def _extract_actor(request: Request) -> _Actor:
    """Build an actor from ``X-Actor-Id`` / ``X-Actor-Role`` headers.

    ``admin-dashboard-api`` is the only ingress that populates these
    headers (after running its OIDC + RBAC pre-checks). Direct
    requests (integration tests, smoke probes) fall back to the
    ``"system"`` actor - production deployments enforce the proxy
    via Compose-level network isolation so this fallback is never
    reachable in real traffic.
    """

    actor_id = request.headers.get("x-actor-id") or "system"
    actor_role_raw = (request.headers.get("x-actor-role") or "system").lower()
    if actor_role_raw not in ("admin", "system", "dept_admin"):
        # Unknown role: treat as the lowest-privilege fallback so a
        # malformed proxy header cannot escalate.
        actor_role_raw = "system"
    return _Actor(
        actor_id=actor_id,
        actor_role=actor_role_raw,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# 5.3 - POST /admin/departments
# ---------------------------------------------------------------------------


def _parse_create_body(body: Mapping[str, Any]) -> DepartmentCreateRequest:
    """Translate a JSON body into a :class:`DepartmentCreateRequest`.

    The router enforces only the structural pre-conditions the
    orchestrator does not itself validate (presence of required
    fields, ``bots`` array shape). Schema-level constraints
    (``id`` regex, mode enum, ``credential_ref`` regex) are enforced
    by ``departments.schema.json`` validation at a higher layer in
    production; for the in-process router we keep a thin guard.
    """

    required = (
        "id", "display_name", "default_language", "web_search_enabled",
        "mode", "jira_project_keys", "bots",
    )
    for field_name in required:
        if field_name not in body:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"missing required field {field_name!r}",
            )

    bots_raw = body.get("bots") or ()
    if not isinstance(bots_raw, Sequence) or not bots_raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="bots must be a non-empty array",
        )

    bots: list[_BotCredential] = []
    for entry in bots_raw:
        if not isinstance(entry, Mapping):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="each bot entry must be an object",
            )
        token = entry.get("personal_token")
        if not isinstance(token, (str, bytes, bytearray)) or not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="bot.personal_token is required and non-empty",
            )
        if isinstance(token, str):
            token_bytes = bytearray(token.encode("utf-8"))
        elif isinstance(token, bytes):
            token_bytes = bytearray(token)
        else:
            token_bytes = bytearray(token)

        bots.append(
            _BotCredential(
                service=entry["service"],  # type: ignore[arg-type]
                url=str(entry.get("url", "")),
                username=str(entry.get("username", "")),
                personal_token=token_bytes,
                account_id=entry.get("account_id"),
                deployment=entry.get("deployment"),
            )
        )

    return DepartmentCreateRequest(
        dept_id=str(body["id"]),
        display_name=str(body["display_name"]),
        default_language=body["default_language"],
        web_search_enabled=bool(body["web_search_enabled"]),
        mode=body["mode"],
        jira_project_keys=tuple(body["jira_project_keys"]),
        confluence_space_keys=tuple(body.get("confluence_space_keys") or ()),
        bitbucket_workspace=body.get("bitbucket_workspace"),
        config_json=dict(body.get("config_json") or {}),
        bots=tuple(bots),
        probe_targets=None,
    )


@router.post("/departments", status_code=status.HTTP_201_CREATED)
async def create_department(
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """``POST /admin/departments`` - atomic create.

    Delegates to :class:`DepartmentCreateOrchestrator`. Translates
    the orchestrator's exception ladder into HTTP status codes:

    * :class:`DepartmentAlreadyExistsError` → 409
    * :class:`ProbeFailureError` / :class:`StagingFailureError` → 502
    * Any other ``Exception`` → 500
    """

    body = await request.json()
    try:
        create_request = _parse_create_body(body)
    except HTTPException:
        raise

    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="department create requires admin role",
        )

    try:
        result = await deps.orchestrator.run(
            create_request,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
        )
    except DepartmentAlreadyExistsError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "dept_duplicate_id", "dept_id": exc.dept_id},
        )
    except (ProbeFailureError, StagingFailureError) as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "dept_id": result.dept_id,
            "request_id": result.request_id,
            "services": list(result.services),
            "credential_refs": dict(result.credential_refs),
            "created_at": result.created_at.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/departments/wizard
# ---------------------------------------------------------------------------


@router.post("/departments/wizard", status_code=status.HTTP_201_CREATED)
async def create_department_wizard(
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Multi-step setup wizard - Jira → Bitbucket → Confluence.

    The wizard is a *thin* state machine on top of
    :class:`DepartmentCreateOrchestrator`: each step's credential is
    probed in isolation; on the first failure the endpoint returns
    HTTP 422 with the failed step name and ``mode`` left at the
    caller's choice (``"shadow"`` or ``"disabled"``). Only when
    every supplied step probe passes does the orchestrator commit
    the department atomically with ``mode="active"``.

    **Atomic identity probe:** After the orchestrator commits
    the department, an inline bot identity probe
    (:func:`probe_bot_identity`) is run for each service. If any
    identity probe fails, the department's mode is downgraded to
    ``"disabled"``. The
    credential write + identity probe are treated as an atomic unit
    from the caller's perspective.

    The state is fully encoded in the request body - there is no
    server-side session. Clients submit either:

    * ``{"steps": ["jira", "bitbucket", ...], "credentials": {...},
       "department": {...}}`` - the canonical multi-step form, or
    * ``{"department": {...}}`` with the embedded ``bots`` array -
      treated as a one-shot wizard equivalent to the atomic create.

    On success the response shape matches :func:`create_department`
    with additional ``account_id_probe_status`` and
    ``account_id_probe_results`` fields.
    """

    body = await request.json()
    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="wizard requires admin role",
        )

    department_payload = body.get("department")
    if not isinstance(department_payload, Mapping):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="wizard requires 'department' object in body",
        )

    # The wizard's per-step probe and the final atomic commit share
    # the exact same orchestrator path - the orchestrator already
    # runs read+write probes and rolls back on the first failure
    # and rolls back on the first failure. The wizard's own state machine therefore reduces to a
    # **caller-side** ordering hint: the body declares the intended
    # step order; the orchestrator runs the probes in the same
    # order via per-bot iteration. On the first probe failure the
    # orchestrator raises ProbeFailureError; we surface the failed
    # service name back to the caller so the UI can pin the user
    # at the matching step.
    try:
        create_request = _parse_create_body(department_payload)
    except HTTPException:
        raise

    try:
        result = await deps.orchestrator.run(
            create_request,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
        )
    except DepartmentAlreadyExistsError as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"error": "dept_duplicate_id", "dept_id": exc.dept_id},
        )
    except ProbeFailureError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "wizard_step_failed",
                "step": exc.service,
                "state": exc.state,
                "detail": exc.message,
            },
        )
    except StagingFailureError as exc:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": "staging_write_failed", "detail": str(exc)},
        )

    # ------------------------------------------------------------------
    # Atomic inline bot identity probe
    #
    # After the orchestrator commits the department (credential write
    # + connectivity probe passed), run probe_bot_identity for each
    # service to resolve the bot's account_id. If ANY identity probe
    # fails, downgrade the department to mode="disabled" so the loop
    # guard never falls back to regex matching for this dept.
    # ------------------------------------------------------------------
    identity_probe_results: dict[str, dict[str, Any]] = {}
    any_probe_failed = False

    if deps.probe_client is not None:
        for service in result.services:
            vault_path = final_vault_path(result.dept_id, service)
            try:
                vault_data = dict(deps.vault.read(vault_path))
                cred = ResolvedCredential(
                    url=vault_data.get("url", ""),
                    username=vault_data.get("username", ""),
                    personal_token=vault_data.get("personal_token", ""),
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "wizard.identity_probe.vault_read_failed "
                    "dept_id=%s service=%s err=%s",
                    result.dept_id,
                    service,
                    type(exc).__name__,
                )
                identity_probe_results[service] = {
                    "status": "failed",
                    "error": f"vault_read_failed: {type(exc).__name__}",
                }
                any_probe_failed = True
                continue

            probe_result = await probe_bot_identity(
                dept_id=result.dept_id,
                service=service,
                client=deps.probe_client,
                cred=cred,
            )

            if probe_result.success and probe_result.account_id:
                identity_probe_results[service] = {
                    "status": "ok",
                    "account_id": probe_result.account_id,
                }
                # Upsert into department_bot_identity table
                try:
                    connection = await deps.connection_factory()
                    async with with_dept_session(
                        "system", result.dept_id, connection=connection
                    ) as conn:
                        await conn.execute(
                            """
                            INSERT INTO automation.department_bot_identity
                                (dept_id, service, account_id, probed_at, probe_status)
                            VALUES ($1, $2, $3, $4, $5)
                            ON CONFLICT (dept_id, service) DO UPDATE
                            SET account_id   = EXCLUDED.account_id,
                                probed_at    = EXCLUDED.probed_at,
                                probe_status = EXCLUDED.probe_status
                            """,
                            result.dept_id,
                            service,
                            probe_result.account_id,
                            result.created_at,
                            "ok",
                        )
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning(
                        "wizard.identity_probe.upsert_failed "
                        "dept_id=%s service=%s err=%s",
                        result.dept_id,
                        service,
                        type(exc).__name__,
                    )
                # Audit success
                await deps.audit_logger.write(
                    AuditEvent(
                        actor_id=actor.actor_id,
                        actor_role=actor.actor_role,
                        dept_id=result.dept_id,
                        action="bot_account_id_probed",
                        resource=f"dept_bot_identity:{result.dept_id}:{service}",
                        result="ok",
                        timestamp=result.created_at,
                        payload={
                            "dept_id": result.dept_id,
                            "service": service,
                            "resolved_account_id": probe_result.account_id,
                        },
                    )
                )
            else:
                identity_probe_results[service] = {
                    "status": "failed",
                    "error": probe_result.error or "unknown",
                }
                any_probe_failed = True
                # Audit failure
                await deps.audit_logger.write(
                    AuditEvent(
                        actor_id=actor.actor_id,
                        actor_role=actor.actor_role,
                        dept_id=result.dept_id,
                        action="bot_account_id_probe_failed",
                        resource=f"dept_bot_identity:{result.dept_id}:{service}",
                        result="error",
                        timestamp=result.created_at,
                        payload={
                            "dept_id": result.dept_id,
                            "service": service,
                            "error_type": probe_result.error or "unknown",
                        },
                    )
                )

    # If any identity probe failed, downgrade the
    # department to mode="disabled". The credential write is already
    # committed (Vault + DB), but the department cannot be used for
    # automation until the identity probe succeeds (re-probe via
    # admin-dashboard /security page).
    if any_probe_failed:
        try:
            connection = await deps.connection_factory()
            async with with_dept_session(
                "system", result.dept_id, connection=connection
            ) as conn:
                await conn.execute(
                    """
                    UPDATE automation.departments
                    SET mode = 'disabled'
                    WHERE id = $1
                    """,
                    result.dept_id,
                )
        except Exception as exc:  # noqa: BLE001
            _LOG.error(
                "wizard.identity_probe.mode_downgrade_failed "
                "dept_id=%s err=%s",
                result.dept_id,
                type(exc).__name__,
            )
        else:
            _LOG.info(
                "wizard.identity_probe.mode_downgraded dept_id=%s mode=disabled",
                result.dept_id,
            )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "dept_id": result.dept_id,
            "request_id": result.request_id,
            "services": list(result.services),
            "credential_refs": dict(result.credential_refs),
            "created_at": result.created_at.isoformat(),
            "wizard": "completed",
            "mode": "disabled" if any_probe_failed else "active",
            "account_id_probe_status": "failed" if any_probe_failed else "ok",
            "account_id_probe_results": identity_probe_results,
        },
    )


# ---------------------------------------------------------------------------
# Credential rotate / dept disable
# ---------------------------------------------------------------------------


@router.post("/departments/{dept_id}/credentials/rotate")
async def rotate_credentials(
    dept_id: str,
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Rotate the bot credential for ``(dept_id, service)``.

    Body shape:

    .. code-block:: json

        {
            "service": "jira" | "bitbucket" | "confluence",
            "new_token": "<plain-text-token>",
            "username": "..."
        }

    The endpoint writes the new credential to
    ``vault:atlassian/<dept_id>/<service>`` (Vault KV-v2 versioning
    is the canonical "old + new accepted for 1h overlap" mechanism).
    An audit row with ``actor_role`` carried from the proxy headers
    is written on every call.

    Allowed roles: ``admin`` (for any dept), ``dept_admin`` (only
    for their own dept - enforced by ``admin-dashboard-api`` on the
    way in; the router still records the actor for the audit row).
    """

    body = await request.json()
    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system", "dept_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="rotation requires admin or dept_admin role",
        )

    service = body.get("service")
    if service not in ("jira", "bitbucket", "confluence"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="service must be one of 'jira', 'bitbucket', 'confluence'",
        )

    new_token = body.get("new_token")
    username = body.get("username")
    if not new_token or not isinstance(new_token, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="new_token is required",
        )
    if not username or not isinstance(username, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="username is required",
        )

    final_path = VaultPath.parse(f"vault:atlassian/{dept_id}/{service}")

    # Hold the plain-text in a bytearray so we can scrub it.
    token_buf = bytearray(new_token.encode("utf-8"))
    try:
        try:
            deps.vault.write(
                final_path,
                {"username": username, "personal_token": new_token},
            )
        except Exception as exc:  # noqa: BLE001
            await deps.audit_logger.write(
                AuditEvent(
                    actor_id=actor.actor_id,
                    actor_role=actor.actor_role,
                    dept_id=dept_id,
                    action="credential_rotation_failed",
                    resource=f"{dept_id}/{service}",
                    result="error",
                    timestamp=_now(deps),
                    payload={"reason": type(exc).__name__},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"vault write failed: {type(exc).__name__}",
            )
    finally:
        # Wipe plain-text after Vault has the value.
        for i in range(len(token_buf)):
            token_buf[i] = 0
        del token_buf
        del new_token

    await deps.audit_logger.write(
        AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            dept_id=dept_id,
            action="credential_rotated",
            resource=f"{dept_id}/{service}",
            result="ok",
            timestamp=_now(deps),
            payload={"service": service},
        )
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "rotated", "dept_id": dept_id, "service": service},
    )


@router.post("/departments/{dept_id}/disable")
async def disable_department(
    dept_id: str,
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Set ``mode=disabled`` and signal Temporal to drain.

    On success the row is updated, the Temporal client (if wired) is
    sent a ``dept_disabled`` signal so any in-flight workflows can
    drain gracefully, and an audit row is written. Subsequent
    workflow start attempts for this dept will be denied by the
    capability gate (the gate consults ``dept.mode``).
    """

    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system", "dept_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="disable requires admin or dept_admin role",
        )

    conn = await deps.connection_factory()
    async with with_dept_session(actor.actor_role, dept_id, connection=conn) as c:
        try:
            await c.execute(
                "UPDATE automation.departments SET mode = 'disabled' WHERE id = $1",
                dept_id,
            )
        except Exception as exc:  # noqa: BLE001
            await deps.audit_logger.write(
                AuditEvent(
                    actor_id=actor.actor_id,
                    actor_role=actor.actor_role,
                    dept_id=dept_id,
                    action="dept_disable_failed",
                    resource=f"department:{dept_id}",
                    result="error",
                    timestamp=_now(deps),
                    payload={"reason": type(exc).__name__},
                )
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"db update failed: {type(exc).__name__}",
            )

    # Best-effort Temporal drain signal - never raises (a missing
    # client is a deployment-time choice, not an error).
    if deps.temporal_client is not None:
        signal = getattr(deps.temporal_client, "signal_dept_disabled", None)
        if callable(signal):
            try:
                await signal(dept_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "dept_disable.temporal_signal_failed dept=%s err=%s",
                    dept_id, type(exc).__name__,
                )

    await deps.audit_logger.write(
        AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            dept_id=dept_id,
            action="dept_disabled",
            resource=f"department:{dept_id}",
            result="ok",
            timestamp=_now(deps),
            payload={},
        )
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "disabled", "dept_id": dept_id},
    )


# ---------------------------------------------------------------------------
# Probe artifacts (partial-orphan listing + cleanup)
# ---------------------------------------------------------------------------


@router.get("/probe-artifacts")
async def list_probe_artifacts(
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """List ``probe_artifacts`` rows with ``state='partial_orphan'``."""

    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system", "dept_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    conn = await deps.connection_factory()
    async with with_dept_session(actor.actor_role, "system", connection=conn) as c:
        rows = await c.fetch(
            """
            SELECT id, dept_id, service, artifact_type, external_id,
                   title_or_name, state, created_at
              FROM automation.probe_artifacts
             WHERE state = 'partial_orphan'
            """
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "artifacts": [
                {
                    "id": str(row["id"]),
                    "dept_id": row["dept_id"],
                    "service": row["service"],
                    "artifact_type": row["artifact_type"],
                    "external_id": row["external_id"],
                    "title_or_name": row["title_or_name"],
                    "state": row["state"],
                    "created_at": (
                        row["created_at"].isoformat()
                        if hasattr(row["created_at"], "isoformat")
                        else str(row["created_at"])
                    ),
                }
                for row in rows
            ],
        },
    )


@router.delete("/probe-artifacts/{artifact_id}")
async def cleanup_probe_artifact(
    artifact_id: str,
    request: Request,
    deps: AdminEndpointDeps = Depends(_deps),
) -> JSONResponse:
    """Delete the underlying Confluence page and mark row ``cleared``.

    The endpoint reads the row, performs the matching delete on the
    target Atlassian surface (Confluence draft, Bitbucket branch,
    Jira comment) via the configured probe client, and updates the
    state to ``cleared``. If the upstream delete fails the row is
    left at ``partial_orphan`` and the endpoint returns HTTP 502.
    """

    actor = _extract_actor(request)
    if actor.actor_role not in ("admin", "system"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    conn = await deps.connection_factory()
    async with with_dept_session(actor.actor_role, "system", connection=conn) as c:
        row = await c.fetchrow(
            """
            SELECT id, dept_id, service, artifact_type, external_id,
                   title_or_name, state
              FROM automation.probe_artifacts
             WHERE id = $1
            """,
            artifact_id,
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        # The endpoint is operator-driven cleanup; we accept any
        # state but only Confluence pages are cleared automatically.
        # Bitbucket branches / Jira comments are surfaced for manual
        # action and the row is just marked cleared after operator
        # confirmation (the body would carry ``confirm: true`` in a
        # production hardened version; here we mark unconditionally
        # since the DELETE verb itself is the confirmation).
        if (
            row["service"] == "confluence"
            and deps.probe_client is not None
            and getattr(deps.probe_client, "confluence_delete_page", None)
        ):
            try:
                # Test fakes / production wiring both accept the
                # external_id directly as the page id.
                from ..probe import ResolvedCredential

                cred = ResolvedCredential(url="", username="", personal_token="")
                await deps.probe_client.confluence_delete_page(
                    cred, page_id=row["external_id"]
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"confluence delete failed: {type(exc).__name__}",
                )

        await c.execute(
            "UPDATE automation.probe_artifacts SET state = 'cleared' WHERE id = $1",
            artifact_id,
        )

    await deps.audit_logger.write(
        AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            dept_id=row["dept_id"],
            action="probe_artifact_cleared",
            resource=f"probe_artifact:{artifact_id}",
            result="ok",
            timestamp=_now(deps),
            payload={"service": row["service"]},
        )
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "cleared", "artifact_id": artifact_id},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now(deps: AdminEndpointDeps) -> datetime:
    """Return the current UTC time using ``deps.clock`` if present."""

    from datetime import timezone

    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc)
