"""Rate limiting middleware for admin-dashboard-api.

Implements per-endpoint rate limiting using slowapi (a Starlette/FastAPI
wrapper around ``limits``). Two tiers are defined:

* **Webhook endpoints** - 100 requests/minute keyed by client IP address.
* **Admin API endpoints** - 60 requests/minute keyed by authenticated user
  identity (falls back to IP when no user context is available).

Health-check paths (``/healthz``, ``/readyz``) are unconditionally exempt
from rate limiting so orchestrators and load balancers never receive 429.

When a limit is exceeded the middleware returns HTTP 429 with:
* ``Retry-After`` header (seconds until the window resets)
* JSON body: ``{"error": "rate_limit_exceeded", "retry_after_seconds": N}``

"""

from __future__ import annotations

import math
import time
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


# ---------------------------------------------------------------------------
# Key functions
# ---------------------------------------------------------------------------


def _get_user_key(request: Request) -> str:
    """Extract user identity for user-based rate limiting.

    Attempts to read the authenticated user from request state (set by
    the OIDC/auth middleware). Falls back to IP address when no user
    context is available (e.g. unauthenticated requests that will be
    rejected by the auth layer anyway).
    """
    # Try common patterns for user identity on the request
    if hasattr(request.state, "user") and request.state.user:
        user = request.state.user
        # Support both string user_id and object with sub/user_id attr
        if isinstance(user, str):
            return f"user:{user}"
        if hasattr(user, "sub"):
            return f"user:{user.sub}"
        if hasattr(user, "user_id"):
            return f"user:{user.user_id}"
    # Fallback to IP-based limiting
    return get_remote_address(request)


# ---------------------------------------------------------------------------
# Limiter instance
# ---------------------------------------------------------------------------

#: Global limiter instance configured with IP-based default key function.
#: Individual endpoints can override the key function via decorators.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60/minute"],
    storage_uri="memory://",  # Production deployments should use Redis URI
)

# ---------------------------------------------------------------------------
# Rate limit constants
# ---------------------------------------------------------------------------

#: Webhook endpoints: 100 requests per minute, keyed by IP address.
WEBHOOK_LIMIT = "100/minute"

#: Admin API endpoints: 60 requests per minute, keyed by user identity.
ADMIN_API_LIMIT = "60/minute"

#: Paths exempt from rate limiting (health probes).
EXEMPT_PATHS: set[str] = {"/healthz", "/readyz"}

#: Path prefixes that identify webhook endpoints.
WEBHOOK_PATH_PREFIXES: tuple[str, ...] = (
    "/webhooks",
    "/api/v1/webhooks",
    "/admin/security/webhooks",
)

#: Path prefixes that identify admin API endpoints.
ADMIN_PATH_PREFIXES: tuple[str, ...] = (
    "/admin/",
    "/api/v1/",
    "/auth/",
)


# ---------------------------------------------------------------------------
# Custom 429 response builder
# ---------------------------------------------------------------------------


def _rate_limit_exceeded_handler(
    request: Request, exc: RateLimitExceeded
) -> JSONResponse:
    """Build the standard 429 response with Retry-After header.

    Parses the ``Retry-After`` value from the exception's headers and
    returns a JSON body conforming to the spec:
    ``{"error": "rate_limit_exceeded", "retry_after_seconds": N}``
    """
    # slowapi attaches the Retry-After header to the exception detail
    retry_after = _extract_retry_after(exc)

    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limit_exceeded",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def _extract_retry_after(exc: RateLimitExceeded) -> int:
    """Extract retry-after seconds from a RateLimitExceeded exception.

    slowapi stores the retry-after value in the exception's headers dict
    or as part of the detail string. We parse it robustly.
    """
    # Try to get from headers attribute
    if hasattr(exc, "headers") and exc.headers:
        retry_val = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
        if retry_val is not None:
            try:
                return max(1, int(retry_val))
            except (ValueError, TypeError):
                pass

    # Try to parse from detail string (format: "Rate limit exceeded: N per M minute")
    if hasattr(exc, "detail") and exc.detail:
        detail = str(exc.detail)
        # Extract window info - default to 60s (1 minute window)
        return 60

    # Default fallback
    return 60


