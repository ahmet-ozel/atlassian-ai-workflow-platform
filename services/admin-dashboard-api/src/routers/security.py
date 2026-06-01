"""``SecurityRouter`` — probe artifacts + rotate banner + secret rotation + SSH runners + webhook secrets.

Five logical surfaces share this module:

* ``GET /admin/security/probe-artifacts`` (`platform-mimari-ops` task
  11.7) — proxies to ``automation-service`` (foundation Q1/Q4 surface)
  so the admin can audit dept connectivity probe history without a
  separate Vault read.
* ``GET /admin/security/credential-rotate-banner`` — local lookup of
  the per-dept TTL banner state. Returns ``{"depts": [...]}`` with
  one entry per dept whose bot credential is within the rotation
  window threshold.
* ``POST /api/v1/security/rotate/webhook_secret`` /
  ``POST /api/v1/security/rotate/bot_credential`` /
  ``POST /api/v1/security/rotate/llm_api_key``
  (`platform-gap-fill` task 15.1, **Validates: Requirements
  15.1–15.5**) — admin-only secret rotation surface. Each endpoint
  writes the new secret material into Vault (KV-v2 retains version
  history), emits a hot-reload signal so dependents invalidate their
  credential caches, and writes one ``secret_rotated`` audit row to
  ``shared.audit_events`` carrying ``{kind, target_id_if_any,
  rotated_by, vault_version, timestamp}`` (R15.4).
* ``GET /admin/security/ssh-runners`` /
  ``POST /admin/security/ssh-runners/{runner_id}/rotate-key`` /
  ``POST /admin/security/ssh-runners/{runner_id}/rotate-known-hosts`` /
  ``POST /admin/security/ssh-runners/{runner_id}/finalize-rotation``
  (`platform-real-usage-gaps` task 8.2, **Validates: Requirements
  8.1, 8.2, 8.3, 8.4**) — SSH key dual-slot rotation endpoints.
  Admin-only. Generates Ed25519 keypairs, manages active/previous
  Vault slots, runs ``ssh-keyscan`` for known_hosts refresh, and
  emits audit events for each operation.
* ``GET /admin/security/webhooks`` /
  ``POST /admin/security/webhooks/{dept_id}/{provider}/rotate`` /
  ``POST /admin/security/webhooks/{dept_id}/{provider}/finalize``
  (`platform-real-usage-gaps` task 9.2, **Validates: Requirements
  9.1, 9.2, 9.3**) — Webhook secret dual-slot rotation endpoints.
  Admin-only. Manages ``secret_current`` / ``secret_previous`` slots
  with a 1-hour overlap window for zero-downtime rotation.

The two read-only banner endpoints share a single :data:`router`
mounted under ``/admin/security``. The three rotation endpoints sit
on a separate :data:`rotation_router` under ``/api/v1/security`` to
match the contract documented in ``requirements.md`` R15.1.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as _secrets_module
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from auth_shared import AuthContext
from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = [
    "router",
    "rotation_router",
    "SecretKind",
    "SupportsSecretRotator",
    "SupportsHotReloadPublisher",
    "SecretRotationError",
    "WebhookSecretRotationRequest",
    "BotCredentialRotationRequest",
    "LlmApiKeyRotationRequest",
    "SshRunnerInfo",
    "SshRunnerListResponse",
    "RotateKeyResponse",
    "RotateKnownHostsRequest",
    "RotateKnownHostsResponse",
    "FinalizeRotationResponse",
    "WebhookEntry",
    "WebhookListResponse",
    "WebhookRotateResponse",
    "WebhookFinalizeResponse",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Read-only banner router (existing surfaces — task 11.7)
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/admin/security", tags=["security"])


def _proxy(request: Request) -> Any:
    return getattr(request.app.state, "admin_proxy", None)


@router.get("/probe-artifacts", dependencies=[Depends(require_admin)])
async def probe_artifacts(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> Any:
    proxy = _proxy(request)
    if proxy is None:
        return {"items": []}
    proxy_response = await proxy.forward(
        method="GET",
        path="/admin/security/probe-artifacts",
        body=b"",
        headers={key: value for key, value in request.headers.items()},
        actor=AuthContext(
            actor_id=actor.sub,
            actor_role="admin",
            dept_ids=frozenset(),
            raw_claims={"sub": actor.sub, "groups": list(actor.groups)},
        ),
        query_string=request.url.query or "",
    )
    if proxy_response.status_code == status.HTTP_404_NOT_FOUND:
        return {"items": []}
    return Response(
        content=proxy_response.body,
        status_code=proxy_response.status_code,
        headers=dict(proxy_response.headers),
    )


@router.get(
    "/credential-rotate-banner", dependencies=[Depends(require_admin)]
)
async def credential_rotate_banner(request: Request) -> dict:
    """Return depts whose bot credential needs rotation soon.

    The rotation TTL state lives on
    ``app.state.credential_rotation_state`` — a callable that
    returns ``[{"dept_id": str, "service": str, "rotates_in_days":
    int}, ...]``. Production wires this to the foundation Vault
    metadata reader; tests inject a list-backed callable.
    """

    state_provider = getattr(
        request.app.state, "credential_rotation_state", None
    )
    if state_provider is None:
        return {"depts": []}
    try:
        depts = list(state_provider() or [])
    except Exception:  # noqa: BLE001
        depts = []
    return {"depts": depts}


# ---------------------------------------------------------------------------
# Rotation router (`platform-gap-fill` task 15.1)
# ---------------------------------------------------------------------------


#: The three secret kinds the rotation endpoints emit on the audit row
#: (R15.4 — ``payload.kind``).
SecretKind = Literal["webhook_secret", "bot_credential", "llm_api_key"]

#: Audit action label written for every rotation (R15.4).
_AUDIT_ACTION_SECRET_ROTATED: str = "secret_rotated"

#: Allow-lists used to reject malformed input early (R15.1 — 400 on
#: invalid kind / payload). Keeping these tight prevents callers from
#: coercing the rotator into writing to arbitrary Vault paths via
#: creative ``service`` / ``provider`` values.
_ALLOWED_BOT_SERVICES: frozenset[str] = frozenset(
    {"jira", "bitbucket", "confluence"}
)

#: Default channel name used by the in-process Redis pub/sub stub
#: when the operator has not wired a real publisher. Documented here
#: so operators tailing logs can grep for the canonical token.
_DEFAULT_RELOAD_CHANNEL: str = "secrets:reload"


class SecretRotationError(Exception):
    """Raised by the rotator when Vault rejects the write.

    The router maps this to ``HTTP 502`` so a transient Vault outage
    does not look like a malformed request to the FE. The exception
    message MUST NOT include the secret value (R15 implementation
    note); the error is constructed with metadata only.
    """


@runtime_checkable
class SupportsSecretRotator(Protocol):
    """Narrow rotation-write surface consumed by this router.

    Production wires this against an adapter built on the
    :mod:`vault_client` library (``hvac``-style KV-v2 writes for the
    three secret families). Tests inject an in-memory fake. The
    protocol is intentionally async + tiny so the router stays
    SDK-agnostic.

    Implementations MUST:

    * Write the new secret material to the appropriate Vault KV-v2
      path (the actual path layout is the implementation's concern).
    * Preserve the previous version through Vault's KV-v2 version
      history (R15.2).
    * Return the new ``version`` number so the audit row can record
      it (R15.4 — ``payload.vault_version``; metadata only, never
      the value).
    * Raise :class:`SecretRotationError` on any non-recoverable
      backend failure (R15 — 502 + no partial commit).
    """

    async def rotate_webhook_secret(
        self,
        *,
        dept_id: str | None,
        new_secret: str,
    ) -> int:
        """Rotate the (optionally per-dept) webhook secret."""

    async def rotate_bot_credential(
        self,
        *,
        dept_id: str,
        service: str,
        new_secret: str,
    ) -> int:
        """Rotate a bot credential under
        ``vault:atlassian/<dept>/<service>``.
        """

    async def rotate_llm_api_key(
        self,
        *,
        provider: str,
        new_key: str,
    ) -> int:
        """Rotate an LLM provider API key."""


@runtime_checkable
class SupportsHotReloadPublisher(Protocol):
    """Hot-reload signalling surface (R15.3).

    Production wires this against a Redis pub/sub publisher (default
    channel ``secrets:reload``) or a fan-out HTTP client that calls
    each dependent service's cache-invalidation endpoint. The router
    only needs the single ``publish`` method.

    Failures are swallowed at the call site — a missed reload signal
    does not invalidate the rotation itself; consumers will pick up
    the new credential on their next cache miss.
    """

    async def publish(self, *, kind: str, target: str | None) -> None:
        """Notify consumers that secret material for *(kind, target)* changed.

        ``target`` is a free-form opaque string (typically
        ``"<dept_id>"``, ``"<dept_id>/<service>"``, or
        ``"<provider>"``) so dependents can scope cache invalidation
        narrowly.
        """


# ---------------------------------------------------------------------------
# Pydantic request models — one per rotation endpoint (R15.1)
# ---------------------------------------------------------------------------


class WebhookSecretRotationRequest(BaseModel):
    """Request body for ``POST /api/v1/security/rotate/webhook_secret``.

    ``dept_id`` is optional — omit for the global webhook secret.
    ``new_secret`` is optional — when omitted the server generates a
    fresh URL-safe token via :func:`secrets.token_urlsafe(32)` so the
    operator can rotate without typing material into the FE.
    """

    dept_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Optional department id. Omit to rotate the global "
            "webhook secret."
        ),
    )
    new_secret: str | None = Field(
        default=None,
        min_length=8,
        max_length=4096,
        description=(
            "Optional caller-supplied secret. When omitted the "
            "server generates a fresh token."
        ),
    )

    @field_validator("dept_id", "new_secret", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


class BotCredentialRotationRequest(BaseModel):
    """Request body for ``POST /api/v1/security/rotate/bot_credential``.

    All three fields are required by the task spec
    (``service``, ``dept_id``, ``new_secret``). ``service`` must be
    one of ``jira``, ``bitbucket``, ``confluence`` — anything else
    is rejected at parse time with HTTP 422 (FastAPI's standard
    validation error path).
    """

    service: Literal["jira", "bitbucket", "confluence"] = Field(
        ...,
        description="Atlassian service (jira / bitbucket / confluence).",
    )
    dept_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Department id whose bot credential is being rotated.",
    )
    new_secret: str = Field(
        ...,
        min_length=8,
        max_length=4096,
        description="New bot credential material (api token / app password).",
    )

    @field_validator("dept_id", "new_secret", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


class LlmApiKeyRotationRequest(BaseModel):
    """Request body for ``POST /api/v1/security/rotate/llm_api_key``.

    Both fields are required. ``provider`` is opaque from the
    router's perspective — the wired :class:`SupportsSecretRotator`
    decides which Vault path corresponds to the provider name. We
    enforce a non-empty string and a length cap so a malformed
    request can't reach the rotator.
    """

    provider: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="LLM provider identifier (eg. openai / anthropic / vllm).",
    )
    new_key: str = Field(
        ...,
        min_length=8,
        max_length=4096,
        description="New API key material.",
    )

    @field_validator("provider", "new_key", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


# ---------------------------------------------------------------------------
# Rotation router setup
# ---------------------------------------------------------------------------


rotation_router = APIRouter(
    prefix="/api/v1/security",
    tags=["security"],
)


# ---------------------------------------------------------------------------
# Dependency lookups (rotator + reload publisher + audit sink)
# ---------------------------------------------------------------------------


def _get_rotator(request: Request) -> SupportsSecretRotator:
    """Return the wired :class:`SupportsSecretRotator` instance.

    Raises:
        HTTPException(503): When the slot is ``None`` (Vault wiring
            still in flight, or the dev Compose profile has not
            brought Vault up yet).
    """

    rotator = getattr(request.app.state, "secret_rotator", None)
    if rotator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "secret_rotator_unavailable",
            },
        )
    return rotator


def _get_reload_publisher(request: Request) -> SupportsHotReloadPublisher | None:
    """Return the hot-reload publisher, or ``None`` when not wired.

    R15.3 mandates that consumers receive a hot-reload signal after
    a successful rotation. When no publisher is configured we log a
    structured ``secret_reload_pending`` warning so operators can
    page consumers manually — the rotation itself still succeeds.
    """

    return getattr(request.app.state, "secret_reload_publisher", None)


def _get_audit_sink(request: Request) -> Any | None:
    """Return the audit sink wired for ``secret_rotated`` events.

    Mirrors the lookup used by :mod:`workflow_control` so both
    surfaces land their events in the same audit stream when an
    explicit override is not configured.
    """

    explicit = getattr(request.app.state, "secret_rotation_audit_sink", None)
    if explicit is not None:
        return explicit
    proxy = getattr(request.app.state, "admin_proxy", None)
    if proxy is not None:
        return getattr(proxy, "_audit", None)
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_token() -> str:
    """Return a fresh URL-safe random token (R15 implementation note).

    Wraps :func:`secrets.token_urlsafe` so tests can monkey-patch a
    single module-level symbol when they need deterministic values.
    32 bytes of entropy → 43-character base64url string, well above
    the 8-char ``min_length`` enforced on caller-supplied values.
    """

    return _secrets_module.token_urlsafe(32)


def _emit_secret_rotated_audit(
    *,
    sink: Any,
    actor: AuthClaims,
    kind: SecretKind,
    target_id: str | None,
    vault_version: int,
    rotated_at: datetime,
) -> Any:
    """Write a single ``secret_rotated`` audit event (R15.4).

    The payload carries ``{kind, target_id_if_any, rotated_by,
    vault_version, timestamp}`` per the task spec — metadata only,
    never the secret value. The function returns the awaitable produced
    by ``sink.write(...)`` so the caller can await it directly; this
    lets the endpoint propagate audit-write failures (the rotation
    itself has already succeeded by this point so a hard failure here
    is logged but does not roll back Vault).
    """

    payload: dict[str, Any] = {
        "kind": kind,
        "target_id_if_any": target_id,
        "rotated_by": actor.sub,
        "vault_version": vault_version,
        "timestamp": rotated_at.isoformat(),
    }
    target_repr = target_id if target_id is not None else "global"
    event = AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=_audit_dept_id(kind, target_id),
        action=_AUDIT_ACTION_SECRET_ROTATED,
        resource=f"secret:{kind}:{target_repr}",
        result="ok",
        timestamp=rotated_at,
        payload=payload,
    )
    return sink.write(event)


def _audit_dept_id(kind: SecretKind, target_id: str | None) -> str | None:
    """Pull a usable ``dept_id`` out of *(kind, target_id)* for the audit row.

    The ``shared.audit_events.dept_id`` column lets operators slice
    the audit log per department; we populate it whenever the
    rotation has a clear dept scope so those queries find the row.
    """

    if target_id is None:
        return None
    if kind == "webhook_secret":
        return target_id
    if kind == "bot_credential":
        # ``target_id`` is ``"<dept>/<service>"`` — dept is the prefix.
        head = target_id.split("/", 1)[0]
        return head or None
    return None


async def _safe_audit(
    sink: Any | None,
    *,
    actor: AuthClaims,
    kind: SecretKind,
    target_id: str | None,
    vault_version: int,
    rotated_at: datetime,
) -> None:
    """Best-effort audit write that never raises.

    The Vault write has already committed successfully by the time
    this runs, so an audit hiccup must not cause the endpoint to
    fail with 5xx after the secret has been rotated. We log the
    failure at WARNING so operators can replay the row from the
    structured log if needed.
    """

    if sink is None:
        return
    try:
        await _emit_secret_rotated_audit(
            sink=sink,
            actor=actor,
            kind=kind,
            target_id=target_id,
            vault_version=vault_version,
            rotated_at=rotated_at,
        )
    except Exception as exc:  # noqa: BLE001 — audit must never block
        logger.warning(
            "secret_rotated audit write failed (kind=%s, target=%s): %s",
            kind,
            target_id,
            exc,
        )


async def _publish_reload(
    request: Request,
    *,
    kind: SecretKind,
    target: str | None,
) -> None:
    """Send the hot-reload signal (R15.3); never raises.

    When no publisher is wired we emit a structured operator log so
    the rotation is still observable as "reload pending — page
    consumers manually". The log line uses the canonical
    ``secrets:reload`` channel name so operators can grep for it.
    """

    publisher = _get_reload_publisher(request)
    if publisher is None:
        # Operators tailing logs can pick this up as the signal to
        # restart / poke dependent services manually. The log shape
        # mirrors the JSON envelope a real publisher would emit on
        # ``secrets:reload`` so log-to-alert pipelines see the same
        # ``kind`` / ``target`` keys regardless of wiring state.
        # TODO(platform-gap-fill 15.1): wire a real Redis pub/sub
        # publisher (or per-service cache-invalidation HTTP fan-out)
        # so consumers refresh credentials without operator action.
        logger.warning(
            "secret_reload_pending channel=%s kind=%s target=%s "
            "(no publisher wired; operators must page consumers)",
            _DEFAULT_RELOAD_CHANNEL,
            kind,
            target,
        )
        return

    try:
        await publisher.publish(kind=kind, target=target)
    except Exception as exc:  # noqa: BLE001 — reload is best-effort
        logger.warning(
            "secret_reload_publish_failed kind=%s target=%s: %s",
            kind,
            target,
            exc,
        )


def _vault_502(*, kind: SecretKind, target: str | None, exc: Exception) -> HTTPException:
    """Build the canonical ``HTTP 502`` returned on Vault write failure.

    Per the task spec the audit row is **only** written on a
    successful rotation (no partial-commit), so this helper does not
    touch the audit sink. It just shapes a stable error envelope so
    the FE can surface the failure consistently.
    """

    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "error": "vault_write_failed",
            "kind": kind,
            "target": target,
            "message": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@rotation_router.post(
    "/rotate/webhook_secret",
    summary="Rotate the webhook HMAC secret (admin only)",
    dependencies=[Depends(require_admin)],
)
async def rotate_webhook_secret(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: WebhookSecretRotationRequest = Body(
        default_factory=WebhookSecretRotationRequest,
    ),
) -> dict[str, Any]:
    """Rotate the (optionally per-dept) webhook HMAC secret.

    **Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.5**

    Body shape (all fields optional):

    .. code-block:: json

        {"dept_id": "<dept>", "new_secret": "<material>"}

    Behaviour:

    1. If ``new_secret`` is omitted, generate a fresh URL-safe token
       (R15 implementation note).
    2. Write to Vault via the wired :class:`SupportsSecretRotator`;
       Vault KV-v2 retains the previous version automatically (R15.2).
    3. Publish a hot-reload signal so consumers invalidate their
       credential caches (R15.3) — best-effort, a missing publisher
       only emits a log warning.
    4. Write one ``secret_rotated`` audit row carrying
       ``{kind, target_id_if_any, rotated_by, vault_version,
       timestamp}`` (R15.4).
    5. Return ``{kind, target, vault_version, generated, rotated_at}``.
       The new secret material is **not** returned — operators that
       need the value read it back from Vault via the RBAC-gated
       read path.

    On Vault write failure the endpoint returns ``HTTP 502`` and the
    audit row is **not** written (no partial commit).
    """

    rotator = _get_rotator(request)

    new_secret = body.new_secret or _generate_token()
    generated = body.new_secret is None
    target_id: str | None = body.dept_id

    try:
        version = await rotator.rotate_webhook_secret(
            dept_id=target_id,
            new_secret=new_secret,
        )
    except SecretRotationError as exc:
        # Per task spec: do NOT partial-commit the audit row on a
        # Vault failure. The 502 is the only side-effect.
        raise _vault_502(kind="webhook_secret", target=target_id, exc=exc) from exc

    rotated_at = datetime.now(tz=timezone.utc)

    await _publish_reload(request, kind="webhook_secret", target=target_id)
    await _safe_audit(
        _get_audit_sink(request),
        actor=actor,
        kind="webhook_secret",
        target_id=target_id,
        vault_version=version,
        rotated_at=rotated_at,
    )

    return {
        "kind": "webhook_secret",
        "target": target_id,
        "vault_version": version,
        "generated": generated,
        "rotated_at": rotated_at.isoformat(),
    }


@rotation_router.post(
    "/rotate/bot_credential",
    summary="Rotate a bot credential (admin only)",
    dependencies=[Depends(require_admin)],
)
async def rotate_bot_credential(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: BotCredentialRotationRequest = Body(...),
) -> dict[str, Any]:
    """Rotate a per-department bot credential under
    ``vault:atlassian/<dept>/<service>``.

    **Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.5**

    Body shape (all fields required):

    .. code-block:: json

        {"service": "jira",
         "dept_id": "<dept>",
         "new_secret": "<material>"}

    ``service`` is restricted to ``jira`` / ``bitbucket`` /
    ``confluence`` at parse time (Pydantic ``Literal``); other values
    surface as ``HTTP 422`` (FastAPI validation envelope) per R15.1
    (400-class on invalid payload). On Vault write failure the
    endpoint returns ``HTTP 502`` and **no** audit row is written
    (no partial commit). On success a single ``secret_rotated``
    audit row carries ``{kind="bot_credential",
    target_id_if_any="<dept>/<service>", rotated_by, vault_version,
    timestamp}`` (R15.4).
    """

    # ``service`` is constrained by Pydantic ``Literal``; this is a
    # belt-and-braces check in case a future caller bypasses the
    # model (eg. via a custom dependency).
    if body.service not in _ALLOWED_BOT_SERVICES:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "service must be one of "
                f"{sorted(_ALLOWED_BOT_SERVICES)}"
            ),
        )

    rotator = _get_rotator(request)
    target_id = f"{body.dept_id}/{body.service}"

    try:
        version = await rotator.rotate_bot_credential(
            dept_id=body.dept_id,
            service=body.service,
            new_secret=body.new_secret,
        )
    except SecretRotationError as exc:
        raise _vault_502(
            kind="bot_credential", target=target_id, exc=exc
        ) from exc

    rotated_at = datetime.now(tz=timezone.utc)

    await _publish_reload(request, kind="bot_credential", target=target_id)
    await _safe_audit(
        _get_audit_sink(request),
        actor=actor,
        kind="bot_credential",
        target_id=target_id,
        vault_version=version,
        rotated_at=rotated_at,
    )

    return {
        "kind": "bot_credential",
        "target": target_id,
        "vault_version": version,
        "rotated_at": rotated_at.isoformat(),
    }


@rotation_router.post(
    "/rotate/llm_api_key",
    summary="Rotate an LLM provider API key (admin only)",
    dependencies=[Depends(require_admin)],
)
async def rotate_llm_api_key(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: LlmApiKeyRotationRequest = Body(...),
) -> dict[str, Any]:
    """Rotate an LLM provider API key.

    **Validates: Requirements 15.1, 15.2, 15.3, 15.4, 15.5**

    Body shape (all fields required):

    .. code-block:: json

        {"provider": "openai", "new_key": "<material>"}

    On Vault write failure the endpoint returns ``HTTP 502`` and
    **no** audit row is written (no partial commit). On success a
    single ``secret_rotated`` audit row carries ``{kind="llm_api_key",
    target_id_if_any="<provider>", rotated_by, vault_version,
    timestamp}`` (R15.4).
    """

    rotator = _get_rotator(request)
    target_id = body.provider

    try:
        version = await rotator.rotate_llm_api_key(
            provider=body.provider,
            new_key=body.new_key,
        )
    except SecretRotationError as exc:
        raise _vault_502(
            kind="llm_api_key", target=target_id, exc=exc
        ) from exc

    rotated_at = datetime.now(tz=timezone.utc)

    await _publish_reload(request, kind="llm_api_key", target=target_id)
    await _safe_audit(
        _get_audit_sink(request),
        actor=actor,
        kind="llm_api_key",
        target_id=target_id,
        vault_version=version,
        rotated_at=rotated_at,
    )

    return {
        "kind": "llm_api_key",
        "target": target_id,
        "vault_version": version,
        "rotated_at": rotated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# SSH Runners endpoints (platform-real-usage-gaps task 8.2)
# ---------------------------------------------------------------------------
# Validates: Requirements 8.1, 8.2, 8.3, 8.4
#
# These endpoints manage SSH key dual-slot rotation for execution
# runners. The rotation lifecycle is:
#
# 1. ``GET /admin/security/ssh-runners`` — list runners with status.
# 2. ``POST .../rotate-key`` — generate new Ed25519 keypair, demote
#    active→previous, write new key to active, return public key once.
# 3. ``POST .../rotate-known-hosts`` — ssh-keyscan the host, update
#    fingerprint if changed.
# 4. ``POST .../finalize-rotation`` — clear previous slot after
#    operator confirms new key works on target host.
#
# RBAC: all endpoints require ``admin`` role.
# ---------------------------------------------------------------------------


#: Audit action labels for SSH runner operations.
_AUDIT_SSH_KEY_ROTATED: str = "ssh_key_rotated"
_AUDIT_SSH_KNOWN_HOSTS_ROTATED: str = "ssh_known_hosts_rotated"
_AUDIT_SSH_KEY_ROTATION_FINALIZED: str = "ssh_key_rotation_finalized"

#: Environment variable that defines the SSH runners configuration.
#: Format: comma-separated ``runner_id:host:port`` triples.
#: Example: ``runner-01:192.168.1.100:22,runner-02:10.0.0.5:2222``
#: When not set, falls back to a single runner derived from
#: ``SSH_HOST`` (canonical) with ``SSH_HOST_1`` accepted as a deprecated
#: alias / ``SSH_PORT_DEFAULT`` environment variables.
#:
#: NOTE on single-runner canonical contract: the platform runs exactly
#: one SSH runner host. ``SSH_RUNNERS`` exists for legacy multi-runner
#: deployments; new deployments leave it empty and configure ``SSH_HOST``
#: only. Only the **first** runner in ``SSH_RUNNERS`` is treated as the
#: active runner by the rest of the stack — additional entries are
#: visible only on the rotation UI.
_SSH_RUNNERS_ENV: str = "SSH_RUNNERS"


# ---------------------------------------------------------------------------
# Pydantic models for SSH runner responses
# ---------------------------------------------------------------------------


class SshRunnerInfo(BaseModel):
    """Response model for a single SSH runner entry."""

    runner_id: str
    host: str
    port: int
    last_rotated_at: str | None = None
    active_key_fingerprint: str | None = None
    previous_key_fingerprint: str | None = None
    known_hosts_fingerprint: str | None = None
    status: Literal["ok", "key_expired", "unreachable"] = "ok"


class SshRunnerListResponse(BaseModel):
    """Response model for the SSH runners list endpoint."""

    runners: list[SshRunnerInfo]


class RotateKeyResponse(BaseModel):
    """Response model for the rotate-key endpoint.

    The ``public_key`` field is returned **once** so the operator can
    add it to the target host's ``~/.ssh/authorized_keys``.
    """

    runner_id: str
    public_key: str
    rotated_at: str
    active_key_fingerprint: str


class RotateKnownHostsRequest(BaseModel):
    """Request body for the rotate-known-hosts endpoint.

    When ``accept_new_fingerprint`` is ``True``, the endpoint writes
    the scanned fingerprint to the known_hosts file. When ``False``
    (or omitted on the first call), the endpoint returns the scanned
    fingerprint for the admin to review before accepting.
    """

    accept_new_fingerprint: bool = Field(
        default=False,
        description=(
            "Whether to accept and persist the newly scanned "
            "fingerprint. Set to true after reviewing the scan result."
        ),
    )


class RotateKnownHostsResponse(BaseModel):
    """Response model for the rotate-known-hosts endpoint."""

    runner_id: str
    scanned_fingerprint: str
    previous_fingerprint: str | None = None
    accepted: bool
    updated_at: str | None = None


class FinalizeRotationResponse(BaseModel):
    """Response model for the finalize-rotation endpoint."""

    runner_id: str
    finalized_at: str
    previous_slot_cleared: bool


# ---------------------------------------------------------------------------
# SSH runner configuration resolution
# ---------------------------------------------------------------------------


def _resolve_ssh_runners() -> list[dict[str, Any]]:
    """Resolve the list of configured SSH runners from environment.

    Reads ``SSH_RUNNERS`` env var (comma-separated
    ``runner_id:host:port`` triples). Falls back to a single runner
    derived from ``SSH_HOST`` (canonical) with ``SSH_HOST_1`` accepted
    as a deprecated alias / ``SSH_PORT_DEFAULT`` when the explicit list
    is not configured.

    Single-runner canonical contract: the platform runs **exactly one**
    SSH runner host. New deployments leave ``SSH_RUNNERS`` empty and
    configure ``SSH_HOST`` only. ``SSH_HOST_1`` is preserved as a
    deprecated alias for backwards compatibility; ``SSH_HOST_2`` /
    ``SSH_HOST_3`` are not consulted.

    Returns:
        List of dicts with keys ``runner_id``, ``host``, ``port``.
    """
    raw = os.environ.get(_SSH_RUNNERS_ENV, "").strip()
    if raw:
        runners: list[dict[str, Any]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split(":")
            if len(parts) >= 3:
                runner_id = parts[0].strip()
                host = parts[1].strip()
                try:
                    port = int(parts[2].strip())
                except (TypeError, ValueError):
                    port = 22
            elif len(parts) == 2:
                runner_id = parts[0].strip()
                host = parts[1].strip()
                port = 22
            else:
                runner_id = parts[0].strip()
                host = parts[0].strip()
                port = 22
            runners.append(
                {"runner_id": runner_id, "host": host, "port": port}
            )
        if runners:
            return runners

    # Fallback: single runner from canonical / legacy env vars.
    # Resolution order: SSH_HOST → SSH_HOST_1 (deprecated) → "localhost".
    host = os.environ.get("SSH_HOST", "").strip()
    if not host:
        host = os.environ.get("SSH_HOST_1", "").strip()
    if not host:
        host = "localhost"
    port_raw = os.environ.get("SSH_PORT_DEFAULT", "22")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22
    return [{"runner_id": "default", "host": host, "port": port}]


def _get_vault_client(request: Request) -> Any:
    """Return the Vault client from app state, or raise 503.

    The SSH runner endpoints need a :class:`vault_client.VaultClient`
    instance to read/write SSH key slots. This is wired on
    ``app.state.vault_client`` during lifespan startup.
    """
    vault = getattr(request.app.state, "vault_client", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "vault_client_unavailable",
            },
        )
    return vault


def _ssh_audit_event(
    *,
    actor: AuthClaims,
    action: str,
    runner_id: str,
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """Build an audit event for SSH runner operations."""
    return AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=None,
        action=action,
        resource=f"ssh_runner:{runner_id}",
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload=payload or {},
    )


async def _write_ssh_audit(
    request: Request,
    *,
    actor: AuthClaims,
    action: str,
    runner_id: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write for SSH runner operations."""
    sink = _get_audit_sink(request)
    if sink is None:
        return
    event = _ssh_audit_event(
        actor=actor,
        action=action,
        runner_id=runner_id,
        payload=payload,
    )
    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 — audit must never block
        logger.warning(
            "ssh_runner audit write failed (action=%s, runner=%s): %s",
            action,
            runner_id,
            exc,
        )


# ---------------------------------------------------------------------------
# SSH Runner Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/ssh-runners",
    summary="List SSH runners with status and fingerprints (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=SshRunnerListResponse,
)
async def list_ssh_runners(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> SshRunnerListResponse:
    """Return the list of configured SSH runners with their current state.

    **Validates: Requirement 8.1**

    Each runner entry includes:
    - ``runner_id``, ``host``, ``port`` — identity.
    - ``last_rotated_at`` — UTC ISO timestamp of last key rotation.
    - ``active_key_fingerprint`` — SHA256 fingerprint of the active key.
    - ``previous_key_fingerprint`` — fingerprint of the previous key
      (present only during the overlap window before finalization).
    - ``known_hosts_fingerprint`` — fingerprint from the last
      ``ssh-keyscan`` run.
    - ``status`` — ``"ok"`` | ``"key_expired"`` | ``"unreachable"``.
    """
    runners_config = _resolve_ssh_runners()
    vault = _get_vault_client(request)

    # Lazy import to avoid hard dependency at module load time.
    from vault_client.ssh_keys import (
        read_active,
        read_previous,
        read_rotation_meta,
    )
    from vault_client.path import VaultPath

    runners: list[SshRunnerInfo] = []
    for cfg in runners_config:
        runner_id = cfg["runner_id"]
        host = cfg["host"]
        port = cfg["port"]

        # Read key state from Vault
        active_key = read_active(vault, runner_id)
        previous_key = read_previous(vault, runner_id)
        rotated_at = read_rotation_meta(vault, runner_id)

        # Read known_hosts fingerprint from Vault metadata
        known_hosts_fp: str | None = None
        try:
            kh_path = VaultPath.parse(
                f"vault:ssh/runners/{runner_id}/known_hosts"
            )
            kh_data = vault.read(kh_path)
            known_hosts_fp = kh_data.get("fingerprint")
        except (KeyError, Exception):  # noqa: BLE001
            pass

        # Determine status
        runner_status: Literal["ok", "key_expired", "unreachable"] = "ok"
        if active_key is None:
            runner_status = "unreachable"

        runners.append(
            SshRunnerInfo(
                runner_id=runner_id,
                host=host,
                port=port,
                last_rotated_at=(
                    rotated_at.isoformat() if rotated_at else None
                ),
                active_key_fingerprint=(
                    active_key.fingerprint if active_key else None
                ),
                previous_key_fingerprint=(
                    previous_key.fingerprint if previous_key else None
                ),
                known_hosts_fingerprint=known_hosts_fp,
                status=runner_status,
            )
        )

    return SshRunnerListResponse(runners=runners)


@router.post(
    "/ssh-runners/{runner_id}/rotate-key",
    summary="Rotate SSH key for a runner (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=RotateKeyResponse,
)
async def rotate_ssh_key(
    request: Request,
    runner_id: str,
    actor: AuthClaims = Depends(require_admin),
) -> RotateKeyResponse:
    """Generate a new Ed25519 keypair and rotate the SSH key for a runner.

    **Validates: Requirements 8.2**

    The rotation lifecycle:
    1. Generate a fresh Ed25519 keypair.
    2. Demote the current ``active`` slot to ``previous``.
    3. Write the new key to ``active``.
    4. Return the new public key **once** so the operator can add it
       to the target host's ``~/.ssh/authorized_keys``.

    After this call, both ``active`` and ``previous`` slots are valid
    (zero-downtime overlap window). The operator must call
    ``finalize-rotation`` after verifying the new key works.

    Audit: writes ``ssh_key_rotated`` event.
    """
    # Validate runner_id exists in configuration
    runners_config = _resolve_ssh_runners()
    runner_ids = {r["runner_id"] for r in runners_config}
    if runner_id not in runner_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "runner_not_found",
                "runner_id": runner_id,
            },
        )

    vault = _get_vault_client(request)

    from vault_client.ssh_keys import read_active, rotate

    try:
        public_key = rotate(vault, runner_id)
    except Exception as exc:
        logger.error(
            "SSH key rotation failed for runner %s: %s", runner_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "ssh_key_rotation_failed",
                "runner_id": runner_id,
                "message": str(exc),
            },
        ) from exc

    rotated_at = datetime.now(timezone.utc)

    # Read back the active key to get the fingerprint
    active_key = read_active(vault, runner_id)
    fingerprint = active_key.fingerprint if active_key else ""

    # Audit
    await _write_ssh_audit(
        request,
        actor=actor,
        action=_AUDIT_SSH_KEY_ROTATED,
        runner_id=runner_id,
        payload={
            "runner_id": runner_id,
            "fingerprint": fingerprint,
            "rotated_at": rotated_at.isoformat(),
        },
    )

    return RotateKeyResponse(
        runner_id=runner_id,
        public_key=public_key,
        rotated_at=rotated_at.isoformat(),
        active_key_fingerprint=fingerprint,
    )


