"""Per-user > org-default credential resolution (task 12.5, R10.8).

Implements ``CredentialResolver.resolve(session_id, dept_id, service)``
as specified in
``.kiro/specs/platform-mimari-foundation/design.md`` §"Vault path
domeni" and ``requirements.md`` §Requirement 10.8.

Lookup order
------------

1. **Per-user session credential** —
   ``vault:atlassian/_user_session/<session_id>/<service>``. This path
   is populated by the Streamlit per-user session form (task 12.4)
   when a user supplies their own Atlassian credential for the
   lifetime of an interactive session.
2. **Org-default credential** — ``vault:atlassian/<dept_id>/<service>``.
   This is the bot credential registered through the department
   wizard (task 5.4 / 5.3).
3. **Neither present** — :class:`CredentialMissing` is raised. The
   exception's ``error_code`` attribute equals ``"credential_missing"``
   so callers can match the canonical audit event name without
   string-stringifying the exception.

Why not extend ``decision.credential_resolver``?
------------------------------------------------

``src/decision/credential_resolver.py`` is the *bot-only* resolver
that talks to ``automation.department_bots`` (Postgres) for the
``credential_ref`` lookup. The per-user/session flow does not touch
Postgres — its source of truth is purely the Vault path layout — so
keeping the two resolvers in separate modules avoids muddying the
``decision`` package with session-state plumbing. The two resolvers
**can** be composed at the call site if a workflow ever needs to
combine bot-default with a per-user override (Q6/Q7).

VaultClient contract
--------------------

This module depends on a *structural* (duck-typed) Vault client that
exposes a synchronous ``read(path: str) -> Mapping[str, str]`` method
which raises :class:`KeyError` when the path does not exist. This
matches the canonical :class:`vault_client.VaultClient` protocol
shipped under ``platform/libs/vault_client/`` (task 2.2) without
adding a hard build-time dependency on that package — production
wiring imports the real backend, tests inject an in-memory fake.

Requirements: 10.8
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Mapping, Protocol, runtime_checkable

__all__ = [
    "AtlassianService",
    "CredentialMissing",
    "CredentialResolver",
    "ResolvedCredential",
    "VaultReader",
    "build_org_default_path",
    "build_user_session_path",
]

# ---------------------------------------------------------------------------
# Path helpers (single source of truth for the Vault layout)
# ---------------------------------------------------------------------------

#: Atlassian surfaces this resolver knows about. Mirrors the ``service``
#: enum used everywhere else in the platform (R6.1, ``departments.schema.json``).
AtlassianService = Literal["jira", "bitbucket", "confluence"]

#: Per-user / per-session credential path template (R10.7, design
#: §"Vault path domeni"). The session id is opaque to this module —
#: callers (Streamlit / assistant-service) generate and rotate it.
_USER_SESSION_TEMPLATE: Final[str] = "vault:atlassian/_user_session/{session_id}/{service}"

#: Org-default (department bot) credential path template (R6.1).
_ORG_DEFAULT_TEMPLATE: Final[str] = "vault:atlassian/{dept_id}/{service}"


def build_user_session_path(session_id: str, service: AtlassianService) -> str:
    """Return the per-user session Vault path for *session_id* / *service*.

    Centralising the format here keeps the resolver and any future
    cleanup tooling (e.g. session TTL eviction) in agreement on the
    canonical layout.
    """

    return _USER_SESSION_TEMPLATE.format(session_id=session_id, service=service)


def build_org_default_path(dept_id: str, service: AtlassianService) -> str:
    """Return the org-default (bot) Vault path for *dept_id* / *service*."""

    return _ORG_DEFAULT_TEMPLATE.format(dept_id=dept_id, service=service)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """Outcome of a successful :meth:`CredentialResolver.resolve` call.

    Attributes:
        data: The Vault KV-v2 ``data:`` payload as a flat mapping. The
            resolver does **not** validate the inner shape — callers
            (e.g. the Atlassian client) decide which keys are required
            for their surface, so this resolver stays usable for both
            ``token``-style and ``email + api_token``-style entries.
        path: The Vault path that produced *data*. Useful for audit /
            diagnostics so operators can see whether a request was
            served by a per-user override or the org default without
            poking at the resolver internals.
        source: ``"user_session"`` if the credential came from the
            per-user path, ``"org_default"`` for the department bot
            fallback. The literal values match the audit event payload
            convention.
    """

    data: Mapping[str, str]
    path: str
    source: Literal["user_session", "org_default"]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CredentialMissing(LookupError):
    """Raised when neither per-user nor org-default credential exists.

    Inherits from :class:`LookupError` so generic callers that already
    handle "not found" branches (``except LookupError:``) keep working,
    while specific call sites can catch :class:`CredentialMissing`
    directly to emit the ``credential_missing`` audit event.

    Attributes:
        error_code: Always ``"credential_missing"`` — the canonical
            audit event name (R10.8).
        session_id: The session id that was tried.
        dept_id: The department id that was tried.
        service: The Atlassian surface that was requested.
        attempted_paths: Both Vault paths the resolver tried, in
            lookup order (per-user first). Useful for runbooks /
            diagnostics; never contains the actual secret value.
    """

    error_code: Final[str] = "credential_missing"

    def __init__(
        self,
        *,
        session_id: str,
        dept_id: str,
        service: AtlassianService,
        attempted_paths: tuple[str, str],
    ) -> None:
        self.session_id = session_id
        self.dept_id = dept_id
        self.service = service
        self.attempted_paths = attempted_paths
        super().__init__(
            f"credential_missing: no per-user or org-default credential found "
            f"for session={session_id!r} dept={dept_id!r} service={service!r}; "
            f"tried paths {list(attempted_paths)!r}"
        )


# ---------------------------------------------------------------------------
# Vault client protocol (duck-typed)
# ---------------------------------------------------------------------------


@runtime_checkable
class VaultReader(Protocol):
    """Minimal structural slice of :class:`vault_client.VaultClient`.

    Only the synchronous ``read`` operation is needed by this module;
    a separate module (task 5.3) wraps the same backend for write /
    rotation flows. Keeping the protocol slice tight means tests can
    inject a 3-line fake and avoid pulling in the full ``vault_client``
    package.

    Implementations MUST raise :class:`KeyError` when the requested
    path does not exist; any other exception (network error, auth
    failure, …) is propagated to the caller unchanged so transient
    failures do not get masked as ``credential_missing``.
    """

    def read(self, path: str) -> Mapping[str, str]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# CredentialResolver
# ---------------------------------------------------------------------------


class CredentialResolver:
    """Resolve Atlassian credentials with per-user > org-default priority.

    The resolver is intentionally tiny — all of its logic is the path
    construction and the two-step fallback. Auditing,
    rate-limiting and session-TTL eviction are layered on top by the
    calling service (assistant-service / automation-service) so this
    class stays a pure function over its Vault input (no global state,
    no logging side-effects, no clocks).

    Parameters
    ----------
    vault:
        Any object satisfying the :class:`VaultReader` protocol. In
        production this is :func:`vault_client.make_client(env)`; in
        tests it is an in-memory fake returning fixture dicts.
    """

    def __init__(self, vault: VaultReader) -> None:
        self._vault = vault

    def _read(self, path: str) -> Mapping[str, str]:
        """Read a Vault path from either string- or VaultPath-based clients."""
        try:
            return self._vault.read(path)
        except AttributeError:
            from vault_client import VaultPath  # type: ignore[import-not-found]

            return self._vault.read(VaultPath.parse(path))  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        session_id: str,
        dept_id: str,
        service: AtlassianService,
    ) -> ResolvedCredential:
        """Return the credential for *session_id* / *dept_id* / *service*.

        Lookup order (R10.8):

        1. ``vault:atlassian/_user_session/<session_id>/<service>``
        2. ``vault:atlassian/<dept_id>/<service>``

        If both lookups raise :class:`KeyError` the resolver raises
        :class:`CredentialMissing`. Any other exception (e.g. a Vault
        HTTP 5xx) is propagated to the caller unchanged so transient
        infrastructure failures are not mis-reported as missing
        credentials.

        Parameters
        ----------
        session_id:
            Opaque per-session identifier produced by the Streamlit /
            assistant layer. Empty strings are rejected with
            :class:`ValueError` because the resulting path
            (``…/_user_session//<service>``) would be ambiguous and
            the canonical regex on Vault paths forbids consecutive
            slashes anyway.
        dept_id:
            Department identifier (must match the ``id`` field in
            ``departments.json``). Empty values are rejected for the
            same reason as ``session_id``.
        service:
            One of ``"jira"``, ``"bitbucket"``, ``"confluence"``.

        Returns
        -------
        ResolvedCredential
            Populated with the Vault ``data:`` payload, the path that
            produced it and a ``source`` discriminator.

        Raises
        ------
        CredentialMissing
            When neither Vault path exists.
        ValueError
            When *session_id*, *dept_id* or *service* fails the
            structural pre-conditions described above.
        """

        # Structural validation — keep error messages free of any
        # secret material so they are safe to surface in HTTP 4xx
        # responses or audit events.
        if not session_id:
            raise ValueError("session_id must be a non-empty string")
        if not dept_id:
            raise ValueError("dept_id must be a non-empty string")
        if service not in ("jira", "bitbucket", "confluence"):
            raise ValueError(
                f"service must be one of 'jira', 'bitbucket', 'confluence'; "
                f"got {service!r}"
            )

        user_path = build_user_session_path(session_id, service)
        org_path = build_org_default_path(dept_id, service)

        # 1) Per-user override has priority. We treat ``KeyError`` as
        # "missing" — the protocol contract; any other exception
        # bubbles up to surface real infra failures distinctly.
        try:
            data = self._read(user_path)
        except KeyError:
            data = None

        if data is not None:
            return ResolvedCredential(data=data, path=user_path, source="user_session")

        # 2) Fall back to the org-default bot credential.
        try:
            data = self._read(org_path)
        except KeyError:
            data = None

        if data is not None:
            return ResolvedCredential(data=data, path=org_path, source="org_default")

        # 3) Both absent — surface the canonical audit error code.
        raise CredentialMissing(
            session_id=session_id,
            dept_id=dept_id,
            service=service,
            attempted_paths=(user_path, org_path),
        )

    async def get(
        self,
        dept_id: str,
        service: AtlassianService,
        *,
        scope: Literal["org", "user"] = "org",
        session_id: str | None = None,
    ) -> Mapping[str, str]:
        """Async adapter consumed by ``http_shared.with_atlassian_creds``.

        Automation workers use org-default bot credentials. Interactive
        user scopes may pass ``session_id`` to reuse the same priority
        resolver as :meth:`resolve`.
        """
        if scope == "org":
            if not dept_id:
                raise ValueError("dept_id must be a non-empty string")
            if service not in ("jira", "bitbucket", "confluence"):
                raise ValueError(
                    "service must be one of 'jira', 'bitbucket', 'confluence'"
                )
            return self._read(build_org_default_path(dept_id, service))
        if scope == "user":
            if not session_id:
                raise ValueError("session_id is required for user scope")
            return self.resolve(session_id, dept_id, service).data
        raise ValueError("scope must be 'org' or 'user'")
