"""OIDC/JWT validation for the admin control plane.

This module replaces the original placeholder with a working validator while keeping
the public surface backward compatible. The class name and the
``validate(token: str) -> dict`` method signature are preserved
verbatim so existing callers and the
``tests/property/test_path_coverage.py`` invariant - continue to hold.

The validator supports two modes selected via :class:`OIDCConfig`:

* ``auth_mode="dev"`` - accepts any non-empty bearer token and returns
  the canned admin claims dict ``{"sub": "dev-admin", "groups":
  ["admin"]}``. Empty tokens raise :class:`InvalidTokenError`. This mode
  is intended exclusively for local development; production traffic must
  never be routed here.
* ``auth_mode="production"`` - performs the full JWKS-backed signature
  check via ``python-jose``. The JWKS document is fetched once and
  cached in-memory for at least five minutes so subsequent validations
  do not pay the network cost. Issuer (``iss``), audience (``aud``) and
  expiration (``exp``) claims are checked against the configured
  :class:`OIDCConfig` values; any failure raises
  :class:`InvalidTokenError`.

This module also provides:

* :func:`OIDCConfig.from_env` - env-driven factory honouring
  ``AUTH_PROVIDER`` (``"oidc"`` => production JWKS verification,
  ``"local"`` => dev bypass) and the ``OIDC_ISSUER_URL`` /
  ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET`` triplet.
* :class:`AuthContext` and :func:`extract_auth_context` - a frozen
  dataclass and pure helper that extracts ``actor_id`` (OIDC ``sub``),
  ``actor_role`` (one of the four RBAC roles) and ``dept_ids`` from
  the decoded JWT claim dict.

The module is intentionally self-contained: it only depends on
``httpx`` (HTTP client used to fetch the JWKS document) and
``python-jose[cryptography]`` (RS256 signature verification). Both are
declared as required runtime dependencies in
``libs/auth-shared/pyproject.toml``.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

import httpx
from jose import jwt as jose_jwt
from jose.exceptions import JWTError


# Minimum JWKS cache TTL - five minutes, per the spec's lower bound.
_MIN_JWKS_CACHE_TTL_SECONDS: int = 300

# HTTP timeout when fetching JWKS; deliberately short so a misbehaving
# IdP cannot stall the request handler indefinitely.
_JWKS_FETCH_TIMEOUT_SECONDS: float = 5.0


#: The four RBAC roles ("viewer", "lead",
#: "admin", "dept_admin"). The audit_logger package tracks the same
#: four plus a synthetic ``"system"`` role for background processes;
#: :class:`AuthContext` only carries roles that can come from a human
#: user's OIDC token, so ``"system"`` is intentionally absent here.
AuthRole = Literal["viewer", "lead", "admin", "dept_admin"]

#: Runtime mirror of :data:`AuthRole` for set-membership checks. Kept
#: in lock-step with :data:`AuthRole` by ``test_auth_context`` so a
#: drift fails the suite immediately.
AUTH_ROLES: frozenset[str] = frozenset({"viewer", "lead", "admin", "dept_admin"})


class InvalidTokenError(Exception):
    """Raised when a bearer token fails any validation step.

    Callers (e.g. the FastAPI ``require_admin`` dependency) translate
    this into ``401 Unauthorized``. The exception message is safe to
    surface as the ``detail`` field - it never embeds the raw token or
    private key material.
    """


class MissingClaimError(InvalidTokenError):
    """Raised by :func:`extract_auth_context` when a required claim is
    absent or malformed.

    Sub-classing :class:`InvalidTokenError` keeps the FastAPI
    ``require_admin`` dependency simple - a single ``except
    InvalidTokenError`` clause covers both signature failures and
    missing-claim cases.
    """


@dataclass(frozen=True)
class OIDCConfig:
    """Configuration for :class:`OIDCValidator`.

    Attributes:
        issuer: Expected ``iss`` claim. The validator rejects any token
            whose ``iss`` does not match this exact string.
        audience: Expected ``aud`` claim. Tokens minted for a different
            audience are rejected.
        jwks_url: Absolute HTTPS URL of the IdP's JWKS document.
        auth_mode: Either ``"dev"`` (development bypass) or
            ``"production"`` (full signature + claim verification).
            Defaults to ``"production"`` so misconfigurations fail
            closed.
        client_id: Optional OIDC client identifier. Stored for callers
            that need to mint authorisation URLs (the dashboard
            redirect flow); the validator itself does not consume it.
        client_secret: Optional OIDC client secret. Same intent as
            ``client_id`` - kept here so a single ``OIDCConfig`` is
            the entire auth-layer wiring.
    """

    issuer: str
    audience: str
    jwks_url: str
    auth_mode: Literal["dev", "production"] = "production"
    client_id: str | None = None
    client_secret: str | None = None

    # ------------------------------------------------------------------
    # Env-driven factory.
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        audience: str | None = None,
    ) -> "OIDCConfig":
        """Build an :class:`OIDCConfig` from environment variables.

        Honours the configured OIDC environment contract:

        * ``AUTH_PROVIDER`` - ``"oidc"`` (default, production JWKS
          verification) or ``"local"`` (dev bypass).
        * ``OIDC_ISSUER_URL`` - issuer URL; the JWKS endpoint is
          derived as ``{issuer}/.well-known/jwks.json`` when
          ``OIDC_JWKS_URL`` is not provided. Required when
          ``AUTH_PROVIDER=oidc``.
        * ``OIDC_CLIENT_ID`` / ``OIDC_CLIENT_SECRET`` - recorded on
          the returned config so a single object carries the full
          auth-layer wiring; required when ``AUTH_PROVIDER=oidc``.
        * ``OIDC_AUDIENCE`` - the expected ``aud`` claim. Falls back
          to ``OIDC_CLIENT_ID`` when omitted (the canonical OIDC
          contract: a token's audience is the relying-party client
          id).
        * ``OIDC_JWKS_URL`` - optional explicit override.

        Args:
            env: Mapping to read from. Defaults to ``os.environ`` so
                tests can inject a hermetic dict without monkey-
                patching the process environment.
            audience: Optional override that takes precedence over
                ``OIDC_AUDIENCE`` / ``OIDC_CLIENT_ID``.

        Returns:
            A frozen :class:`OIDCConfig`. For ``AUTH_PROVIDER=local``
            the ``issuer`` / ``audience`` / ``jwks_url`` fields carry
            harmless placeholder strings so the dataclass remains
            non-optional and downstream code does not need to special-
            case ``None``.

        Raises:
            ValueError: If ``AUTH_PROVIDER`` is an unrecognised value
                or if a required ``OIDC_*`` variable is missing while
                ``AUTH_PROVIDER=oidc``.
        """

        source: Mapping[str, str] = env if env is not None else os.environ
        provider = source.get("AUTH_PROVIDER", "oidc").strip().lower()

        if provider == "local":
            # Dev bypass: ``validate("...") -> {"sub": "dev-admin",
            # "groups": ["admin"]}``. Issuer/audience/jwks_url are
            # placeholders; the dev path never consults them.
            return cls(
                issuer="local-dev",
                audience="local-dev",
                jwks_url="http://local-dev.invalid/.well-known/jwks.json",
                auth_mode="dev",
                client_id=source.get("OIDC_CLIENT_ID") or None,
                client_secret=source.get("OIDC_CLIENT_SECRET") or None,
            )

        if provider != "oidc":
            raise ValueError(
                "AUTH_PROVIDER must be one of {'oidc', 'local'}; "
                f"got {provider!r}"
            )

        issuer = source.get("OIDC_ISSUER_URL", "").strip()
        client_id = source.get("OIDC_CLIENT_ID", "").strip()
        client_secret = source.get("OIDC_CLIENT_SECRET", "").strip()
        if not issuer:
            raise ValueError(
                "AUTH_PROVIDER=oidc requires OIDC_ISSUER_URL to be set"
            )
        if not client_id:
            raise ValueError(
                "AUTH_PROVIDER=oidc requires OIDC_CLIENT_ID to be set"
            )
        if not client_secret:
            raise ValueError(
                "AUTH_PROVIDER=oidc requires OIDC_CLIENT_SECRET to be set"
            )

        jwks_url = source.get("OIDC_JWKS_URL", "").strip()
        if not jwks_url:
            jwks_url = issuer.rstrip("/") + "/.well-known/jwks.json"

        resolved_audience = (
            audience
            if audience is not None
            else source.get("OIDC_AUDIENCE", "").strip() or client_id
        )

        return cls(
            issuer=issuer,
            audience=resolved_audience,
            jwks_url=jwks_url,
            auth_mode="production",
            client_id=client_id,
            client_secret=client_secret,
        )


# ---------------------------------------------------------------------------
# AuthContext - claim extraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthContext:
    """Per-request actor context derived from an OIDC token.

    Carried on the request scope by FastAPI dependencies so audit
    writes (``audit_logger.AuditEvent.actor_id`` / ``actor_role``) and
    Postgres RLS bindings (``db_shared.with_dept_session``) read from
    a single source of truth. The fields mirror the auth context
    pseudocode in §`AdminProxy`:

    .. code-block:: python

        actor: AuthContext   # actor_id, role, dept_ids

    Attributes:
        actor_id: The OIDC ``sub`` claim - a stable, opaque user id
            minted by the IdP. Used as the ``actor_id`` column of
            ``audit_events``.
        actor_role: One of the four RBAC roles. Anything else is rejected by
            :func:`extract_auth_context`.
        dept_ids: Frozen set of department ids the user is a member
            of. Empty for ``admin`` accounts (which see every
            department by virtue of the role) and for users that
            have not yet been provisioned to any department.
        raw_claims: The decoded JWT claim dict, kept for downstream
            consumers that need access to additional claims (eg.
            ``email`` for audit annotations). Stored as a frozen
            ``Mapping`` so the dataclass remains hashable through
            ``frozen=True``.
    """

    actor_id: str
    actor_role: AuthRole
    dept_ids: frozenset[str]
    raw_claims: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    def is_admin(self) -> bool:
        """``True`` if the actor holds the global ``admin`` role."""

        return self.actor_role == "admin"

    def can_access_dept(self, dept_id: str) -> bool:
        """Return whether the actor may access ``dept_id``.

        ``admin`` actors may always access; every other role is
        restricted to ``dept_id in self.dept_ids``. ``viewer`` /
        ``lead`` / ``dept_admin`` are all dept-scoped - global
        actions are an admin-only concern enforced
        separately by :func:`auth_shared.policy.requires`.
        """

        if not dept_id:
            # Empty dept_id is meaningless; treat as "no access" rather
            # than silently allowing.
            return False
        if self.actor_role == "admin":
            return True
        return dept_id in self.dept_ids


def extract_auth_context(claims: Mapping[str, Any]) -> AuthContext:
    """Extract an :class:`AuthContext` from a decoded JWT claim dict.

    Implements the claim-mapping rules for user role and department ids:

    * ``sub`` -> ``actor_id`` (required, non-empty string).
    * ``role`` (preferred) or first matching entry of ``roles`` /
      ``groups`` -> ``actor_role``. Only the four roles defined in
      :data:`AUTH_ROLES` are accepted; anything else raises
      :class:`MissingClaimError`. ``roles`` / ``groups`` may be a
      space-separated string or a list of strings - both forms occur
      in the wild (Auth0 vs Keycloak).
    * ``dept_ids`` (preferred) or ``departments`` -> ``dept_ids``
      frozen-set. Same string-or-list flexibility. Missing or empty
      is allowed (yields an empty set).

    Args:
        claims: The dict returned by :meth:`OIDCValidator.validate`.

    Returns:
        A frozen :class:`AuthContext` carrying the extracted fields.

    Raises:
        MissingClaimError: If ``sub`` is missing or empty, or if no
            valid role can be resolved from ``role`` / ``roles`` /
            ``groups``.
    """

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise MissingClaimError("token missing required 'sub' claim")
    actor_id = sub.strip()

    role = _extract_role(claims)
    dept_ids = _extract_dept_ids(claims)

    return AuthContext(
        actor_id=actor_id,
        actor_role=role,
        dept_ids=dept_ids,
        raw_claims=dict(claims),
    )


def _extract_role(claims: Mapping[str, Any]) -> AuthRole:
    """Return the first valid RBAC role found on ``claims``.

    Lookup order:
      1. ``role`` - the canonical single-valued claim shape.
      2. ``roles`` - list or space-separated string.
      3. ``groups`` - list or space-separated string (Keycloak's
         default group claim).

    Strings are stripped and lower-cased before comparison so
    ``"Admin"`` from a sloppy IdP is still recognised. The first
    candidate matching :data:`AUTH_ROLES` wins; if none matches a
    :class:`MissingClaimError` is raised.
    """

    candidates: list[str] = []

    role = claims.get("role")
    if isinstance(role, str) and role.strip():
        candidates.extend(_normalise_string_list(role))

    for key in ("roles", "groups"):
        value = claims.get(key)
        candidates.extend(_normalise_string_list(value))

    for candidate in candidates:
        normalised = candidate.strip().lower()
        if normalised in AUTH_ROLES:
            # ``Literal`` types are erased at runtime; the ``cast`` is
            # implicit because :data:`AUTH_ROLES` only contains values
            # that match :data:`AuthRole`.
            return normalised  # type: ignore[return-value]

    raise MissingClaimError(
        "token does not carry a known RBAC role; expected one of "
        f"{sorted(AUTH_ROLES)} on claim 'role', 'roles' or 'groups'"
    )


def _extract_dept_ids(claims: Mapping[str, Any]) -> frozenset[str]:
    """Return the frozen set of department ids on ``claims``.

    Lookup order:
      1. ``dept_ids`` - canonical claim name.
      2. ``departments`` - alternative used by Streamlit.

    Empty / missing claims yield an empty set; this is a legal state
    for ``admin`` users who do not need explicit dept membership.
    """

    for key in ("dept_ids", "departments"):
        value = claims.get(key)
        items = _normalise_string_list(value)
        if items:
            return frozenset(item.strip() for item in items if item.strip())
    return frozenset()


def _normalise_string_list(value: Any) -> list[str]:
    """Coerce a list-or-space-separated-string claim to a ``list[str]``.

    Returns an empty list for ``None``, non-string scalars, and lists
    whose entries are not strings. Real-world IdPs are inconsistent
    about whether multi-valued claims are arrays or whitespace-
    separated strings, so this helper accepts both.
    """

    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in value.split() if part]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return [item for item in value if isinstance(item, str) and item]
    return []


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------


@dataclass
class _JWKSCache:
    """In-memory JWKS cache with a monotonic TTL clock.

    Stored as a mutable dataclass so the validator can refresh it under
    a lock without rebuilding the surrounding :class:`OIDCValidator`.
    """

    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: float = 0.0  # monotonic seconds; ``0.0`` => uncached

    def is_fresh(self, now: float, ttl_seconds: int) -> bool:
        return self.fetched_at != 0.0 and (now - self.fetched_at) < ttl_seconds


class OIDCValidator:
    """Validate OIDC-issued bearer tokens for the admin control plane.

    The class name and the :meth:`validate` signature are preserved
    from the original placeholder. Construction
    requires an :class:`OIDCConfig`; existing callers that previously
    instantiated ``OIDCValidator()`` without arguments are not in the
    codebase yet, so the wider signature change is backward compatible.
    """

    def __init__(
        self,
        config: OIDCConfig,
        *,
        jwks_cache_ttl_seconds: int = _MIN_JWKS_CACHE_TTL_SECONDS,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Construct a validator.

        Args:
            config: Validator configuration (see :class:`OIDCConfig`).
            jwks_cache_ttl_seconds: Override the JWKS cache TTL. The
                value is clamped to at least
                ``_MIN_JWKS_CACHE_TTL_SECONDS`` (5 minutes) so the spec
                guarantee cannot be relaxed by accident.
            http_client: Optional injected ``httpx.Client``. When
                ``None`` (default), a private client is created lazily.
                Tests pass a mock client so the validator never hits
                the network.
        """

        self._config = config
        self._jwks_cache_ttl_seconds = max(
            jwks_cache_ttl_seconds, _MIN_JWKS_CACHE_TTL_SECONDS
        )
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._cache = _JWKSCache()
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API - preserves the placeholder signature.
    # ------------------------------------------------------------------

    @property
    def config(self) -> OIDCConfig:
        """Read-only access to the :class:`OIDCConfig` in use."""

        return self._config

    def validate(self, token: str) -> dict:
        """Validate ``token`` and return the decoded claims dict.

        Args:
            token: Compact-serialised JWT string presented by the
                caller (the value of the ``Authorization: Bearer ...``
                header, with the ``Bearer`` prefix already stripped).

        Returns:
            The decoded claims dictionary on success.

        Raises:
            InvalidTokenError: If the token is empty, fails signature
                verification, has an invalid ``iss`` / ``aud`` /
                ``exp`` claim, or - in dev mode - is empty.
        """

        if self._config.auth_mode == "dev":
            return self._validate_dev(token)
        return self._validate_production(token)

    def authenticate(self, token: str) -> AuthContext:
        """Validate ``token`` and return a populated :class:`AuthContext`.

        Convenience wrapper combining :meth:`validate` with
        :func:`extract_auth_context`. FastAPI dependencies in
        ``admin-dashboard-api`` use this entry point so the validator
        owns the full token-to-actor pipeline.

        Raises :class:`InvalidTokenError` for signature / claim
        failures and :class:`MissingClaimError` (a subclass) when the
        decoded token lacks ``sub`` or a recognised role.
        """

        claims = self.validate(token)
        return extract_auth_context(claims)

    # ------------------------------------------------------------------
    # Mode-specific helpers.
    # ------------------------------------------------------------------

    def _validate_dev(self, token: str) -> dict:
        """Dev-mode bypass: accept any non-empty bearer token.

        Returns the canned admin claims dict so downstream
        ``require_admin`` checks pass during local development.
        Production never routes here.
        """

        if not token:
            raise InvalidTokenError("empty token")
        return {"sub": "dev-admin", "role": "admin", "groups": ["admin"]}

    def _validate_production(self, token: str) -> dict:
        """Full JWKS-backed signature + claim verification."""

        if not token:
            raise InvalidTokenError("empty token")

        try:
            unverified_header = jose_jwt.get_unverified_header(token)
        except JWTError as exc:
            raise InvalidTokenError(f"invalid token header: {exc}") from exc

        kid = unverified_header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError("token header missing 'kid'")

        alg = unverified_header.get("alg")
        if alg != "RS256":
            # The spec pins RS256; any other algorithm (including
            # ``"none"``) is rejected outright to avoid algorithm
            # confusion attacks.
            raise InvalidTokenError(f"unsupported alg '{alg}'")

        jwk = self._get_signing_key(kid)
        if jwk is None:
            raise InvalidTokenError(f"unknown signing key '{kid}'")

        try:
            claims = jose_jwt.decode(
                token,
                jwk,
                algorithms=["RS256"],
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={
                    "require_exp": True,
                    "require_iat": False,
                    "require_nbf": False,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "verify_signature": True,
                },
            )
        except JWTError as exc:
            raise InvalidTokenError(f"invalid token: {exc}") from exc

        # ``jose_jwt.decode`` already enforced ``aud``/``iss``/``exp``
        # but we re-assert ``iss`` == config.issuer defensively to keep
        # the contract obvious to future readers.
        if claims.get("iss") != self._config.issuer:
            raise InvalidTokenError("issuer mismatch")
        return dict(claims)

    # ------------------------------------------------------------------
    # JWKS cache.
    # ------------------------------------------------------------------

    def _get_signing_key(self, kid: str) -> dict[str, Any] | None:
        """Return the JWK for ``kid``, refreshing the cache if needed.

        The cache TTL is enforced before any network I/O happens. When
        the cache is stale or the requested ``kid`` is missing, the
        JWKS document is re-fetched once; if the key is still missing
        afterwards the function returns ``None`` and the caller raises
        :class:`InvalidTokenError`.
        """

        now = time.monotonic()
        with self._cache_lock:
            if self._cache.is_fresh(now, self._jwks_cache_ttl_seconds):
                key = self._cache.keys.get(kid)
                if key is not None:
                    return key

            # Either the cache is stale or the ``kid`` rotated in.
            keys = self._fetch_jwks()
            self._cache = _JWKSCache(keys=keys, fetched_at=time.monotonic())
            return self._cache.keys.get(kid)

    def _fetch_jwks(self) -> dict[str, dict[str, Any]]:
        """Fetch the JWKS document and index keys by ``kid``."""

        client = self._http_client or httpx.Client(
            timeout=_JWKS_FETCH_TIMEOUT_SECONDS
        )
        try:
            response = client.get(self._config.jwks_url)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise InvalidTokenError(f"jwks fetch failed: {exc}") from exc
        except ValueError as exc:  # JSON decode error
            raise InvalidTokenError(f"jwks parse failed: {exc}") from exc
        finally:
            if self._owns_http_client and client is not self._http_client:
                client.close()

        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list):
            raise InvalidTokenError("jwks document has no 'keys' array")

        indexed: dict[str, dict[str, Any]] = {}
        for entry in raw_keys:
            if not isinstance(entry, dict):
                continue
            kid = entry.get("kid")
            if isinstance(kid, str) and kid:
                indexed[kid] = entry
        return indexed