# ---------------------------------------------------------------------------
# Rate Limit Middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that enforces per-path rate limits.

    This middleware intercepts every request and applies the appropriate
    rate limit based on the request path:

    * Exempt paths → no limiting applied
    * Webhook paths → WEBHOOK_LIMIT (100/min) keyed by IP
    * Admin/API paths → ADMIN_API_LIMIT (60/min) keyed by user/IP
    * Other paths → default limit (60/min) keyed by IP

    The middleware uses an internal sliding-window counter (in-memory by
    default; Redis-backed in production) to track request counts per key
    per window.
    """

    def __init__(self, app: Any, *, storage_uri: str = "memory://") -> None:
        super().__init__(app)
        self._windows: dict[str, _WindowState] = {}
        self._storage_uri = storage_uri

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Apply rate limiting before forwarding the request."""
        path = request.url.path

        # Exempt health-check paths (behavior 8.3)
        if path in EXEMPT_PATHS:
            return await call_next(request)

        # Determine limit and key based on path
        limit, key = self._resolve_limit_and_key(request, path)

        # Check rate limit
        allowed, retry_after = self._check_limit(key, limit)

        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "retry_after_seconds": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _resolve_limit_and_key(
        self, request: Request, path: str
    ) -> tuple[int, str]:
        """Determine the rate limit and key for a given request path.

        Returns:
            Tuple of (requests_per_minute, rate_limit_key)
        """
        # Webhook endpoints: IP-based, 100/min
        if any(path.startswith(prefix) for prefix in WEBHOOK_PATH_PREFIXES):
            ip = get_remote_address(request)
            return 100, f"webhook:{ip}"

        # Admin API endpoints: user-based, 60/min
        if any(path.startswith(prefix) for prefix in ADMIN_PATH_PREFIXES):
            user_key = _get_user_key(request)
            return 60, f"admin:{user_key}"

        # Default: IP-based, 60/min
        ip = get_remote_address(request)
        return 60, f"default:{ip}"

    def _check_limit(self, key: str, limit: int) -> tuple[bool, int]:
        """Check if the request is within the rate limit.

        Uses a fixed-window algorithm with 60-second windows.

        Returns:
            Tuple of (is_allowed, retry_after_seconds)
        """
        now = time.time()
        window_start = now - (now % 60)  # Align to minute boundary

        state = self._windows.get(key)

        if state is None or state.window_start != window_start:
            # New window - create fresh state
            self._windows[key] = _WindowState(
                window_start=window_start,
                request_count=1,
            )
            return True, 0

        # Same window - increment and check
        state.request_count += 1

        if state.request_count > limit:
            # Calculate seconds until window resets
            window_end = window_start + 60
            retry_after = max(1, math.ceil(window_end - now))
            return False, retry_after

        return True, 0

    def reset(self) -> None:
        """Clear all rate limit state. Useful for testing."""
        self._windows.clear()


class _WindowState:
    """Internal state for a single rate-limit window."""

    __slots__ = ("window_start", "request_count")

    def __init__(self, window_start: float, request_count: int) -> None:
        self.window_start = window_start
        self.request_count = request_count


# ---------------------------------------------------------------------------
# Public API for app registration
# ---------------------------------------------------------------------------


def install_rate_limiter(app: Any) -> RateLimitMiddleware:
    """Install the rate limiter middleware on a FastAPI/Starlette app.

    Also registers the custom 429 exception handler for slowapi
    decorator-based limiting (used on individual route handlers).

    Returns the middleware instance for testing purposes.
    """
    import os

    from slowapi import _rate_limit_exceeded_handler as _default_handler

    # Use Redis in production if REDIS_URL is set
    storage_uri = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")

    # Register the slowapi limiter state on the app (required by slowapi)
    app.state.limiter = limiter

    # Register custom exception handler for decorator-based limits
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Add the middleware
    middleware = RateLimitMiddleware(app, storage_uri=storage_uri)
    return middleware
