"""auth-shared: shared OIDC/JWT validation and RBAC primitives.

Re-exports the public API of the package so callers can simply do::

    from auth_shared import OIDCValidator, OIDCConfig, InvalidTokenError
    from auth_shared import AuthContext, extract_auth_context
    from auth_shared import Role, requires, check, PermissionDenied
"""

from .oidc import (
    AUTH_ROLES,
    AuthContext,
    AuthRole,
    InvalidTokenError,
    MissingClaimError,
    OIDCConfig,
    OIDCValidator,
    extract_auth_context,
)
from .policy import (
    ROLES,
    MissingActorError,
    PermissionDenied,
    Role,
    check,
    is_allowed,
    requires,
    requires_role,
)

__all__ = [
    # oidc
    "AUTH_ROLES",
    "AuthContext",
    "AuthRole",
    "InvalidTokenError",
    "MissingClaimError",
    "OIDCConfig",
    "OIDCValidator",
    "extract_auth_context",
    # policy
    "ROLES",
    "MissingActorError",
    "PermissionDenied",
    "Role",
    "check",
    "is_allowed",
    "requires",
    "requires_role",
]
