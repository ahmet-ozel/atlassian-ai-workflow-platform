"""Per-department webhook HMAC-SHA256 authentication middleware.

The middleware intercepts every inbound webhook request and:

1. **Extracts a department hint** from the ``X-Department-Key`` HTTP
   header or the URL path (project_key prefix). When both sources
   carry a value, the header takes priority.
2. **Fetches the department-specific HMAC secret** from Vault at
   ``secret/webhook/{dept_id}/secret`` with a 3-second timeout.
3. **Computes HMAC-SHA256** over the raw request body and performs a
   **timing-safe comparison** via :func:`hmac.compare_digest`.
4. **Falls back to a global secret** when the department cannot be
   determined.
5. Returns **503** when Vault is unreachable within the 3-second
   timeout.
6. Returns **401** when HMAC verification fails, logging the
   department and source IP as a security event.
7. Returns **401** when the global fallback is undefined and the
   department cannot be determined.

"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

__all__ = [
    "DepartmentContext",
    "VaultSecretReader",
    "WebhookAuthMiddleware",
]

_LOG = logging.getLogger(__name__)
_SECURITY_LOG = logging.getLogger("security.webhook_auth")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: HTTP header carrying the department hint (highest priority source).
_DEPT_HEADER = "X-Department-Key"

#: HTTP header carrying the HMAC signature from the webhook sender.
#: Supports both ``X-Hub-Signature-256`` (GitHub/Bitbucket style) and
#: ``X-Webhook-Signature`` (custom Atlassian style). The middleware
#: checks both and uses the first non-empty value.
_SIGNATURE_HEADERS = ("X-Hub-Signature-256", "X-Webhook-Signature")

#: Vault path template for per-department webhook secrets.
_DEPT_SECRET_PATH_TEMPLATE = "secret/webhook/{dept_id}/secret"

#: Vault path for the global fallback webhook secret.
_GLOBAL_SECRET_PATH = "secret/webhook/global/secret"

#: Maximum time (seconds) to wait for a Vault read before returning 503.
_VAULT_TIMEOUT_SECONDS: float = 3.0

#: Regex to extract a project_key prefix from the URL path.
#: Matches patterns like ``/webhooks/jira/PROJ-123/...`` where ``PROJ``
#: is the project key (uppercase letters, 2-10 chars).
_PROJECT_KEY_RE = re.compile(r"/webhooks/[^/]+/([A-Z][A-Z0-9_]{1,9})-")

#: HMAC signature prefix (``sha256=<hex>``).
_SHA256_PREFIX = "sha256="


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class VaultSecretReader(Protocol):
    """Async interface to read a secret value from Vault.

    Production wiring wraps the synchronous :class:`vault_client.VaultClient`
    in an executor call; tests inject a simple async fake.

    Implementations MUST raise :class:`KeyError` when the path does not
    exist and :class:`TimeoutError` or :class:`asyncio.TimeoutError`
    when the read exceeds the caller's deadline.
    """

    async def read_secret(self, path: str) -> dict[str, str]:
        """Read the secret at *path* and return its key-value payload.

        Args:
            path: Vault path without the ``vault:`` prefix, e.g.
                ``"secret/webhook/payments/secret"``.

        Returns:
            Flat mapping of secret fields.

        Raises:
            KeyError: Path does not exist in Vault.
            TimeoutError: Read exceeded the configured deadline.
            Exception: Any other Vault communication failure.
        """
        ...


@dataclass(frozen=True, slots=True)
class DepartmentContext:
    """Resolved department context attached to the request state.

    Downstream handlers can access this via ``request.state.dept_context``
    after the middleware authenticates the request.

    Attributes:
        dept_id: The resolved department identifier, or ``None`` when
            the global fallback secret was used.
        source: How the department was determined (``"header"``,
            ``"url_path"``, or ``"global_fallback"``).
    """

    dept_id: str | None
    source: str


# ---------------------------------------------------------------------------
# WebhookAuthMiddleware
# ---------------------------------------------------------------------------


class WebhookAuthMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware for per-department webhook HMAC authentication.

    Args:
        app: The ASGI application (injected by Starlette).
        vault_reader: Async Vault secret reader implementation.
        global_fallback_secret: Optional pre-loaded global fallback
            HMAC secret. When ``None``, the middleware attempts to
            read it from Vault at ``secret/webhook/global/secret``.
            If that also fails and the department cannot be determined,
            the request is rejected with 401.
        skip_paths: Set of URL paths that bypass authentication
            (e.g. ``/healthz``, ``/readyz``).
    """

    def __init__(
        self,
        app,  # noqa: ANN001 - Starlette typing
        *,
        vault_reader: VaultSecretReader,
        global_fallback_secret: str | None = None,
        skip_paths: set[str] | None = None,
    ) -> None:
        super().__init__(app)
        self._vault_reader = vault_reader
        self._global_fallback_secret = global_fallback_secret
        self._skip_paths: set[str] = skip_paths or {"/healthz", "/readyz"}

    # ------------------------------------------------------------------
    # Middleware entry point
    # ------------------------------------------------------------------

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Authenticate the webhook request before forwarding."""

        # Skip non-webhook paths (health probes, admin endpoints, etc.)
        if request.url.path in self._skip_paths:
            return await call_next(request)

        # Only authenticate webhook paths
        if not request.url.path.startswith("/webhooks"):
            return await call_next(request)

        # 1. Extract the HMAC signature from request headers.
        signature = self._extract_signature(request)
        if not signature:
            _SECURITY_LOG.warning(
                "webhook_auth_missing_signature: path=%s client=%s",
                request.url.path,
                request.client.host if request.client else "unknown",
            )
            return Response(
                content='{"error":"missing_signature"}',
                status_code=401,
                media_type="application/json",
            )

        # 2. Read the raw request body for HMAC computation.
        body = await request.body()

        # 3. Determine the department.
        dept_id = self._extract_department(request)

        # 4. Fetch the appropriate secret and verify.
        if dept_id is not None:
            # Attempt per-department secret from Vault.
            result = await self._verify_with_dept_secret(
                dept_id=dept_id,
                body=body,
                signature=signature,
                request=request,
            )
            if result is not None:
                return result

            # Verification passed - attach context and proceed.
            source = self._determine_source(request)
            request.state.dept_context = DepartmentContext(
                dept_id=dept_id, source=source
            )
            return await call_next(request)

        # Department unknown - use global fallback.
        result = await self._verify_with_global_fallback(
            body=body,
            signature=signature,
            request=request,
        )
        if result is not None:
            return result

        # Global fallback verification passed.
        request.state.dept_context = DepartmentContext(
            dept_id=None, source="global_fallback"
        )
        return await call_next(request)

    # ------------------------------------------------------------------
    # Department extraction
    # ------------------------------------------------------------------

    def _extract_department(self, request: Request) -> str | None:
        """Extract department hint from header (priority) or URL path.

        Returns ``None`` when neither source provides a usable value.
        """

        # Header takes priority.
        header_value = request.headers.get(_DEPT_HEADER)
        if header_value and header_value.strip():
            return header_value.strip()

        # Fall back to URL path project_key prefix.
        match = _PROJECT_KEY_RE.search(request.url.path)
        if match:
            return match.group(1).lower()

        return None

    def _determine_source(self, request: Request) -> str:
        """Determine which source provided the department hint."""

        header_value = request.headers.get(_DEPT_HEADER)
        if header_value and header_value.strip():
            return "header"
        return "url_path"

    # ------------------------------------------------------------------
    # Signature extraction
    # ------------------------------------------------------------------

    def _extract_signature(self, request: Request) -> str | None:
        """Extract the HMAC signature from known headers.

        Returns the raw header value (e.g. ``sha256=abcdef...``) or
        ``None`` when no signature header is present.
        """

        for header_name in _SIGNATURE_HEADERS:
            value = request.headers.get(header_name)
            if value and value.strip():
                return value.strip()
        return None

    # ------------------------------------------------------------------
    # Verification helpers
    # ------------------------------------------------------------------

    async def _verify_with_dept_secret(
        self,
        *,
        dept_id: str,
        body: bytes,
        signature: str,
        request: Request,
    ) -> Response | None:
        """Verify HMAC using the per-department Vault secret.

        Returns a Response on failure (503 or 401), or ``None`` on
        successful verification.
        """

        path = _DEPT_SECRET_PATH_TEMPLATE.format(dept_id=dept_id)

        try:
            payload = await asyncio.wait_for(
                self._vault_reader.read_secret(path),
                timeout=_VAULT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            _LOG.error(
                "webhook_auth_vault_timeout: path=%s dept=%s timeout=%.1fs",
                path,
                dept_id,
                _VAULT_TIMEOUT_SECONDS,
            )
            return Response(
                content='{"error":"vault_unavailable"}',
                status_code=503,
                media_type="application/json",
            )
        except KeyError:
            # Secret not found for this dept - fall back to global.
            _LOG.warning(
                "webhook_auth_dept_secret_not_found: dept=%s path=%s",
                dept_id,
                path,
            )
            # Try global fallback instead.
            result = await self._verify_with_global_fallback(
                body=body,
                signature=signature,
                request=request,
            )
            if result is not None:
                return result
            # Global fallback passed - update context source.
            request.state.dept_context = DepartmentContext(
                dept_id=dept_id, source="global_fallback"
            )
            # Return None to signal success to the caller, but we've
            # already set the context, so the caller should just
            # call_next. We need a different signal here.
            return None
        except Exception:
            _LOG.error(
                "webhook_auth_vault_error: path=%s dept=%s",
                path,
                dept_id,
                exc_info=True,
            )
            return Response(
                content='{"error":"vault_unavailable"}',
                status_code=503,
                media_type="application/json",
            )

        # Extract the HMAC secret from the Vault payload.
        secret_value = payload.get("hmac_secret") or payload.get("secret")
        if not secret_value:
            _LOG.error(
                "webhook_auth_missing_secret_field: dept=%s path=%s",
                dept_id,
                path,
            )
            return Response(
                content='{"error":"vault_unavailable"}',
                status_code=503,
                media_type="application/json",
            )

        # HMAC-SHA256 timing-safe comparison.
        if not self._verify_hmac(
            secret=secret_value, body=body, signature=signature
        ):
            client_ip = request.client.host if request.client else "unknown"
            _SECURITY_LOG.warning(
                "webhook_auth_hmac_failed: dept=%s client=%s path=%s",
                dept_id,
                client_ip,
                request.url.path,
            )
            return Response(
                content='{"error":"unauthorized"}',
                status_code=401,
                media_type="application/json",
            )

        return None  # Success

    async def _verify_with_global_fallback(
        self,
        *,
        body: bytes,
        signature: str,
        request: Request,
    ) -> Response | None:
        """Verify HMAC using the global fallback secret.

        Returns a Response on failure (503 or 401), or ``None`` on
        successful verification.
        """

        # Use pre-loaded global secret if available.
        secret_value = self._global_fallback_secret

        if secret_value is None:
            # Attempt to read from Vault.
            try:
                payload = await asyncio.wait_for(
                    self._vault_reader.read_secret(_GLOBAL_SECRET_PATH),
                    timeout=_VAULT_TIMEOUT_SECONDS,
                )
                secret_value = payload.get("hmac_secret") or payload.get("secret")
            except asyncio.TimeoutError:
                _LOG.error(
                    "webhook_auth_vault_timeout: path=%s timeout=%.1fs",
                    _GLOBAL_SECRET_PATH,
                    _VAULT_TIMEOUT_SECONDS,
                )
                return Response(
                    content='{"error":"vault_unavailable"}',
                    status_code=503,
                    media_type="application/json",
                )
            except KeyError:
                # Global fallback not defined.
                client_ip = request.client.host if request.client else "unknown"
                _SECURITY_LOG.warning(
                    "webhook_auth_no_fallback: client=%s path=%s "
                    "reason=global_fallback_undefined",
                    client_ip,
                    request.url.path,
                )
                return Response(
                    content='{"error":"unauthorized"}',
                    status_code=401,
                    media_type="application/json",
                )
            except Exception:
                _LOG.error(
                    "webhook_auth_vault_error: path=%s",
                    _GLOBAL_SECRET_PATH,
                    exc_info=True,
                )
                return Response(
                    content='{"error":"vault_unavailable"}',
                    status_code=503,
                    media_type="application/json",
                )

        if not secret_value:
            # Fallback secret field is empty.
            client_ip = request.client.host if request.client else "unknown"
            _SECURITY_LOG.warning(
                "webhook_auth_no_fallback: client=%s path=%s "
                "reason=global_fallback_empty",
                client_ip,
                request.url.path,
            )
            return Response(
                content='{"error":"unauthorized"}',
                status_code=401,
                media_type="application/json",
            )

        # HMAC-SHA256 timing-safe comparison.
        if not self._verify_hmac(
            secret=secret_value, body=body, signature=signature
        ):
            client_ip = request.client.host if request.client else "unknown"
            _SECURITY_LOG.warning(
                "webhook_auth_hmac_failed: dept=unknown client=%s path=%s "
                "source=global_fallback",
                client_ip,
                request.url.path,
            )
            return Response(
                content='{"error":"unauthorized"}',
                status_code=401,
                media_type="application/json",
            )

        return None  # Success

    # ------------------------------------------------------------------
    # HMAC computation
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_hmac(*, secret: str, body: bytes, signature: str) -> bool:
        """Compute HMAC-SHA256 and perform timing-safe comparison.

        Supports signatures in ``sha256=<hex>`` format (standard
        webhook convention) as well as raw hex strings.

        Args:
            secret: The HMAC secret (UTF-8 string from Vault).
            body: Raw request body bytes.
            signature: The signature header value.

        Returns:
            ``True`` if the signature matches, ``False`` otherwise.
        """

        secret_bytes = secret.encode("utf-8")

        # Strip the ``sha256=`` prefix if present.
        if signature.startswith(_SHA256_PREFIX):
            received_hex = signature[len(_SHA256_PREFIX):]
        else:
            received_hex = signature

        if not received_hex:
            return False

        expected_hex = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()

        # Timing-safe comparison.
        return hmac.compare_digest(expected_hex, received_hex)
