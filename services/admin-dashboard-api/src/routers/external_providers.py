"""REST router for external provider status probes.

Exposes a single endpoint:

* ``GET /api/v1/services/external`` - reads ``kind="external"`` entries
  from ``config/services.manifest.json``, probes each via
  :func:`~src.lifecycle.external_probe.probe_external`, and returns the
  aggregated results.

The endpoint is gated on ``Depends(require_admin)`` per the existing
admin-dashboard RBAC pattern.

External provider probes feed the admin dashboard's downtime view.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

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


def _load_external_entries(
    request: Request,
    *,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
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
        if entry.get("kind") == "external" and _external_entry_is_enabled(entry, env=env)
    ]


def _env_is_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def _external_entry_is_enabled(
    entry: dict[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return whether an external provider should be surfaced/probed.

    Optional providers are hidden until the operator explicitly enables
    them, provides a credential, or provides a concrete base URL that
    differs from the built-in fallback. Compose may inject fallback
    defaults such as VLLM_BASE_URL even when the operator never selected
    that provider; those defaults must not make the Dashboard show a red
    provider card.
    """

    if not bool(entry.get("optional", False)):
        return True

    source = os.environ if env is None else env

    enabled_env = str(entry.get("enabled_env") or "").strip()
    if enabled_env and source.get(enabled_env, "").strip().lower() in _TRUE_VALUES:
        return True

    credential_env = str(entry.get("credential_env") or "").strip()
    if credential_env and source.get(credential_env, "").strip():
        return True

    base_url_env = str(entry.get("base_url_env") or "").strip()
    configured_base_url = source.get(base_url_env, "").strip() if base_url_env else ""
    default_base_url = str(entry.get("base_url_default") or "").strip()
    if configured_base_url and configured_base_url.rstrip("/") != default_base_url.rstrip("/"):
        return True

    return False


async def _get_model_env(request: Request) -> dict[str, str]:
    """Return env values from process env plus Dashboard-entered service overrides."""

    env = dict(os.environ)
    vault_client = getattr(request.app.state, "vault_client", None)
    if vault_client is None:
        return env

    for service_name in ("streamlit-ui", "assistant-service", "admin-dashboard-api"):
        try:
            overrides = await vault_client.read_env_overrides(service_name=service_name)
        except Exception as exc:  # noqa: BLE001 - status widget must not break services page
            logger.debug("model env override read failed for %s: %s", service_name, exc)
            continue
        for key, value in overrides.items():
            if value and not env.get(key):
                env[key] = value

    return env


def _get_vault_reader(request: Request) -> VaultCredentialReader | None:
    """Build a VaultCredentialReader from app state if available."""
    vault_client = getattr(request.app.state, "vault_client", None)
    if vault_client is None:
        return None
    return VaultCredentialReader(vault_client=vault_client)


def _get_http_client(request: Request):
    """Return a client for external provider probes.

    The app-wide client is intentionally created with ``trust_env=False`` so
    internal service-to-service calls are not routed through a workstation
    proxy. Public AI provider probes are the opposite: on developer machines
    and corporate networks they often need the host proxy/cert environment.
    Let ``probe_external`` create its short-lived default client so those env
    settings are honored.
    """
    return None


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

    Implements ``GET /api/v1/services/external``.
    """
    effective_env = await _get_model_env(request)
    entries = _load_external_entries(request, env=effective_env)

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
            env=effective_env,
        )

        # Emit audit entries for failed probes and streak alerts.
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
