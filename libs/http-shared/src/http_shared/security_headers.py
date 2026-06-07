"""Security headers middleware for FastAPI/Starlette applications.

Adds standard security headers to every HTTP response to mitigate
XSS, clickjacking, and MIME-sniffing attacks.

Usage::

    from http_shared.security_headers import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "X-XSS-Protection": "1; mode=block",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Her HTTP yanıtına güvenlik başlıkları ekler.

    Adds the following headers to every response regardless of status code,
    content type, or request method:

    - ``X-Frame-Options: DENY`` - prevents clickjacking by disallowing framing
    - ``X-Content-Type-Options: nosniff`` - prevents MIME-type sniffing
    - ``X-XSS-Protection: 1; mode=block`` - enables browser XSS filter
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:  # noqa: ANN001
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response
