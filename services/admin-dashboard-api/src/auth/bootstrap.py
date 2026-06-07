"""One-time admin bootstrap token management.

This module implements the :class:`BootstrapTokenService` which handles
the initial admin access flow described in behavior 2. On first boot
(when no admin user exists in the database), a cryptographically secure
one-time token is generated, hashed (SHA-256), and stored in the
``auth.bootstrap_tokens`` table. The plain token is printed to stdout
exactly once so the operator can use it to create the first admin user
via ``POST /auth/bootstrap``.

Once OIDC is configured the bootstrap mechanism is disabled entirely
(behavior 2.5).

Security properties:
- Plain token is NEVER persisted - only the SHA-256 hash is stored.
- Token has a 1-hour TTL after which it becomes invalid.
- Token is single-use: consumed on first successful validation.
- Token generation is idempotent: if a valid (unexpired, unconsumed)
  token already exists, no new token is generated.

"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class BootstrapTokenService:
    """One-time admin bootstrap token yönetimi."""

    TOKEN_TTL = timedelta(hours=1)

    async def generate_if_needed(self, db_pool) -> str | None:
        """Generate a bootstrap token if no admin user exists in the DB.

        Returns the plain token string when a new token is generated,
        or ``None`` if an admin already exists or a valid token is
        already pending.

        The plain token is printed to stdout so the operator can
        retrieve it from container logs on first boot.
        """

        async with db_pool.acquire() as conn:
            # Check if any admin user already exists
            admin_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM auth.bootstrap_tokens
                    WHERE consumed_at IS NOT NULL
                )
                """
            )
            if admin_exists:
                logger.info(
                    "bootstrap token generation skipped - admin already "
                    "bootstrapped"
                )
                return None

            # Check if a valid (unexpired, unconsumed) token already exists
            valid_token_exists = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM auth.bootstrap_tokens
                    WHERE consumed_at IS NULL
                      AND expires_at > now()
                )
                """
            )
            if valid_token_exists:
                logger.info(
                    "bootstrap token generation skipped - valid pending "
                    "token already exists"
                )
                return None

            # Generate a new cryptographically secure token
            plain_token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(plain_token.encode()).hexdigest()
            expires_at = datetime.now(timezone.utc) + self.TOKEN_TTL

            await conn.execute(
                """
                INSERT INTO auth.bootstrap_tokens (token_hash, expires_at)
                VALUES ($1, $2)
                """,
                token_hash,
                expires_at,
            )

            # Print to stdout for operator retrieval (behavior 2.1)
            print(  # noqa: T201
                f"\n{'=' * 60}\n"
                f"  BOOTSTRAP ADMIN TOKEN (one-time use, expires in 1 hour)\n"
                f"  Token: {plain_token}\n"
                f"  Use: POST /auth/bootstrap with "
                f'{{"token": "<token>"}}\n'
                f"{'=' * 60}\n"
            )

            logger.info(
                "bootstrap token generated - expires at %s",
                expires_at.isoformat(),
            )

            return plain_token

    async def validate_and_consume(self, token: str, db_pool) -> bool:
        """Validate a bootstrap token and mark it as consumed.

        Returns ``True`` if the token was valid and successfully
        consumed (admin creation should proceed). Returns ``False``
        if the token is expired, already consumed, or not found.
        """

        token_hash = hashlib.sha256(token.encode()).hexdigest()

        async with db_pool.acquire() as conn:
            # Attempt to consume the token atomically
            row = await conn.fetchrow(
                """
                UPDATE auth.bootstrap_tokens
                SET consumed_at = now()
                WHERE token_hash = $1
                  AND consumed_at IS NULL
                  AND expires_at > now()
                RETURNING id
                """,
                token_hash,
            )

            if row is None:
                logger.warning(
                    "bootstrap token validation failed - token not found, "
                    "expired, or already consumed"
                )
                return False

            logger.info(
                "bootstrap token consumed successfully (id=%s)",
                row["id"],
            )
            return True

    async def is_oidc_configured(self) -> bool:
        """Check whether an OIDC provider is actively configured.

        When OIDC is configured, the bootstrap token mechanism is
        disabled (behavior 2.5). This checks for the presence of
        the required OIDC environment variables with non-empty values.
        """

        oidc_issuer = os.environ.get("OIDC_ISSUER", "").strip()
        oidc_audience = os.environ.get("OIDC_AUDIENCE", "").strip()
        oidc_jwks_url = os.environ.get("OIDC_JWKS_URL", "").strip()
        auth_mode = os.environ.get("AUTH_MODE", "dev").strip().lower()

        # OIDC is considered configured when auth_mode is "production"
        # AND all three OIDC parameters are set to non-empty values.
        is_configured = (
            auth_mode == "production"
            and bool(oidc_issuer)
            and bool(oidc_audience)
            and bool(oidc_jwks_url)
        )

        if is_configured:
            logger.info(
                "OIDC provider is configured (issuer=%s) - bootstrap "
                "mechanism disabled",
                oidc_issuer,
            )

        return is_configured