@router.post(
    "/ssh-runners/{runner_id}/rotate-known-hosts",
    summary="Refresh known_hosts fingerprint for a runner (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=RotateKnownHostsResponse,
)
async def rotate_known_hosts(
    request: Request,
    runner_id: str,
    actor: AuthClaims = Depends(require_admin),
    body: RotateKnownHostsRequest = Body(
        default_factory=RotateKnownHostsRequest,
    ),
) -> RotateKnownHostsResponse:
    """Run ``ssh-keyscan`` against the runner host and update fingerprint.

    **Validates: Requirement 8.3**

    Two-phase operation:
    1. First call (``accept_new_fingerprint=false``): scans the host
       and returns the new fingerprint for admin review.
    2. Second call (``accept_new_fingerprint=true``): persists the
       scanned fingerprint to Vault.

    Audit: writes ``ssh_known_hosts_rotated`` event on acceptance.
    """
    # Validate runner_id exists
    runners_config = _resolve_ssh_runners()
    runner_map = {r["runner_id"]: r for r in runners_config}
    if runner_id not in runner_map:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "runner_not_found",
                "runner_id": runner_id,
            },
        )

    runner_cfg = runner_map[runner_id]
    host = runner_cfg["host"]
    port = runner_cfg["port"]

    vault = _get_vault_client(request)
    from vault_client.path import VaultPath

    # Run ssh-keyscan to get the host fingerprint
    try:
        cmd = ["ssh-keyscan", "-t", "ed25519,rsa", "-p", str(port), host]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        scan_output = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "ssh_keyscan_failed",
                "runner_id": runner_id,
                "host": host,
                "message": str(exc),
            },
        ) from exc

    if not scan_output:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "ssh_keyscan_empty",
                "runner_id": runner_id,
                "host": host,
                "message": (
                    "ssh-keyscan returned no output — host may be "
                    "unreachable or SSH is not running."
                ),
            },
        )

    # Compute fingerprint from the scan output (first key line)
    import hashlib
    import base64 as _b64

    scanned_fingerprint = ""
    for line in scan_output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3:
            try:
                raw_bytes = _b64.b64decode(parts[2])
                digest = hashlib.sha256(raw_bytes).digest()
                b64fp = _b64.b64encode(digest).rstrip(b"=").decode("ascii")
                scanned_fingerprint = f"SHA256:{b64fp}"
                break
            except Exception:  # noqa: BLE001
                continue

    if not scanned_fingerprint:
        # Fallback: use the raw scan output as fingerprint identifier
        scanned_fingerprint = f"raw:{scan_output[:64]}"

    # Read previous fingerprint from Vault
    kh_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/known_hosts")
    previous_fingerprint: str | None = None
    try:
        kh_data = vault.read(kh_path)
        previous_fingerprint = kh_data.get("fingerprint")
    except (KeyError, Exception):  # noqa: BLE001
        pass

    if not body.accept_new_fingerprint:
        # Phase 1: return scanned fingerprint for review
        return RotateKnownHostsResponse(
            runner_id=runner_id,
            scanned_fingerprint=scanned_fingerprint,
            previous_fingerprint=previous_fingerprint,
            accepted=False,
            updated_at=None,
        )

    # Phase 2: persist the new fingerprint
    updated_at = datetime.now(timezone.utc)
    vault.write(
        kh_path,
        {
            "fingerprint": scanned_fingerprint,
            "host": host,
            "port": str(port),
            "scan_output": scan_output,
            "updated_at": updated_at.isoformat(),
        },
    )

    # Audit
    await _write_ssh_audit(
        request,
        actor=actor,
        action=_AUDIT_SSH_KNOWN_HOSTS_ROTATED,
        runner_id=runner_id,
        payload={
            "runner_id": runner_id,
            "host": host,
            "port": port,
            "new_fingerprint": scanned_fingerprint,
            "previous_fingerprint": previous_fingerprint,
            "updated_at": updated_at.isoformat(),
        },
    )

    return RotateKnownHostsResponse(
        runner_id=runner_id,
        scanned_fingerprint=scanned_fingerprint,
        previous_fingerprint=previous_fingerprint,
        accepted=True,
        updated_at=updated_at.isoformat(),
    )


