"""Audit-emitting credential resolver wrapper for assistant-service.

Thin call-site wrapper around
:class:`automation_service.credentials.CredentialResolver` that adds the
**audit-emission** contract for credential resolution.

The foundation resolver
(:class:`automation_service.credentials.CredentialResolver`) owns the
priority decision (per-user → org-default) and the path layout. This
wrapper layers exactly one extra responsibility on top:

1. **Audit emission at the call-site.** Every resolution attempt emits
   exactly one of three canonical audit events:

   * ``credential_resolved_per_user_session`` (``result="ok"``) - the
     per-user override at
     ``vault:atlassian/_user_session/<session_id>/<service>`` produced
     the credential.
   * ``credential_resolved_org_default`` (``result="ok"``) - the
     per-user path was missing and the org-default path
     ``vault:atlassian/<dept_id>/<service>`` produced the credential.
   * ``credential_resolve_failed`` (``result="error"``) - neither path
     existed; the wrapper re-raises :class:`CredentialNotFoundError`
     after writing the audit event.

2. **Plain-credential redaction in the audit payload.** The audit
   ``payload`` dict carries diagnostic identifiers only - ``service``,
   ``vault_path`` (path string, never the secret), and the missing
   ``attempted_paths`` tuple on failure. The Vault ``data`` mapping
   (which contains the plain token) is **never** propagated into the
   payload. The audit log must not leak those values.

The resolver itself stays a thin proxy - there is no caching, no
retry, no rate-limiting layered here. Those concerns live one layer
up (the chat handler / MCP credential injection helper) so this
wrapper remains a pure function over its inputs (resolver call +
audit write) and survives the hypothesis-driven property test
without flakiness.

Why a separate module instead of extending the foundation resolver?
-------------------------------------------------------------------

The foundation :class:`CredentialResolver` is intentionally
audit-agnostic: it is consumed by both ``automation-service`` and
``assistant-service``, and the two services emit audit events under
different ``actor_role`` regimes (``"system"`` for background workers,
the OIDC subject's role for the chat path). Hard-coding the audit
sink inside the foundation resolver would either pick the wrong role
for one service or force every caller to pass an audit writer even
when they don't need one. Keeping the audit wiring at the call site
mirrors the "audit at call-site" contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final, Protocol, runtime_checkable

# The foundation resolver lives in ``automation_service.credentials``.
# We import the symbols by name so the assistant-service has a single
# import surface the test (``test_credential_resolve_priority.py``) can
# patch / inspect.
from automation_service.credentials import (
    AtlassianService,
    CredentialMissing,
    CredentialResolver as _FoundationResolver,
    ResolvedCredential,
    VaultReader,
    build_org_default_path,
    build_user_session_path,
)
from audit_logger import AuditEvent

__all__ = [
    "AUDIT_ACTION_FAILED",
    "AUDIT_ACTION_ORG_DEFAULT",
    "AUDIT_ACTION_PER_USER",
    "AtlassianService",
    "AuditingCredentialResolver",
    "AuditWriterProtocol",
    "CredentialNotFoundError",
    "PLAIN_CREDENTIAL_KEYS",
    "ResolvedCredential",
    "VaultReader",
]


# ---------------------------------------------------------------------------
# Canonical audit action names
# ---------------------------------------------------------------------------

#: Emitted when the per-user override at
#: ``vault:atlassian/_user_session/<session_id>/<service>`` produced
#: the credential.
AUDIT_ACTION_PER_USER: Final[str] = "credential_resolved_per_user_session"

#: Emitted when the per-user path was missing and the org-default path
#: ``vault:atlassian/<dept_id>/<service>`` produced the credential.
AUDIT_ACTION_ORG_DEFAULT: Final[str] = "credential_resolved_org_default"

#: Emitted when neither the per-user nor the org-default path exists.
#: The wrapper writes
#: this event **before** re-raising :class:`CredentialNotFoundError`
#: so failures are observable in the audit log even when the caller
#: drops the exception.
AUDIT_ACTION_FAILED: Final[str] = "credential_resolve_failed"


#: The resource URN-like string included on every audit event the
#: wrapper writes. The format ``credential:<dept_id>/<service>``
#: matches the convention used by the foundation audit logger
#: (``workflow:<type>``, ``department:<id>``, …).
def _resource(dept_id: str, service: AtlassianService) -> str:
    return f"credential:{dept_id}/{service}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CredentialNotFoundError(CredentialMissing):
    """Raised when neither per-user nor org-default credential exists.

    Subclasses :class:`CredentialMissing` (the canonical foundation
    error) so existing call sites that already handle the foundation
    error keep working. The dedicated subclass exists because
    ``CredentialNotFoundError`` is the documented public error; the
    alias avoids forcing every
    assistant-service call site to import a name from the
    automation-service package and keeps the public failure mode of
    this wrapper independently importable.

    The ``error_code`` attribute is inherited from
    :class:`CredentialMissing` and remains ``"credential_missing"`` -
    the canonical audit error code. The wrapper, however, writes the
    audit event under ``action=credential_resolve_failed`` because
    that is the action name used for this failure; the two strings refer
    to different fields of the same row.
    """


# ---------------------------------------------------------------------------
# Audit writer protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditWriterProtocol(Protocol):
    """Minimal write surface this wrapper needs.

    Production wiring passes the real
    :class:`audit_logger.AuditLogger` (whose ``write`` method enforces
    the ``actor_role`` invariant before emitting INSERT). Tests
    inject a list-backed fake to assert the emitted events.

    Synchronous or asynchronous - the wrapper itself is synchronous
    so we accept a plain callable. Callers wanting async semantics
    can wrap with ``asyncio.run_coroutine_threadsafe`` on the
    boundary; that is out of scope for this property test.
    """

    def write(self, event: AuditEvent) -> None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# AuditingCredentialResolver
# ---------------------------------------------------------------------------


class AuditingCredentialResolver:
    """Wrap :class:`CredentialResolver` with audit emission at the call-site.

    Parameters
    ----------
    resolver:
        The underlying :class:`automation_service.credentials.CredentialResolver`
        instance - owns the Vault reads and the priority decision.
    audit:
        Anything satisfying :class:`AuditWriterProtocol`. Each
        :meth:`resolve` call emits exactly one event through this
        sink.
    actor_id:
        The audit ``actor_id`` to record (typically the OIDC
        ``sub`` of the chat user). Required - empty strings would
        slip past the foundation ``actor_role`` CHECK and pollute
        the audit log with anonymous rows.
    actor_role:
        The audit ``actor_role`` (``"viewer" | "lead" | "admin" |
        "dept_admin" | "system"``). Forwarded verbatim - the
        wrapper does not second-guess the caller's RBAC mapping.
    clock:
        Optional callable returning a timezone-aware
        :class:`~datetime.datetime`. Defaults to
        ``datetime.now(timezone.utc)`` and is exposed as a
        constructor argument for deterministic property tests.

    Notes
    -----
    The wrapper is intentionally constructed once per request /
    chat-session: ``actor_id`` and ``actor_role`` are fixed for the
    lifetime of the instance, so callers who serve multiple users
    on a shared resolver should rebuild it (cheap - no I/O at
    construction time).
    """

    def __init__(
        self,
        *,
        resolver: _FoundationResolver,
        audit: AuditWriterProtocol,
        actor_id: str,
        actor_role: str,
        clock=lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("actor_id must be a non-empty string")
        if not isinstance(actor_role, str) or not actor_role:
            raise ValueError("actor_role must be a non-empty string")

        self._resolver = resolver
        self._audit = audit
        self._actor_id = actor_id
        self._actor_role = actor_role
        self._clock = clock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        session_id: str,
        dept_id: str,
        service: AtlassianService,
    ) -> ResolvedCredential:
        """Resolve credentials and emit the matching audit event.

        Three outcomes:

        * Per-user override hit → ``credential_resolved_per_user_session``
          (``result="ok"``); the resolver's
          ``ResolvedCredential.source == "user_session"``.
        * Org-default fallback hit →
          ``credential_resolved_org_default`` (``result="ok"``); the
          resolver's ``ResolvedCredential.source == "org_default"``.
        * Both paths missing → ``credential_resolve_failed``
          (``result="error"``) is written **before**
          :class:`CredentialNotFoundError` is re-raised. The audit
          payload carries the missing ``attempted_paths`` tuple
          (path strings, no plain credential).
        """

        try:
            resolved = self._resolver.resolve(session_id, dept_id, service)
        except CredentialMissing as err:
            user_path = build_user_session_path(session_id, service)
            org_path = build_org_default_path(dept_id, service)
            self._audit.write(
                AuditEvent(
                    actor_id=self._actor_id,
                    actor_role=self._actor_role,  # type: ignore[arg-type]
                    dept_id=dept_id,
                    action=AUDIT_ACTION_FAILED,
                    resource=_resource(dept_id, service),
                    result="error",
                    timestamp=self._clock(),
                    payload={
                        "service": service,
                        "session_id": session_id,
                        # Path strings only - never the Vault `data`
                        # mapping.
                        "attempted_paths": list(err.attempted_paths),
                    },
                )
            )
            # Re-raise as the documented public type so callers can
            # `except CredentialNotFoundError` without importing the
            # foundation alias.
            raise CredentialNotFoundError(
                session_id=session_id,
                dept_id=dept_id,
                service=service,
                attempted_paths=(user_path, org_path),
            ) from err

        action = (
            AUDIT_ACTION_PER_USER
            if resolved.source == "user_session"
            else AUDIT_ACTION_ORG_DEFAULT
        )
        self._audit.write(
            AuditEvent(
                actor_id=self._actor_id,
                actor_role=self._actor_role,  # type: ignore[arg-type]
                dept_id=dept_id,
                action=action,
                resource=_resource(dept_id, service),
                result="ok",
                timestamp=self._clock(),
                # Path string only - no Vault `data` mapping. The
                # `source` discriminator lets dashboards filter
                # per-user vs org-default events without parsing the
                # path.
                payload={
                    "service": service,
                    "session_id": session_id,
                    "vault_path": resolved.path,
                    "source": resolved.source,
                },
            )
        )
        return resolved


# Plain-credential payload keys that MUST NOT appear in any audit
# payload this wrapper emits. Exposed for the property test so the
# leak invariant is asserted against the same
# canonical key list the production code uses (matches the keys
# stored under ``vault:atlassian/.../<service>`` per the bot
# credential schema in ``departments.schema.json``).
PLAIN_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"personal_token", "api_token", "token", "password", "secret"}
)
