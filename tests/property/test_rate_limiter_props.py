"""Behavioral tests for Rate Limiter middleware.

For any sequence of requests from the same key (IP or user_id) within a
sliding window, the rate limiter SHALL allow the first N requests (where N
is the configured limit) and reject all subsequent requests with HTTP 429
until the window resets. The ``Retry-After`` header value SHALL be a
positive integer representing the seconds until the window resets.

For any request to a path in the exempt set (``/healthz``, ``/readyz``),
the rate limiter SHALL never return HTTP 429 regardless of the request
volume from the same key within any time window.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Ensure the rate_limit module is importable from the service source tree.
_SERVICE_SRC = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "admin-dashboard-api"
    / "src"
)
if str(_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVICE_SRC))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from middleware.rate_limit import RateLimitMiddleware, EXEMPT_PATHS


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Admin API endpoints are limited to 60 requests per minute.
_ADMIN_LIMIT = 60


# ---------------------------------------------------------------------------
# Rate limiter enforcement
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    num_requests=st.integers(min_value=1, max_value=120),
)
def test_rate_limiter_enforcement(num_requests: int) -> None:
    """For any sequence of requests from the same key within a window,
    the first N requests (where N is the configured limit) are allowed
    and all subsequent requests are rejected with HTTP 429. The
    Retry-After header is a positive integer.
    """
    # Create a fresh app for each test case to ensure clean state
    app = FastAPI()

    @app.get("/admin/resource")
    async def admin_resource():
        return {"data": "admin"}

    app.add_middleware(RateLimitMiddleware)

    client = TestClient(app)

    limit = _ADMIN_LIMIT  # Admin endpoints have 60/min limit

    allowed_count = 0
    rejected_count = 0

    for i in range(num_requests):
        response = client.get("/admin/resource")

        if i < limit:
            # First N requests should be allowed
            assert response.status_code == 200, (
                f"Request {i + 1} should be allowed (limit={limit}), "
                f"got status {response.status_code}"
            )
            allowed_count += 1
        else:
            # Subsequent requests should be rejected with 429
            assert response.status_code == 429, (
                f"Request {i + 1} should be rejected (limit={limit}), "
                f"got status {response.status_code}"
            )
            rejected_count += 1

            # Verify response body
            body = response.json()
            assert body["error"] == "rate_limit_exceeded"
            assert isinstance(body["retry_after_seconds"], int)
            assert body["retry_after_seconds"] > 0

            # Verify Retry-After header is a positive integer
            retry_after_header = response.headers.get("Retry-After")
            assert retry_after_header is not None, (
                "Retry-After header must be present on 429 responses"
            )
            retry_after_value = int(retry_after_header)
            assert retry_after_value > 0, (
                f"Retry-After must be a positive integer, got {retry_after_value}"
            )

    # Verify counts are consistent
    if num_requests <= limit:
        assert allowed_count == num_requests
        assert rejected_count == 0
    else:
        assert allowed_count == limit
        assert rejected_count == num_requests - limit


# ---------------------------------------------------------------------------
# Rate limiter path exemption
# ---------------------------------------------------------------------------

# Exempt paths that should never be rate-limited.
_EXEMPT_PATH = st.sampled_from(sorted(EXEMPT_PATHS))

# Number of requests to send in a burst (high volume to stress the exemption).
# We use values well above the default 60/min limit to prove exemption holds.
_EXEMPT_REQUEST_COUNT = st.integers(min_value=61, max_value=200)


def _create_exempt_test_app() -> FastAPI:
    """Create a minimal FastAPI app with rate limiter for exempt path testing."""
    app = FastAPI()

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    app.add_middleware(RateLimitMiddleware)
    return app


@settings(max_examples=100, deadline=None)
@given(
    path=_EXEMPT_PATH,
    num_requests=_EXEMPT_REQUEST_COUNT,
)
def test_rate_limiter_path_exemption(path: str, num_requests: int) -> None:
    """For any request to a path in the exempt set (/healthz, /readyz),
    the rate limiter SHALL never return HTTP 429 regardless of the
    request volume from the same key within any time window.
    """
    app = _create_exempt_test_app()
    client = TestClient(app)

    # Send many requests to the exempt path — none should get 429
    for i in range(num_requests):
        response = client.get(path)
        assert response.status_code != 429, (
            f"Request #{i + 1} to exempt path {path!r} returned 429. "
            f"Exempt paths must never be rate-limited regardless of volume. "
            f"Total requests sent: {num_requests}"
        )
        # The response should be successful (200)
        assert response.status_code == 200, (
            f"Request #{i + 1} to exempt path {path!r} returned "
            f"{response.status_code}, expected 200."
        )