@router.post(
    "/ssh-runners/{runner_id}/finalize-rotation",
    summary="Finalize SSH key rotation by clearing previous slot (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=FinalizeRotationResponse,
)
async def finalize_ssh_rotation(
    request: Request,
    runner_id: str,
    actor: AuthClaims = Depends(require_admin),
) -> FinalizeRotationResponse:
    """Finalize the SSH key rotation by clearing the previous slot.

    **Validates: Requirement 8.4**

    Called after the operator has verified that the new key works
    against the target host (i.e., the new public key has been added
    to ``~/.ssh/authorized_keys`` on the remote). After this call,
    only the ``active`` slot contains a valid key.

    Audit: writes ``ssh_key_rotation_finalized`` event.
    """
    # Validate runner_id exists
    runners_config = _resolve_ssh_runners()
    runner_ids = {r["runner_id"] for r in runners_config}
    if runner_id not in runner_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "runner_not_found",
                "runner_id": runner_id,
            },
        )

    vault = _get_vault_client(request)

    from vault_client.ssh_keys import finalize, read_previous

    # Check if there's actually a previous slot to clear
    previous_key = read_previous(vault, runner_id)
    if previous_key is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "no_previous_slot",
                "runner_id": runner_id,
                "message": (
                    "No previous key slot to finalize — either rotation "
                    "was already finalized or no rotation has occurred."
                ),
            },
        )

    try:
        finalize(vault, runner_id)
    except Exception as exc:
        logger.error(
            "SSH key finalization failed for runner %s: %s", runner_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "ssh_key_finalization_failed",
                "runner_id": runner_id,
                "message": str(exc),
            },
        ) from exc

    finalized_at = datetime.now(timezone.utc)

    # Audit
    await _write_ssh_audit(
        request,
        actor=actor,
        action=_AUDIT_SSH_KEY_ROTATION_FINALIZED,
        runner_id=runner_id,
        payload={
            "runner_id": runner_id,
            "finalized_at": finalized_at.isoformat(),
        },
    )

    return FinalizeRotationResponse(
        runner_id=runner_id,
        finalized_at=finalized_at.isoformat(),
        previous_slot_cleared=True,
    )


