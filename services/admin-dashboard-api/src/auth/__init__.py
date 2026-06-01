"""Admin API authentication helpers.

This package exposes the FastAPI dependency that every ``/admin/services``
endpoint must declare in order to enforce OIDC bearer-token authentication
(Requirement 10). The actual token validation is delegated to
``libs/auth-shared`` so the wire-protocol logic is shared with future
HTTP services and Temporal workers.

Public API:

* :class:`AuthClaims` — frozen claims tuple returned by ``require_admin``.
* :func:`get_validator` — cached :class:`auth_shared.OIDCValidator` factory.
* :func:`require_admin` — FastAPI dependency that fails the request with
  ``401 Unauthorized`` (missing / invalid token) or ``403 Forbidden``
  (valid token without the ``admin`` claim).
"""

from .dependencies import AuthClaims, get_validator, require_admin

__all__ = ["AuthClaims", "get_validator", "require_admin"]
