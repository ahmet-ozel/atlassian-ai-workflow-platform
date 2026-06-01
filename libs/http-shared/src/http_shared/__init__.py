"""http-shared: shared HTTP client factory for MCP / Firecrawl calls.

Re-exports the public API of the package so callers can simply do::

    from http_shared import make_mcp_client, KNOWN_CLIENT_SOURCES
    from http_shared import with_atlassian_creds, CredentialResolutionError
    from http_shared import RedactionFilter, install_redaction_filter
    from http_shared import SecurityHeadersMiddleware, SECURITY_HEADERS
"""

from .auth_inject import CredentialResolutionError, ServiceLiteral, with_atlassian_creds
from .client import KNOWN_CLIENT_SOURCES, make_mcp_client
from .redaction import (
    REDACTION_PATTERNS,
    REDACTION_PLACEHOLDER,
    RedactionFilter,
    install_redaction_filter,
    redact_text,
)
from .security_headers import SECURITY_HEADERS, SecurityHeadersMiddleware

__all__ = [
    "CredentialResolutionError",
    "KNOWN_CLIENT_SOURCES",
    "REDACTION_PATTERNS",
    "REDACTION_PLACEHOLDER",
    "RedactionFilter",
    "SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
    "ServiceLiteral",
    "install_redaction_filter",
    "make_mcp_client",
    "redact_text",
    "with_atlassian_creds",
]