# ---------------------------------------------------------------------------
# Webhook Secret Rotation endpoints (platform-real-usage-gaps task 9.2)
# ---------------------------------------------------------------------------
# Validates: Requirements 9.1, 9.2, 9.3
#
# These endpoints manage webhook HMAC secret dual-slot rotation for
# the dept × provider matrix. The rotation lifecycle is:
#
# 1. ``GET /admin/security/webhooks`` — list all dept × provider
#    entries with rotation status and overlap window remaining.
# 2. ``POST .../webhooks/{dept_id}/{provider}/rotate`` — generate a
#    new 32-byte secret, demote current→previous with overlap window,
#    return the new secret once so the operator can paste it into the
#    Atlassian/Bitbucket webhook configuration UI.
# 3. ``POST .../webhooks/{dept_id}/{provider}/finalize`` — clear the
#    previous slot after the operator has updated the provider-side
#    webhook secret.
#
# RBAC: all endpoints require ``admin`` role.
# ---------------------------------------------------------------------------


#: Audit action labels for webhook secret operations.
_AUDIT_WEBHOOK_SECRET_ROTATED: str = "webhook_secret_rotated"
_AUDIT_WEBHOOK_SECRET_ROTATION_FINALIZED: str = "webhook_secret_rotation_finalized"

