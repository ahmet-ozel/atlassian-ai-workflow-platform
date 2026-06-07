"""``POST /auth/bootstrap`` - one-time admin user creation endpoint.

This router exposes the bootstrap token consumption flow described in
behavior 2. The endpoint is intentionally **unauthenticated** because
it exists to create the very first admin user before any OIDC provider
is configured.

Flow:
1. Client sends ``POST /auth/bootstrap`` with ``{"token": "<token>"}``.
2. If OIDC is already configured → 410 Gone (bootstrap disabled).
3. If the token format is invalid → 400 Bad Request.
4. If the token is valid → create admin user, invalidate token → 201.
5. If the token is expired or already consumed → 401 Unauthorized.

Security considerations:
- The endpoint is rate-limited by the global rate limiter middleware.
- No authentication is required (this IS the authentication bootstrap).
- Once OIDC is configured, the endpoint is permanently disabled (410).
- The token is single-use and has a 1-hour TTL.

"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

__all__ = ["router"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/auth",
    tags=["auth"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class BootstrapRequest(BaseModel):
    """Request body for the bootstrap endpoint."""

    token: str = Field(
        ...,
        min_length=1,
        description="The one-time bootstrap token printed to stdout on first boot.",
    )


class BootstrapSuccessResponse(BaseModel):
    """Response body on successful admin creation."""

    user_id: str
    message: str = "admin_created"


# ---------------------------------------------------------------------------
# Token format validation
# ---------------------------------------------------------------------------

# ``secrets.token_urlsafe(32)`` produces a 43-character base64url string.
# We accept tokens that are base64url-safe characters (A-Z, a-z, 0-9, -, _)
# with a reasonable length range (32-64 chars) to allow for future changes
# in token generation while rejecting obviously malformed input.
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{32,64}$")


def _is_valid_token_format(token: str) -> bool:
    """Check whether the token matches the expected format.

    Returns ``True`` for well-formed base64url tokens of 32-64 chars.
    Returns ``False`` for empty strings, tokens with invalid characters,
    or tokens outside the expected length range.
    """
    return bool(_TOKEN_PATTERN.match(token))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_db_pool(request: Request):
    """Return the asyncpg pool from app state, or raise 503."""
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "pg_pool_unavailable",
            },
        )
    return pool


# ---------------------------------------------------------------------------
# POST /auth/bootstrap
# ---------------------------------------------------------------------------


@router.post(
    "/bootstrap",
    summary="Consume bootstrap token and create first admin user",
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Admin user created successfully"},
        400: {"description": "Invalid token format"},
        401: {"description": "Token expired or already used"},
        410: {"description": "Bootstrap disabled - OIDC is active"},
        503: {"description": "Database unavailable"},
    },
)
async def bootstrap_admin(
    request: Request,
    body: BootstrapRequest,
) -> BootstrapSuccessResponse:
    """Consume a one-time bootstrap token to create the first admin user.

    This endpoint is unauthenticated by design - it exists to bootstrap
    the very first admin before OIDC is configured.

    """

    from ..auth.bootstrap import BootstrapTokenService

    bootstrap_service = BootstrapTokenService()

    # ---- 1. Check if OIDC is configured (behavior 2.5) ----
    if await bootstrap_service.is_oidc_configured():
        logger.info(
            "bootstrap attempt rejected - OIDC provider is active"
        )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "bootstrap_disabled_oidc_active"},
        )

    # ---- 2. Validate token format (behavior 2.3) ----
    if not _is_valid_token_format(body.token):
        logger.warning(
            "bootstrap attempt rejected - invalid token format"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_token_format"},
        )

    # ---- 3. Validate and consume the token (behaviors 2.3, 2.4) ----
    db_pool = _get_db_pool(request)

    consumed = await bootstrap_service.validate_and_consume(
        token=body.token,
        db_pool=db_pool,
    )

    if not consumed:
        logger.warning(
            "bootstrap attempt rejected - token expired or already used"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "bootstrap_token_expired_or_used"},
        )

    # ---- 4. Create the admin user (behavior 2.3) ----
    user_id = str(uuid.uuid4())

    async with db_pool.acquire() as conn:
        # Create the admin user in the auth schema.
        # The table may not exist yet in all environments - we use
        # a CREATE TABLE IF NOT EXISTS guard so the bootstrap flow
        # works even on a fresh database where only the
        # bootstrap_tokens migration has run.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth.users (
                id UUID PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'admin',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_via TEXT NOT NULL DEFAULT 'bootstrap'
            )
            """
        )

        await conn.execute(
            """
            INSERT INTO auth.users (id, role, created_via)
            VALUES ($1, 'admin', 'bootstrap')
            """,
            uuid.UUID(user_id),
        )

    logger.info(
        "bootstrap admin user created (user_id=%s)",
        user_id,
    )

    return BootstrapSuccessResponse(user_id=user_id)
