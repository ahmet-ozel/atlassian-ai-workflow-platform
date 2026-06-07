"""Behavioral tests for security header presence.

*For any* HTTP request to any endpoint on Admin_Dashboard_API,
Assistant_Service, or Automation_Service, the response SHALL contain all
three security headers: ``X-Frame-Options: DENY``,
``X-Content-Type-Options: nosniff``, and
``X-XSS-Protection: 1; mode=block``. No response SHALL omit any of these
headers regardless of status code, content type, or request method.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.testclient import TestClient

from http_shared import SecurityHeadersMiddleware, SECURITY_HEADERS


# ---------------------------------------------------------------------------
# Minimal FastAPI app with SecurityHeadersMiddleware registered
# ---------------------------------------------------------------------------


def _build_app() -> FastAPI:
    """Create a minimal FastAPI app with the security headers middleware.

    The app exposes a catch-all route that returns different status codes
    and content types based on query parameters, allowing Hypothesis to
    exercise the middleware across a wide variety of responses.
    """
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def catch_all(path: str, status_code: int = 200, content_type: str = "json"):
        """Return a response with the requested status code and content type."""
        if content_type == "plain":
            return PlainTextResponse(
                content=f"response for /{path}",
                status_code=status_code,
            )
        return JSONResponse(
            content={"path": path, "status": status_code},
            status_code=status_code,
        )

    return app


_APP = _build_app()
_CLIENT = TestClient(_APP)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# HTTP methods the middleware must handle.
_HTTP_METHODS = st.sampled_from(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])

# Arbitrary URL paths - alphanumeric segments separated by slashes.
_PATH_SEGMENTS = st.from_regex(r"[a-z0-9_\-]{1,20}", fullmatch=True)
_PATHS = st.lists(_PATH_SEGMENTS, min_size=1, max_size=4).map("/".join)

# Content types the response may use.
_CONTENT_TYPES = st.sampled_from(["json", "plain"])

# HTTP status codes - covers success, redirect, client error, server error.
_STATUS_CODES = st.sampled_from([200, 201, 204, 301, 400, 401, 403, 404, 422, 429, 500, 502, 503])


# ---------------------------------------------------------------------------
# Security header behavior
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    method=_HTTP_METHODS,
    path=_PATHS,
    content_type=_CONTENT_TYPES,
    status_code=_STATUS_CODES,
)
def test_security_headers_present_in_every_response(
    method: str,
    path: str,
    content_type: str,
    status_code: int,
) -> None:
    """For every combination of HTTP method, path, content type, and status
    code, the response MUST contain all three security headers with their
    expected values.
    """
    url = f"/{path}?status_code={status_code}&content_type={content_type}"
    response = _CLIENT.request(method, url)

    for header_name, expected_value in SECURITY_HEADERS.items():
        actual_value = response.headers.get(header_name)
        assert actual_value is not None, (
            f"Missing security header {header_name!r} in response. "
            f"method={method!r}, path={path!r}, status_code={status_code}, "
            f"content_type={content_type!r}"
        )
        assert actual_value == expected_value, (
            f"Security header {header_name!r} has wrong value: "
            f"expected {expected_value!r}, got {actual_value!r}. "
            f"method={method!r}, path={path!r}, status_code={status_code}, "
            f"content_type={content_type!r}"
        )