#: Allowed webhook providers — kept in sync with
#: :data:`vault_client.webhook_secrets._ALLOWED_PROVIDERS`.
_ALLOWED_WEBHOOK_PROVIDERS: frozenset[str] = frozenset(
    {"jira", "bitbucket", "confluence"}
)
_WEBHOOK_ROTATION_OVERLAP_S: int = int(
    os.getenv("WEBHOOK_ROTATION_OVERLAP_S", "3600")
)

def _departments_config_path() -> Path:
    """Resolve ``config/departments.json`` across host and container layouts."""

    env_root = os.getenv("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root) / "config" / "departments.json"

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "config" / "departments.json"
        if candidate.exists():
            return candidate
    return Path("/app/config/departments.json")


# ---------------------------------------------------------------------------
# Pydantic models for webhook secret responses
# ---------------------------------------------------------------------------


class WebhookEntry(BaseModel):
    """Response model for a single dept × provider webhook entry."""

    dept_id: str
    provider: Literal["jira", "bitbucket", "confluence"]
    last_rotated_at: str | None = None
    overlap_window_remaining_s: int | None = None
    status: Literal["ok", "overlap_active", "never_rotated"] = "never_rotated"


class WebhookListResponse(BaseModel):
    """Response model for the webhook secrets list endpoint."""

    entries: list[WebhookEntry]


class WebhookRotateResponse(BaseModel):
    """Response model for the webhook rotate endpoint.

    The ``new_secret`` field is returned **once** so the operator can
    paste it into the Atlassian/Bitbucket webhook configuration UI.
    """

    dept_id: str
    provider: str
    new_secret: str
    rotated_at: str


class WebhookFinalizeResponse(BaseModel):
    """Response model for the webhook finalize endpoint."""

    dept_id: str
    provider: str
    finalized_at: str
    previous_slot_cleared: bool


# ---------------------------------------------------------------------------
# Webhook helpers
# ---------------------------------------------------------------------------


def _load_department_ids() -> list[str]:
    """Load department IDs from config/departments.json.

    Returns a list of department IDs. If the file is missing or
    malformed, returns an empty list (best-effort).
    """
    try:
        with open(_departments_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        depts = data.get("departments", [])
        return [d["id"] for d in depts if isinstance(d, dict) and "id" in d]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.warning("Failed to load departments.json for webhooks: %s", exc)
        return []


async def _write_webhook_audit(
    request: Request,
    *,
    actor: AuthClaims,
    action: str,
    dept_id: str,
    provider: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write for webhook secret operations."""
    sink = _get_audit_sink(request)
    if sink is None:
        return
    event = AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=dept_id,
        action=action,
        resource=f"webhook_secret:{dept_id}/{provider}",
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload=payload or {},
    )
    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 — audit must never block
        logger.warning(
            "webhook_secret audit write failed (action=%s, dept=%s, provider=%s): %s",
            action,
            dept_id,
            provider,
            exc,
        )


async def _vault_read_secret(vault: Any, path: str) -> dict[str, str] | None:
    reader = getattr(vault, "read_kv2_secret", None)
    if reader is None:
        return None
    return await reader(path=path)


async def _vault_write_secret(vault: Any, path: str, data: dict[str, str]) -> None:
    writer = getattr(vault, "write_kv2_secret", None)
    if writer is None:
        raise RuntimeError("vault client does not support write_kv2_secret")
    await writer(path=path, data=data)


async def _vault_delete_secret(vault: Any, path: str) -> None:
    deleter = getattr(vault, "delete_kv2_secret", None)
    if deleter is None:
        raise RuntimeError("vault client does not support delete_kv2_secret")
    await deleter(path=path)


def _webhook_path(provider: str, dept_id: str, suffix: str = "") -> str:
    tail = f"/{suffix.strip('/')}" if suffix else ""
    return f"webhooks/{provider}/{dept_id}{tail}"


async def _read_webhook_rotation_meta(
    vault: Any, dept_id: str, provider: str
) -> datetime | None:
    data = await _vault_read_secret(vault, _webhook_path(provider, dept_id, "meta"))
    raw = (data or {}).get("rotated_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


async def _read_webhook_overlap_remaining(
    vault: Any, dept_id: str, provider: str
) -> int | None:
    data = await _vault_read_secret(
        vault, _webhook_path(provider, dept_id, "previous")
    )
    raw_until = (data or {}).get("overlap_until")
    if not raw_until:
        return None
    try:
        overlap_until = datetime.fromisoformat(raw_until)
    except ValueError:
        return None
    if overlap_until.tzinfo is None:
        overlap_until = overlap_until.replace(tzinfo=timezone.utc)
    remaining = (overlap_until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


async def _rotate_webhook_secret(
    vault: Any, dept_id: str, provider: str
) -> tuple[str, datetime]:
    new_secret = _secrets_module.token_hex(32)
    now = datetime.now(timezone.utc)
    current_path = _webhook_path(provider, dept_id)
    previous_path = _webhook_path(provider, dept_id, "previous")

    current = await _vault_read_secret(vault, current_path)
    current_secret = (current or {}).get("secret")
    if current_secret:
        overlap_until = now + timedelta(seconds=_WEBHOOK_ROTATION_OVERLAP_S)
        await _vault_write_secret(
            vault,
            previous_path,
            {
                "secret": current_secret,
                "overlap_until": overlap_until.isoformat(),
            },
        )

    await _vault_write_secret(vault, current_path, {"secret": new_secret})
    await _vault_write_secret(
        vault,
        _webhook_path(provider, dept_id, "meta"),
        {"rotated_at": now.isoformat()},
    )
    return new_secret, now


async def _finalize_webhook_secret(vault: Any, dept_id: str, provider: str) -> None:
    await _vault_delete_secret(vault, _webhook_path(provider, dept_id, "previous"))


# ---------------------------------------------------------------------------
# Webhook Secret Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/webhooks",
    summary="List webhook secrets matrix (dept × provider) (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=WebhookListResponse,
)
async def list_webhooks(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> WebhookListResponse:
    """Return the dept × provider webhook secret matrix with rotation status.

    **Validates: Requirement 9.1**

    Each entry includes:
    - ``dept_id`` — department identifier.
    - ``provider`` — one of ``jira``, ``bitbucket``, ``confluence``.
    - ``last_rotated_at`` — UTC ISO timestamp of last rotation.
    - ``overlap_window_remaining_s`` — seconds remaining in the
      overlap window (present only when ``status == "overlap_active"``).
    - ``status`` — ``"ok"`` | ``"overlap_active"`` | ``"never_rotated"``.
    """
    dept_ids = _load_department_ids()
    vault = _get_vault_client(request)

    entries: list[WebhookEntry] = []
    for dept_id in dept_ids:
        for provider in sorted(_ALLOWED_WEBHOOK_PROVIDERS):
            # Read rotation metadata from Vault
            rotated_at = await _read_webhook_rotation_meta(vault, dept_id, provider)
            overlap_remaining = await _read_webhook_overlap_remaining(
                vault, dept_id, provider
            )

            # Determine status
            if rotated_at is None:
                entry_status: Literal["ok", "overlap_active", "never_rotated"] = (
                    "never_rotated"
                )
            elif overlap_remaining is not None and overlap_remaining > 0:
                entry_status = "overlap_active"
            else:
                entry_status = "ok"

            entries.append(
                WebhookEntry(
                    dept_id=dept_id,
                    provider=provider,  # type: ignore[arg-type]
                    last_rotated_at=(
                        rotated_at.isoformat() if rotated_at else None
                    ),
                    overlap_window_remaining_s=(
                        overlap_remaining
                        if overlap_remaining is not None and overlap_remaining > 0
                        else None
                    ),
                    status=entry_status,
                )
            )

    return WebhookListResponse(entries=entries)


@router.post(
    "/webhooks/{dept_id}/{provider}/rotate",
    summary="Rotate webhook secret for a dept × provider (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=WebhookRotateResponse,
)
async def rotate_webhook(
    request: Request,
    dept_id: str,
    provider: str,
    actor: AuthClaims = Depends(require_admin),
) -> WebhookRotateResponse:
    """Rotate the webhook HMAC secret for a department × provider pair.

    **Validates: Requirements 9.2**

    The rotation lifecycle:
    1. Generate a fresh 32-byte random secret.
    2. Demote the current ``secret_current`` to ``secret_previous``
       with an ``overlap_until`` timestamp (default 1 hour).
    3. Write the new secret to ``secret_current``.
    4. Return the new secret **once** so the operator can paste it
       into the Atlassian/Bitbucket webhook configuration UI.

    After this call, both ``secret_current`` and ``secret_previous``
    are valid for HMAC verification (zero-downtime overlap window).
    The operator should call ``finalize`` after updating the
    provider-side webhook configuration.

    Audit: writes ``webhook_secret_rotated`` event.
    """
    # Validate provider
    if provider not in _ALLOWED_WEBHOOK_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_provider",
                "provider": provider,
                "allowed": sorted(_ALLOWED_WEBHOOK_PROVIDERS),
            },
        )

    # Validate dept_id exists
    dept_ids = _load_department_ids()
    if dept_id not in dept_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "department_not_found",
                "dept_id": dept_id,
            },
        )

    vault = _get_vault_client(request)

    try:
        new_secret, rotated_at = await _rotate_webhook_secret(
            vault, dept_id, provider
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "rotation_validation_failed",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.error(
            "Webhook secret rotation failed for dept=%s provider=%s: %s",
            dept_id,
            provider,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "webhook_secret_rotation_failed",
                "dept_id": dept_id,
                "provider": provider,
                "message": str(exc),
            },
        ) from exc

    # Audit
    await _write_webhook_audit(
        request,
        actor=actor,
        action=_AUDIT_WEBHOOK_SECRET_ROTATED,
        dept_id=dept_id,
        provider=provider,
        payload={
            "dept_id": dept_id,
            "provider": provider,
            "rotated_at": rotated_at.isoformat(),
        },
    )

    return WebhookRotateResponse(
        dept_id=dept_id,
        provider=provider,
        new_secret=new_secret,
        rotated_at=rotated_at.isoformat(),
    )


@router.post(
    "/webhooks/{dept_id}/{provider}/finalize",
    summary="Finalize webhook secret rotation (admin only)",
    dependencies=[Depends(require_admin)],
    response_model=WebhookFinalizeResponse,
)
async def finalize_webhook(
    request: Request,
    dept_id: str,
    provider: str,
    actor: AuthClaims = Depends(require_admin),
) -> WebhookFinalizeResponse:
    """Finalize the webhook secret rotation by clearing the previous slot.

    **Validates: Requirement 9.3**

    Called after the operator has updated the provider-side webhook
    configuration with the new secret. After this call, only the
    ``secret_current`` slot contains a valid secret; HMAC verification
    will only accept signatures computed with the current secret.

    Audit: writes ``webhook_secret_rotation_finalized`` event.
    """
    # Validate provider
    if provider not in _ALLOWED_WEBHOOK_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_provider",
                "provider": provider,
                "allowed": sorted(_ALLOWED_WEBHOOK_PROVIDERS),
            },
        )

    # Validate dept_id exists
    dept_ids = _load_department_ids()
    if dept_id not in dept_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "department_not_found",
                "dept_id": dept_id,
            },
        )

    vault = _get_vault_client(request)

    # Check if there's actually an overlap window active
    overlap_remaining = await _read_webhook_overlap_remaining(
        vault, dept_id, provider
    )
    if overlap_remaining is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "no_overlap_active",
                "dept_id": dept_id,
                "provider": provider,
                "message": (
                    "No active overlap window to finalize — either "
                    "rotation was already finalized or no rotation "
                    "has occurred."
                ),
            },
        )

    try:
        await _finalize_webhook_secret(vault, dept_id, provider)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "finalization_validation_failed",
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.error(
            "Webhook secret finalization failed for dept=%s provider=%s: %s",
            dept_id,
            provider,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "webhook_secret_finalization_failed",
                "dept_id": dept_id,
                "provider": provider,
                "message": str(exc),
            },
        ) from exc

    finalized_at = datetime.now(timezone.utc)

    # Audit
    await _write_webhook_audit(
        request,
        actor=actor,
        action=_AUDIT_WEBHOOK_SECRET_ROTATION_FINALIZED,
        dept_id=dept_id,
        provider=provider,
        payload={
            "dept_id": dept_id,
            "provider": provider,
            "finalized_at": finalized_at.isoformat(),
        },
    )

    return WebhookFinalizeResponse(
        dept_id=dept_id,
        provider=provider,
        finalized_at=finalized_at.isoformat(),
        previous_slot_cleared=True,
    )
