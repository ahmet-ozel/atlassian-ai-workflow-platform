"""REST router for external provider status probes (task 10.3, R10).

Exposes a single endpoint:

* ``GET /api/v1/services/external`` — reads ``kind="external"`` entries
  from ``config/services.manifest.json``, probes each via
  :func:`~src.lifecycle.external_probe.probe_external`, and returns the
  aggregated results.

The endpoint is gated on ``Depends(require_admin)`` per the existing
admin-dashboard RBAC pattern.

Design references
-----------------
* design.md §R10 — External Provider Downtime Widget.
* tasks.md task 10.3 — Admin Dashboard API: external endpoint.
* Requirements 10.3.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth.dependencies import require_admin
from ..lifecycle.external_probe import (
    ExternalProbeResult,
    VaultCredentialReader,
    emit_probe_audit,
    probe_external,
)

logger = logging.getLogger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/services",
    tags=["external-providers"],
    dependencies=[Depends(require_admin)],
)

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ExternalServiceResponse(BaseModel):
    """Single external provider probe result."""

    name: str
    kind: str = "external"
    base_url: str
    status: str
    last_probed_at: float
    latency_ms: float | None = None
    error: str | None = None


class ExternalServicesListResponse(BaseModel):
    """Aggregated response for all external providers."""

    services: list[ExternalServiceResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_external_entries(request: Request) -> list[dict[str, Any]]:
    """Load ``kind="external"`` entries from the services manifest.

    Reads the raw JSON from disk (via workspace_root) rather than the
    parsed :class:`ManagedServiceEntry` tuple on ``app.state.manifest``
    because the manifest loader filters to managed service kinds only.
    External entries use a different schema shape with fields like
    ``base_url_env``, ``probe_path``, etc.

    Falls back to ``app.state.workspace_root`` → Settings when the
    lifespan slot is not wired.
    """
    workspace_root = getattr(request.app.state, "workspace_root", None)
    if workspace_root is None:
        from ..config import Settings

        workspace_root = Settings().workspace_root

    if workspace_root is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "workspace_root_unavailable",
            },
        )

    manifest_path = Path(workspace_root) / "config" / "services.manifest.json"
    if not manifest_path.exists():
        # Try alternate layout (repo root vs platform/ root)
        alt_path = (
            Path(workspace_root) / "platform" / "config" / "services.manifest.json"
        )
        if alt_path.exists():
            manifest_path = alt_path
        else:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "not_ready",
                    "reason": "manifest_not_found",
                },
            )

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read services manifest: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "manifest_read_error",
            },
        ) from exc

    services = raw.get("services", [])
    return [
        entry
        for entry in services
        if entry.get("kind") == "external" and _external_entry_is_enabled(entry)
    ]


def _env_is_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _external_entry_is_enabled(entry: dict[str, Any]) -> bool:
    """Return whether an external provider should be surfaced/probed.

    Optional providers are hidden until the operator explicitly enables
    them or provides a concrete base URL via the configured environment
    variable. Built-in defaults are not enough to enable an optional
    provider; otherwise unused fallbacks such as vLLM or Anthropic look
    broken in the Dashboard even when the active provider is OpenAI.
    """

    if not bool(entry.get("optional", False)):
        return True

    enabled_env = str(entry.get("enabled_env") or "").strip()
    if enabled_env and _env_is_truthy(enabled_env):
        return True

    base_url_env = str(entry.get("base_url_env") or "").strip()
    if base_url_env and os.environ.get(base_url_env, "").strip():
        return True

    return False


def _get_vault_reader(request: Request) -> VaultCredentialReader | None:
    """Build a VaultCredentialReader from app state if available."""
    vault_client = getattr(request.app.state, "vault_client", None)
    if vault_client is None:
        return None
    return VaultCredentialReader(vault_client=vault_client)


def _get_http_client(request: Request):
    """Resolve the shared httpx.AsyncClient from app state."""
    return getattr(request.app.state, "http_client", None)


def _get_audit_writer(request: Request):
    """Resolve the AuditWriter from app state for probe audit emission."""
    return getattr(request.app.state, "audit_writer", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/external",
    response_model=ExternalServicesListResponse,
    summary="List external provider statuses",
    description=(
        "Reads kind='external' entries from the services manifest, "
        "probes each provider, and returns aggregated status results. "
        "Results are cached for 30 seconds per provider."
    ),
)
async def list_external_services(
    request: Request,
    bypass_cache: bool = Query(
        default=False,
        description="Force fresh probes, bypassing the 30s cache.",
    ),
) -> ExternalServicesListResponse:
    """Probe all external providers and return their statuses.

    Implements Requirement 10.3: ``GET /api/v1/services/external``.
    """
    entries = _load_external_entries(request)

    if not entries:
        return ExternalServicesListResponse(services=[])

    http_client = _get_http_client(request)
    vault_reader = _get_vault_reader(request)
    audit_writer = _get_audit_writer(request)

    results: list[ExternalServiceResponse] = []
    for entry in entries:
        probe_result: ExternalProbeResult = await probe_external(
            entry,
            http_client=http_client,
            vault_reader=vault_reader,
            bypass_cache=bypass_cache,
        )

        # Emit audit entries for failed probes and streak alerts
        # (Requirement 10.7, task 10.4).
        try:
            await emit_probe_audit(probe_result, audit_writer=audit_writer)
        except Exception as exc:  # noqa: BLE001 - probe visibility must survive audit drift
            logger.warning(
                "external provider audit emission failed for %s: %s",
                probe_result.name,
                exc,
            )

        results.append(
            ExternalServiceResponse(
                name=probe_result.name,
                kind="external",
                base_url=probe_result.base_url,
                status=probe_result.status,
                last_probed_at=probe_result.last_probed_at,
                latency_ms=probe_result.latency_ms,
                error=probe_result.error,
            )
        )

    return ExternalServicesListResponse(services=results)
