"""Vault-backed credential resolver for department bot credentials.

Reads ``automation.department_bots`` from Postgres to discover which
Vault path (``credential_ref``) holds the PAT for a given department ×
service pair (``scope="org"``), then fetches the actual secret from
Vault KV-v2.

For ``scope="user"`` the resolver bypasses Postgres entirely and reads
the per-user / per-session path
``secret/atlassian/_user_session/{session_id}/{service}`` directly.
The two scopes are **strictly isolated** - see
:class:`CredentialScopeViolationError` and the property test
``tests/property/test_credential_scope_isolation.py``.

The resolved credential is returned as an :class:`AtlassianCredential`
dataclass suitable for injection via
:func:`http_shared.auth_inject.with_atlassian_creds`.

TTL cache + drift detection
---------------------------

Every successful ``get(...)`` populates an in-memory cache keyed by
``(scope, dept_id_or_session_id, service)``. Subsequent calls within
:data:`_CACHE_TTL` (300 seconds) return the cached
:class:`AtlassianCredential` without touching Vault or Postgres. Once
the TTL expires the resolver fetches fresh material via
``vault.read_with_metadata(path)``; if the fresh
``data.metadata.created_time`` is strictly greater than the cached
value, the resolver writes a ``vault_credential_refreshed`` audit row
so operators can correlate worker-side rotation pickup with the
admin-dashboard *Security* drift banner.

The cache is process-local; a worker restart drops it, which is by
design - admin-triggered restarts are the operator escape hatch when
the 300 s window is too long.

"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Final, Literal

import asyncpg

from http_shared.auth_inject import CredentialResolutionError


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptBotRow:
    """A row from ``automation.department_bots``."""

    id: int
    department_id: str
    service: str
    credential_ref: str
    account_id: str | None
    username: str | None
    deployment: str


@dataclass(frozen=True, slots=True)
class AtlassianCredential:
    """Resolved credential triple for an Atlassian service."""

    url: str
    username: str
    personal_token: str


@dataclass(frozen=True, slots=True)
class CachedEntry:
    """A single in-memory cache slot for a resolved credential.

    Stores the resolved :class:`AtlassianCredential` together with the
    wall-clock timestamp of insertion (``cached_at``) and the Vault
    KV-v2 ``data.metadata.created_time`` value at the moment the
    secret was fetched. The latter is what drives drift detection:
    when the TTL expires and the resolver
    re-reads Vault, a fresh ``created_time`` strictly greater than
    ``vault_created_time`` indicates an out-of-band rotation and
    triggers the ``vault_credential_refreshed`` audit emission.

    Attributes
    ----------
    credential:
        The resolved credential triple. Cached by reference; the
        dataclass is ``frozen=True`` so accidental mutation is
        impossible.
    cached_at:
        Timezone-aware UTC timestamp recorded at the moment this
        entry was created or refreshed. The TTL window is
        ``cached_at + _CACHE_TTL``.
    vault_created_time:
        Vault KV-v2 ``data.metadata.created_time`` for the secret
        version this entry caches. ``None`` when the underlying
        Vault client did not surface metadata (legacy ``read_secret``
        path); a missing value disables drift detection for the
        affected key but does not break TTL caching.
    """

    credential: AtlassianCredential
    cached_at: datetime
    vault_created_time: str | None


# ---------------------------------------------------------------------------
# Cache configuration
# ---------------------------------------------------------------------------

#: Default cache TTL - 300 seconds (5 minutes). Mirrors the Vault
#: rotation banner cadence in admin-dashboard *Security* sub-page so
#: a worker picks up rotated material within one TTL window without
#: a process restart.
_CACHE_TTL: Final[timedelta] = timedelta(seconds=300)


#: Type alias for the cache key. The first element is the canonical
#: scope (``"org"`` or ``"user"`` - the legacy ``"bot"`` alias is
#: normalised before key construction). The second element is the
#: ``dept_id`` for org scope and the ``session_id`` for user scope -
#: keeping a single tuple shape lets one cache serve both scopes
#: without leaking either side's identity into the other slot
#: (the path-isolation invariant carries over to the cache
#: layer).
CacheKey = tuple[str, str, str]


# ---------------------------------------------------------------------------
# Scope literals & path-prefix sentinels
# ---------------------------------------------------------------------------

#: The resolver accepts ``"org"`` (worker bot) and ``"user"`` (Streamlit
#: per-session) scopes after deprecating ``"bot"``. The
#: legacy ``"bot"`` literal is still accepted as a silent alias for
#: ``"org"`` so callers that have not yet migrated continue to work; the
#: deprecation warning is emitted by
#: :func:`http_shared.auth_inject.with_atlassian_creds`, which is the
#: only public-API entry point for credential injection.
ScopeLiteral = Literal["org", "user"]

#: Path infix that uniquely identifies a per-user / per-session Vault
#: secret. Both ``secret/atlassian/_user_session/...`` and
#: ``secret/atlassian/_user_persisted/...`` (Z7 PIN-encrypted opt-in
#: persistence) start with ``atlassian/_user`` so a single substring
#: check covers both flavours.
_USER_PATH_INFIX: Final[str] = "atlassian/_user"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CredentialScopeViolationError(RuntimeError):
    """Raised when a Vault read crosses the org  user scope boundary.

    A cross-scope read attempt is treated as a critical security event
    (uyumluluk Q7). The resolver:

    1. Writes a ``credential_scope_violation_attempt`` audit row with
       ``actor_role="system"`` and ``result="denied"``.
    2. Raises this exception so the calling workflow / handler aborts
       before any secret material is loaded into memory.

    Attributes
    ----------
    scope:
        The requested scope (``"org"`` or ``"user"``).
    attempted_path:
        The Vault path the resolver was about to read, which violated
        the requested scope's prefix invariant.
    dept_id:
        Department identifier for which resolution was requested
        (``"_unknown"`` if not applicable in the user-scope case).
    service:
        Atlassian service name (``"jira"``, ``"bitbucket"``,
        ``"confluence"``).
    """

    def __init__(
        self,
        *,
        scope: str,
        attempted_path: str,
        dept_id: str,
        service: str,
    ) -> None:
        self.scope = scope
        self.attempted_path = attempted_path
        self.dept_id = dept_id
        self.service = service
        super().__init__(
            f"credential_scope_violation: scope={scope!r} attempted to "
            f"read path={attempted_path!r} (dept={dept_id!r} "
            f"service={service!r})"
        )


# ---------------------------------------------------------------------------
# VaultClient protocol (duck-typed)
# ---------------------------------------------------------------------------


class VaultClient:
    """Protocol-like base for Vault KV-v2 reads.

    Real implementations wrap httpx calls to the Vault HTTP API.
    Tests inject a fake that returns in-memory dicts.
    """

    async def read_secret(self, path: str) -> dict[str, str] | None:
        """Read a KV-v2 secret at *path*.

        Returns the ``data.data`` dict on success, or ``None`` when the
        path does not exist (404).
        """
        raise NotImplementedError  # pragma: no cover

    async def read_with_metadata(
        self, path: str
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        """Read a KV-v2 secret along with its metadata.

        Returns a ``(data, metadata)`` tuple where ``data`` matches
        the :meth:`read_secret` payload and ``metadata`` mirrors
        Vault's KV-v2 ``data.metadata`` block (most importantly the
        ``created_time`` ISO-8601 string). ``None`` is returned when
        the path does not exist (404), matching :meth:`read_secret`.

        This method exists for drift detection. Implementations that only need :meth:`read_secret`
        may leave this unimplemented; the resolver falls back to
        :meth:`read_secret` and disables drift detection for that
        backend.
        """
        raise NotImplementedError  # pragma: no cover


# ---------------------------------------------------------------------------
# CredentialResolver
# ---------------------------------------------------------------------------


class CredentialResolver:
    """Resolves Atlassian credentials with strict scope isolation.

    Scope semantics
    ---------------

    * ``scope="org"`` (default; ``"bot"`` is a deprecated alias) - bot /
      worker credentials. The resolver looks up
      ``automation.department_bots.credential_ref`` in Postgres and
      reads the resulting Vault path. The path **must** be org-shaped
      (i.e. it does not contain ``atlassian/_user``); otherwise a
      :class:`CredentialScopeViolationError` is raised.
    * ``scope="user"`` - Streamlit per-user session credentials. The
      resolver bypasses Postgres entirely and reads
      ``secret/atlassian/_user_session/{session_id}/{service}``
      directly. *session_id* is required and must be a non-empty
      string; *dept_id* is accepted for diagnostics but is **not**
      consulted for path construction.

    Cross-scope reads (e.g. an ``"org"`` call ending up at a
    ``_user_session`` path due to a corrupted ``credential_ref`` row,
    or a ``"user"`` call somehow being routed to a dept path) emit a
    ``credential_scope_violation_attempt`` audit event and raise
    :class:`CredentialScopeViolationError`.

    Parameters
    ----------
    vault:
        A Vault client with an async ``read_secret(path)`` method that
        returns the KV-v2 ``data.data`` dict (or ``None`` for 404).
    db:
        An asyncpg connection pool connected to the automation database.
    audit_logger:
        Optional :class:`audit_logger.AuditLogger`. If supplied,
        cross-scope violations write a critical audit row before
        raising; if ``None`` the resolver still raises but skips the
        audit write (used in unit tests that do not exercise the audit
        path).
    """

    def __init__(
        self,
        vault: Any,
        db: asyncpg.Pool,
        audit_logger: Any | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vault = vault
        self._db = db
        self._audit_logger = audit_logger
        self._dept_bots_cache: list[DeptBotRow] | None = None
        # In-memory TTL cache. Keyed by
        # ``(scope, dept_id_or_session_id, service)`` so org and user
        # scopes share the same map without ever colliding - ``scope``
        # is part of the key and the second slot semantics differ per
        # scope, so two reads with the same ``(dept_id, service)`` but
        # different scopes resolve to two distinct entries.
        self._cred_cache: dict[CacheKey, CachedEntry] = {}
        # Injectable clock for deterministic TTL tests; defaults to
        # ``datetime.now(tz=UTC)``. The clock MUST return tz-aware
        # values - the resolver subtracts cached vs. now to compute
        # the TTL window and a naive datetime would raise.
        self._clock: Callable[[], datetime] = clock or _utc_now

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def list_dept_bots(self) -> list[DeptBotRow]:
        """Return all department bot registrations (cached after first call).

        Reads from ``automation.department_bots`` and caches the result
        in memory for the lifetime of this resolver instance. This is
        acceptable because bot registrations change infrequently and the
        resolver is typically short-lived (per-request or per-workflow).
        """
        if self._dept_bots_cache is not None:
            return self._dept_bots_cache

        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, department_id, service, credential_ref,
                       account_id, username, deployment
                FROM automation.department_bots
                ORDER BY department_id, service
                """
            )

        self._dept_bots_cache = [
            DeptBotRow(
                id=row["id"],
                department_id=row["department_id"],
                service=row["service"],
                credential_ref=row["credential_ref"],
                account_id=row["account_id"],
                username=row["username"],
                deployment=row["deployment"],
            )
            for row in rows
        ]
        return self._dept_bots_cache

    async def get(
        self,
        dept_id: str,
        service: str,
        scope: str = "org",
        *,
        session_id: str | None = None,
    ) -> AtlassianCredential:
        """Resolve credentials for a department × service pair.

        Parameters
        ----------
        dept_id:
            Department identifier (e.g., ``"payment"``). For
            ``scope="user"`` this is accepted only for diagnostics
            (audit ``dept_id`` field) and is **not** consulted for
            path construction.
        service:
            Atlassian service name (``"jira"``, ``"bitbucket"``, or
            ``"confluence"``).
        scope:
            Credential scope. ``"org"`` (default) reads the worker
            bot credential via ``automation.department_bots``;
            ``"user"`` reads the Streamlit per-user session
            credential at
            ``secret/atlassian/_user_session/{session_id}/{service}``.
            ``"bot"`` is a deprecated alias for ``"org"``.
        session_id:
            Required when ``scope="user"`` (raises
            :class:`ValueError` otherwise). Ignored for
            ``scope="org"``.

        Returns
        -------
        AtlassianCredential
            The resolved credential with ``url``, ``username``, and
            ``personal_token`` fields.

        Raises
        ------
        ValueError
            If *scope* is not one of ``"org"``, ``"user"``,
            ``"bot"`` (deprecated), or if ``scope="user"`` is
            requested without a non-empty *session_id*.
        CredentialScopeViolationError
            If the resolved Vault path violates the scope's prefix
            invariant (cross-scope read attempt). A
            ``credential_scope_violation_attempt`` audit row is
            written before the exception is raised.
        CredentialResolutionError
            If the department bot row is not found, the Vault path
            returns 404, or the secret is missing required fields.
        """
        # Normalise legacy ``"bot"`` to the canonical ``"org"`` value.
        # The DeprecationWarning is emitted at the public-API edge in
        # :func:`http_shared.auth_inject.with_atlassian_creds`; here we
        # silently rewrite so internal callers and the property test
        # observe a single canonical value.
        if scope == "bot":
            scope = "org"

        if scope not in ("org", "user"):
            raise ValueError(
                f"scope must be 'org' or 'user' (or deprecated 'bot'); "
                f"got {scope!r}"
            )

        if scope == "user":
            if not session_id:
                raise ValueError(
                    "scope='user' requires a non-empty session_id"
                )
            # The user-session path is fully synthesised from
            # ``(session_id, service)``; no Postgres lookup is
            # required and the cache key uses ``session_id`` as the
            # second slot. ``dept_id`` is preserved for audit
            # diagnostics only.
            cache_id = session_id
            path = self._build_user_session_path(session_id, service)
            # Defensive: the constructed path must NOT match the org
            # shape. By construction it always contains the user
            # infix, so the inverse check is what matters here - if a
            # caller somehow triggers a path without the user infix
            # while requesting scope="user" we raise.
            if _USER_PATH_INFIX not in path:
                await self._audit_scope_violation(
                    scope="user",
                    attempted_path=path,
                    dept_id=dept_id or "_unknown",
                    service=service,
                )
                raise CredentialScopeViolationError(
                    scope="user",
                    attempted_path=path,
                    dept_id=dept_id or "_unknown",
                    service=service,
                )
        else:  # scope == "org"
            cache_id = dept_id
            # 1. Find the credential_ref from Postgres.
            path = await self._lookup_credential_ref(dept_id, service)
            # 2. Cross-scope guard: an org-scope read MUST NOT touch
            # the per-user prefix. A leaked credential_ref pointing
            # at ``_user_session`` / ``_user_persisted`` is treated
            # as a critical security violation.
            if _USER_PATH_INFIX in path:
                await self._audit_scope_violation(
                    scope="org",
                    attempted_path=path,
                    dept_id=dept_id,
                    service=service,
                )
                raise CredentialScopeViolationError(
                    scope="org",
                    attempted_path=path,
                    dept_id=dept_id,
                    service=service,
                )

        # ------------------------------------------------------------
        # TTL cache lookup
        # ------------------------------------------------------------
        # Cache key uses the *canonical* scope (``"bot"`` already
        # rewritten to ``"org"``) so legacy callers and modern callers
        # share a single cache entry per logical credential.
        cache_key: CacheKey = (scope, cache_id, service)
        cached = self._cred_cache.get(cache_key)
        now = self._clock()
        if cached is not None and now - cached.cached_at < _CACHE_TTL:
            # TTL still valid: return without touching Vault. This is
            # the hot path; everything below is the refresh branch.
            return cached.credential

        # ------------------------------------------------------------
        # Cache miss or stale entry: read from Vault.
        # ------------------------------------------------------------
        # Prefer ``read_with_metadata`` so we can compare
        # ``data.metadata.created_time`` for drift detection; fall
        # back to ``read_secret`` for backends that don't surface
        # metadata (legacy fakes, simple in-memory test stubs).
        secret, vault_created_time = await self._read_secret_with_metadata(path)
        if secret is None:
            # Stale cache entries are never served on 404 - a removed
            # secret should fail loudly so operators notice rather
            # than silently using whatever value was last cached.
            self._cred_cache.pop(cache_key, None)
            raise CredentialResolutionError(
                dept_id,
                service,
                f"Vault path not found: {path}",
            )

        # Extract and validate required fields.
        url = secret.get("url", "")
        username = secret.get("username", "")
        personal_token = secret.get("personal_token", "")

        if not url or not username or not personal_token:
            missing = [
                field_name
                for field_name, value in [
                    ("url", url),
                    ("username", username),
                    ("personal_token", personal_token),
                ]
                if not value
            ]
            raise CredentialResolutionError(
                dept_id,
                service,
                f"incomplete secret at {path}: missing {missing}",
            )

        credential = AtlassianCredential(
            url=url,
            username=username,
            personal_token=personal_token,
        )

        # ------------------------------------------------------------
        # Drift detection
        # ------------------------------------------------------------
        # When refreshing an entry whose ``vault_created_time`` is
        # strictly less than the freshly-read value, emit a
        # ``vault_credential_refreshed`` audit row so operators can
        # correlate worker rotation pickup with the admin-dashboard
        # *Security* drift banner. Drift is **only** asserted when
        # both timestamps are non-empty strings - a missing prior
        # value means the cache had no metadata to compare against
        # (e.g. populated by the legacy ``read_secret`` path) and
        # would yield a spurious "rotation" event on first refresh.
        if (
            cached is not None
            and cached.vault_created_time
            and vault_created_time
            and vault_created_time > cached.vault_created_time
        ):
            await self._audit_credential_refreshed(
                scope=scope,
                dept_id=dept_id,
                service=service,
                prev_created_time=cached.vault_created_time,
                new_created_time=vault_created_time,
            )

        self._cred_cache[cache_key] = CachedEntry(
            credential=credential,
            cached_at=now,
            vault_created_time=vault_created_time,
        )

        return credential

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_session_path(session_id: str, service: str) -> str:
        """Construct the canonical per-user session Vault path.

        The layout mirrors the single source of truth in
        :func:`automation_service.credentials.build_user_session_path`
        so the org-scope resolver and the user-scope resolver agree on
        path shape without importing across module boundaries (the
        ``decision`` package is intentionally framework-light).
        """
        return f"secret/atlassian/_user_session/{session_id}/{service}"

    async def _lookup_credential_ref(self, dept_id: str, service: str) -> str:
        """Find the Vault credential_ref for a department × service pair.

        Raises :class:`CredentialResolutionError` if no matching row
        exists in ``automation.department_bots``.
        """
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT credential_ref
                FROM automation.department_bots
                WHERE department_id = $1 AND service = $2
                """,
                dept_id,
                service,
            )

        if row is None:
            raise CredentialResolutionError(
                dept_id,
                service,
                f"no bot registration for dept={dept_id!r} service={service!r}",
            )

        return row["credential_ref"]

    async def _audit_scope_violation(
        self,
        *,
        scope: str,
        attempted_path: str,
        dept_id: str,
        service: str,
    ) -> None:
        """Emit a ``credential_scope_violation_attempt`` audit row.

        Best-effort: if the audit_logger is unavailable or its write
        raises, we swallow the failure here and let the caller raise
        :class:`CredentialScopeViolationError` so that a broken audit
        sink never masks the security signal at the call site.

        The audit payload deliberately records ``attempted_path`` (the
        Vault key, not the secret value) so operators can correlate
        the violation with rotation logs without exposing secret
        material.
        """
        if self._audit_logger is None:
            return

        # Local imports keep ``audit_logger`` an optional dependency at
        # import time - the ``decision`` package is loaded by code paths
        # that do not always wire an audit sink (CLI tools, REPL).
        try:
            from audit_logger import AuditEvent  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - defensive
            return

        event = AuditEvent(
            actor_id="automation-service.credential_resolver",
            actor_role="system",
            dept_id=dept_id if dept_id and dept_id != "_unknown" else None,
            action="credential_scope_violation_attempt",
            resource=f"vault:{attempted_path}",
            result="denied",
            timestamp=datetime.now(timezone.utc),
            payload={
                "scope": scope,
                "attempted_path": attempted_path,
                "service": service,
                "dept_id": dept_id,
            },
        )
        try:
            await self._audit_logger.write(event)
        except Exception:  # noqa: BLE001 - best-effort audit
            # Deliberately swallow - the violation is still raised by
            # the caller, so the security signal reaches the workflow
            # boundary regardless of audit-pipeline health.
            pass

    async def _read_secret_with_metadata(
        self, path: str
    ) -> tuple[dict[str, str] | None, str | None]:
        """Read a Vault secret returning ``(data, created_time)``.

        Tries :meth:`VaultClient.read_with_metadata` first so the
        resolver can populate ``CachedEntry.vault_created_time`` for
        drift detection. When the underlying
        backend does not implement that method (legacy fakes, simple
        in-memory stubs), the resolver falls back to
        :meth:`VaultClient.read_secret` and returns ``(data, None)``;
        drift detection is disabled for that key but TTL caching
        still works.

        Returns a ``(None, None)`` tuple when Vault returns 404 so
        the caller can distinguish missing paths from
        metadata-less backends without re-reading.
        """
        read_with_metadata = getattr(self._vault, "read_with_metadata", None)
        if callable(read_with_metadata):
            try:
                result = await read_with_metadata(path)
            except NotImplementedError:
                # Subclasses may inherit the protocol stub but not
                # override it; drop to the legacy path silently.
                result = None
            else:
                if result is None:
                    return None, None
                data, metadata = result
                created_time = None
                if isinstance(metadata, dict):
                    raw = metadata.get("created_time")
                    if isinstance(raw, str) and raw:
                        created_time = raw
                return data, created_time

        # Legacy / metadata-less backend.
        data = await self._vault.read_secret(path)
        return data, None

    async def _audit_credential_refreshed(
        self,
        *,
        scope: str,
        dept_id: str,
        service: str,
        prev_created_time: str,
        new_created_time: str,
    ) -> None:
        """Emit a ``vault_credential_refreshed`` audit row.

        Best-effort: failures inside the audit write must not
        propagate to the workflow caller, which has already
        successfully resolved the new credential. The drift signal
        is observable, but a broken audit sink should not destabilise
        worker hot paths.
        """
        if self._audit_logger is None:
            return

        try:
            from audit_logger import AuditEvent  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - defensive
            return

        event = AuditEvent(
            actor_id="automation-service.credential_resolver",
            actor_role="system",
            dept_id=dept_id if dept_id else None,
            action="vault_credential_refreshed",
            resource=f"vault:{scope}/{dept_id}/{service}",
            result="ok",
            timestamp=self._clock(),
            payload={
                "scope": scope,
                "dept_id": dept_id,
                "service": service,
                "prev_created_time": prev_created_time,
                "new_created_time": new_created_time,
            },
        )
        try:
            await self._audit_logger.write(event)
        except Exception:  # noqa: BLE001 - best-effort audit
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current UTC time as a tz-aware :class:`datetime`.

    Wrapped in a module-level helper so it can be referenced by name
    from the resolver's ``__init__`` default - passing
    ``datetime.now`` directly would freeze the implementation to the
    bound method object at import time and complicate
    monkey-patching during tests.
    """
    return datetime.now(timezone.utc)
