"""FastAPI dependency that enforces admin-only OIDC authentication.

This module wires the ``libs/auth-shared`` :class:`OIDCValidator` into
the admin-dashboard-api request pipeline. Every ``/admin/services``
endpoint declares ``Depends(require_admin)`` so the lifecycle REST
surface is uniformly gated (Requirement 10.1).

Behaviour matrix (Requirements 10.2 / 10.3):

* Missing or non-``Bearer`` ``Authorization`` header → ``401`` immediately,
  *before* any claim inspection, so anonymous probes never leak whether
  a service exists.
* ``Bearer`` prefix present but the token portion is empty → ``401``.
* ``OIDCValidator.validate`` raises :class:`InvalidTokenError` → ``401``.
* Validator returns claims, but neither ``groups`` nor ``roles`` contains
  ``"admin"`` → ``403``. Read-only access is *not* granted to authenticated
  non-admin users (Requirement 10.3 second sentence).
* All checks pass → :class:`AuthClaims` is returned and bound to the
  endpoint handler argument.

The validator is constructed once per process via :func:`get_validator`,
which reads :class:`src.config.Settings` and memoises the result with
:func:`functools.lru_cache`. Tests can override the dependency with
``app.dependency_overrides[get_validator]`` to inject stub validators.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status

from auth_shared import InvalidTokenError, OIDCConfig, OIDCValidator

from ..config import Settings


@dataclass(frozen=True)
class AuthClaims:
    """Subject + group claims surfaced to admin endpoints.

    Attributes:
        sub: OIDC ``sub`` claim — used as the ``actor`` field on every
            audit log entry written by lifecycle handlers (Requirement
            11.2).
        groups: Union of the validated token's ``groups`` and ``roles``
            claims, frozen as a tuple so the value is hashable and can be
            stored in immutable dataclasses without copying.
    """

    sub: str
    groups: tuple[str, ...]


@lru_cache(maxsize=1)
def _build_validator(
    issuer: str,
    audience: str,
    jwks_url: str,
    auth_mode: str,
) -> OIDCValidator:
    """Construct an :class:`OIDCValidator` from primitive setting values.

    The arguments are split out (instead of taking a :class:`Settings`
    instance directly) so :func:`functools.lru_cache` can hash them.
    Settings instances are unhashable Pydantic models.
    """

    if auth_mode not in ("dev", "production"):
        # Defensive guard — Settings already constrains this via a
        # ``Literal`` type, but if a future caller bypasses Settings
        # we still want a clear failure mode rather than a silent
        # fall-through to ``production`` validation.
        raise ValueError(f"unsupported AUTH_MODE: {auth_mode!r}")

    config = OIDCConfig(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        auth_mode=auth_mode,  # type: ignore[arg-type]
    )
    return OIDCValidator(config)


def get_validator() -> OIDCValidator:
    """Return the process-wide :class:`OIDCValidator` singleton.

    Reads :class:`Settings` on first invocation and caches the result;
    subsequent calls return the same validator instance so the JWKS
    cache inside :class:`OIDCValidator` is shared across requests.
    """

    settings = Settings()
    return _build_validator(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        auth_mode=settings.auth_mode,
    )


# ``Bearer`` is the only auth-scheme accepted by the admin endpoints.
# Stored as a constant so the case-insensitive comparison and prefix
# slice agree on the same string.
_BEARER_SCHEME = "bearer"
_BEARER_PREFIX_LEN = len(_BEARER_SCHEME) + 1  # ``"bearer "`` (7 chars)


async def require_admin(
    request: Request,
    validator: OIDCValidator = Depends(get_validator),
) -> AuthClaims:
    """Validate the request's bearer token and assert admin membership.

    Raises:
        HTTPException(401): When the ``Authorization`` header is missing,
            does not start with ``Bearer ``, contains an empty token, or
            the token fails OIDC validation.
        HTTPException(403): When the token is valid but neither the
            ``groups`` nor the ``roles`` claim contains ``"admin"``.

    Returns:
        AuthClaims: Subject + group claims for the authenticated admin.
    """

    auth_header = request.headers.get("authorization", "")

    # Per Requirement 10.2 we MUST raise 401 before any claim inspection
    # when the header is absent or the scheme is wrong. The
    # case-insensitive match below treats ``Bearer``, ``bearer`` and
    # ``BEARER`` identically (RFC 7235 §2.1 — schemes are case-insensitive).
    if not auth_header.lower().startswith(_BEARER_SCHEME + " "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
        )

    token = auth_header[_BEARER_PREFIX_LEN:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="empty bearer token",
        )

    try:
        claims = validator.validate(token)
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    # Union of ``groups`` and ``roles`` so an IdP that surfaces RBAC under
    # either name still lets an admin through. Non-iterable / non-list
    # claim values are coerced to an empty set rather than raising, so a
    # malformed token without group claims falls through to the 403
    # branch with a clear ``admin claim required`` message.
    groups = _collect_admin_membership(claims)
    if "admin" not in groups:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin claim required",
        )

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        # OIDC spec mandates ``sub``; if it's missing the token is
        # malformed even though the signature checked out. Treat as 401
        # rather than 403 because we cannot reliably identify the actor
        # (Requirement 11.2 needs a stable ``sub`` for audit entries).
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    return AuthClaims(sub=sub, groups=tuple(sorted(groups)))


def _collect_admin_membership(claims: dict) -> set[str]:
    """Return the union of ``groups`` and ``roles`` from ``claims``.

    Either claim may be absent or hold a non-iterable value (e.g. a
    string, ``None``, an integer); both cases collapse to ``set()`` so
    the caller's ``"admin" in groups`` check remains well-defined.
    """

    collected: set[str] = set()
    for claim_name in ("groups", "roles"):
        value = claims.get(claim_name)
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                if isinstance(item, str):
                    collected.add(item)
    return collected
