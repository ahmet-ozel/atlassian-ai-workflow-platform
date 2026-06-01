"""``POST /admin/vault/init`` — Vault production initialization endpoint.

This router exposes the Vault operator init flow described in
Requirement 7. The endpoint executes ``vault operator init`` with
5 key shares and 3 threshold, returning the unseal keys and root
token for one-time display by the Setup Wizard UI.

Flow:
1. Check if Vault is already initialized via ``/v1/sys/init``.
2. If already initialized → 409 Conflict.
3. Execute init via ``/v1/sys/init`` PUT with key shares/threshold.
4. Write root token to Vault's own secret engine for safekeeping.
5. Return unseal keys and root token (one-time display).

Security considerations:
- The unseal keys and root token are returned ONLY once.
- After the response is sent, the keys exist only in the operator's
  possession and (for the root token) in Vault's secret engine.
- This endpoint should be protected by admin authentication in
  production.

Requirements: 7.1, 7.3, 7.6
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

__all__ = ["router"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/admin/vault",
    tags=["vault"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class VaultInitResponse(BaseModel):
    """Response body on successful Vault initialization."""

    unseal_keys: list[str] = Field(
        ...,
        description="5 Shamir unseal key shares (base64-encoded).",
    )
    unseal_keys_base64: list[str] = Field(
        ...,
        description="5 Shamir unseal key shares (base64-encoded, same as unseal_keys).",
    )
    root_token: str = Field(
        ...,
        description="Root token for Vault access (one-time display).",
    )
    message: str = "vault_initialized"


class VaultInitRequest(BaseModel):
    """Optional request body for Vault init (allows overriding defaults)."""

    secret_shares: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of key shares to split the master key into.",
    )
    secret_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of key shares required to reconstruct the master key.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_vault_addr(request: Request) -> str:
    """Return the Vault address from app settings."""
    from ..config import Settings

    settings = Settings()
    return settings.vault_addr


def _get_http_client(request: Request) -> httpx.AsyncClient:
    """Return the shared httpx client from app state, or create one."""
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "http_client_unavailable",
            },
        )
    return client


def _get_vault_token(request: Request) -> str:
    """Return the current Vault token from app settings."""
    from ..config import Settings

    settings = Settings()
    return settings.vault_token


# ---------------------------------------------------------------------------
# POST /admin/vault/init
# ---------------------------------------------------------------------------


@router.post(
    "/init",
    summary="Initialize Vault in production mode with Shamir key shares",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"description": "Vault initialized successfully — one-time key display"},
        409: {"description": "Vault is already initialized"},
        502: {"description": "Vault communication error"},
        503: {"description": "HTTP client unavailable"},
    },
)
async def vault_init(
    request: Request,
    body: VaultInitRequest | None = None,
) -> VaultInitResponse:
    """Initialize Vault with Shamir secret sharing for production use.

    Executes ``vault operator init`` with the specified number of key
    shares (default 5) and threshold (default 3). Returns the unseal
    keys and root token for one-time display.

    After initialization, the root token is written to Vault's own
    secret engine for safekeeping.

    **Validates: Requirements 7.1, 7.3, 7.6**
    """

    if body is None:
        body = VaultInitRequest()

    # Validate threshold <= shares
    if body.secret_threshold > body.secret_shares:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_parameters",
                "message": "secret_threshold must be <= secret_shares",
            },
        )

    vault_addr = _get_vault_addr(request)
    http_client = _get_http_client(request)

    # ---- 1. Check if Vault is already initialized (Requirement 7.6) ----
    try:
        init_status_resp = await http_client.get(
            f"{vault_addr}/v1/sys/init",
        )
    except httpx.HTTPError as exc:
        logger.error("Failed to check Vault init status: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_communication_error",
                "message": "Failed to communicate with Vault server",
            },
        )

    if init_status_resp.status_code != 200:
        logger.error(
            "Vault /v1/sys/init returned unexpected status: %d",
            init_status_resp.status_code,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_communication_error",
                "message": f"Vault returned status {init_status_resp.status_code}",
            },
        )

    init_status = init_status_resp.json()
    if init_status.get("initialized", False):
        logger.info("Vault init attempt rejected — already initialized")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "vault_already_initialized"},
        )

    # ---- 2. Initialize Vault (Requirement 7.1) ----
    # Execute vault operator init via the HTTP API with 5 key shares
    # and 3 threshold (configurable via request body).
    init_payload = {
        "secret_shares": body.secret_shares,
        "secret_threshold": body.secret_threshold,
    }

    try:
        init_resp = await http_client.put(
            f"{vault_addr}/v1/sys/init",
            json=init_payload,
        )
    except httpx.HTTPError as exc:
        logger.error("Failed to initialize Vault: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_init_failed",
                "message": "Failed to communicate with Vault during initialization",
            },
        )

    if init_resp.status_code != 200:
        logger.error(
            "Vault init returned unexpected status: %d — %s",
            init_resp.status_code,
            init_resp.text[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_init_failed",
                "message": f"Vault init returned status {init_resp.status_code}",
            },
        )

    init_result = init_resp.json()

    unseal_keys = init_result.get("keys", [])
    unseal_keys_base64 = init_result.get("keys_base64", [])
    root_token = init_result.get("root_token", "")

    if not unseal_keys or not root_token:
        logger.error("Vault init response missing keys or root_token")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_init_failed",
                "message": "Vault init response missing expected fields",
            },
        )

    logger.info(
        "Vault initialized successfully with %d key shares and threshold %d",
        body.secret_shares,
        body.secret_threshold,
    )

    # ---- 3. Write root token to Vault's secret engine (Requirement 7.3) ----
    # After init, we use the new root token to authenticate and store
    # the root token in Vault's own KV-v2 secret engine for safekeeping.
    try:
        await http_client.post(
            f"{vault_addr}/v1/secret/data/platform/root-token",
            headers={"X-Vault-Token": root_token},
            json={"data": {"token": root_token}},
        )
        logger.info("Root token written to Vault secret engine")
    except httpx.HTTPError as exc:
        # Non-fatal — the operator still has the token in the response.
        # Log a warning so they know the write-back failed.
        logger.warning(
            "Failed to write root token to Vault secret engine: %s. "
            "The token is still returned in this response.",
            exc,
        )

    return VaultInitResponse(
        unseal_keys=unseal_keys,
        unseal_keys_base64=unseal_keys_base64,
        root_token=root_token,
    )
