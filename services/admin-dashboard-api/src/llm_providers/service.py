"""``ProviderService`` — business logic for LLM provider management.

Orchestrates the (asyncpg transaction, Vault write) pair, applies
masking, dispatches to :class:`~llm_providers.connection_tester.ConnectionTester`
and emits audit events. The class is intentionally thin — every method
is a single, named-purpose flow that the router (in
:mod:`routers.llm_providers`) calls as an HTTP shim.

The service holds **no global state**: every collaborator (asyncpg
pool, :class:`VaultClient`, :class:`ConnectionTester`, audit sink) is
injected through the constructor so tests can replace each one with a
hand-rolled fake. The router builds a fresh service per request from
the lifespan-managed collaborators on ``app.state``.

Error handling
--------------

* Vault write failure during create → ``ROLLBACK``, raise
  :class:`VaultWriteFailed` (→ 502 ``vault_write_failed``) (R3.5).
* Rollback itself fails → log ``llm_provider_rollback_failed`` at
  ERROR with ``{provider_id, exc.__class__.__name__}`` and still
  surface the 502 (R3.6).
* Delete blocked by referencing override → raise
  :class:`ProviderInUse` (→ 409 ``provider_in_use``) (R1.7).
* Vault delete failure → leave the row, raise
  :class:`VaultDeleteFailed` (→ 502 ``vault_delete_failed``) and emit
  ``llm_provider_delete_vault_failed`` audit (R3.7).
* Sink failures are swallowed at WARNING log level — they NEVER mask
  the underlying HTTP response (R12.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID, uuid4

from audit_logger import AuditEvent
from http_shared.redaction import redact_text

from ..lifecycle.vault_client import VaultClient, VaultWriteError
from .connection_tester import ConnectionTester, TestRequest
from .dept_override_repository import DeptOverrideRepository
from .masking import mask
from .repository import LLMProviderRepository, LLMProviderRow
from .schemas import (
    ConnectionTestError,
    ConnectionTestResult,
    DeptOverrideDTO,
    LLMProviderConfigDTO,
    ProviderUpdate,
    UnsavedTestRequest,
)


__all__ = [
    "ProviderService",
    "VaultWriteFailed",
    "VaultDeleteFailed",
    "ProviderInUse",
    "ProviderNotFound",
    "ProviderInactive",
    "DUMMY_AUDIT_DEPT",
]


_LOG = logging.getLogger(__name__)


#: Audit ``dept_id`` value for cross-dept (global) provider events.
#: The ``audit_events`` schema treats ``None`` as "no dept context";
#: the constant exists so audit emission stays consistent across
#: every flow that targets the provider catalogue rather than a
#: specific department.
DUMMY_AUDIT_DEPT: str | None = None


# ---------------------------------------------------------------------------
# Service-layer exceptions (router maps each to an HTTP status code)
# ---------------------------------------------------------------------------


class VaultWriteFailed(Exception):
    """Raised by :meth:`ProviderService.create` / :meth:`update` on Vault failure.

    Router maps this to ``502 {"error": "vault_write_failed", ...}``.
    """

    def __init__(self, provider_id: UUID, cause: Exception) -> None:
        super().__init__(
            f"vault write failed for provider_id={provider_id}: {cause}"
        )
        self.provider_id = provider_id
        self.cause = cause


class VaultDeleteFailed(Exception):
    """Raised by :meth:`ProviderService.delete` on Vault delete failure.

    Row stays in Postgres; router maps to ``502
    {"error": "vault_delete_failed", ...}``.
    """

    def __init__(self, provider_id: UUID, cause: Exception) -> None:
        super().__init__(
            f"vault delete failed for provider_id={provider_id}: {cause}"
        )
        self.provider_id = provider_id
        self.cause = cause


class ProviderInUse(Exception):
    """Raised by :meth:`ProviderService.delete` when a dept pins the provider.

    Router maps this to ``409 {"error": "provider_in_use", "dept_ids": [...]}``.
    """

    def __init__(self, provider_id: UUID, dept_ids: list[str]) -> None:
        super().__init__(
            f"provider {provider_id} is in use by {len(dept_ids)} dept(s)"
        )
        self.provider_id = provider_id
        self.dept_ids = dept_ids


class ProviderNotFound(Exception):
    """Raised by :meth:`ProviderService.set_override` on missing target.

    Router maps to ``422 {"error": "provider_not_found", ...}``.
    """

    def __init__(self, provider_id: UUID) -> None:
        super().__init__(f"provider {provider_id} does not exist")
        self.provider_id = provider_id


class ProviderInactive(Exception):
    """Raised by :meth:`ProviderService.set_override` on inactive target.

    Router maps to ``409 {"error": "provider_inactive", ...}``.
    """

    def __init__(self, provider_id: UUID) -> None:
        super().__init__(f"provider {provider_id} is inactive")
        self.provider_id = provider_id


# ---------------------------------------------------------------------------
# Audit sink protocol — narrow enough that LoggingAuditSink + the asyncpg
# writer both satisfy it.
# ---------------------------------------------------------------------------


class _AuditSink(Protocol):
    async def write(self, event: AuditEvent) -> None: ...


# ---------------------------------------------------------------------------
# Service implementation
# ---------------------------------------------------------------------------


@dataclass
class _ProviderCreateInput:
    """Normalised view of a :class:`ProviderCreate` instance.

    Kept module-private so the service can accept either a
    discriminated-union variant or an :class:`UnsavedTestRequest`
    (which carries the same fields) without branching on Pydantic
    internals at the call site.
    """

    provider_type: str
    name: str
    model: str
    context_length: int
    base_url: str | None
    api_key: str | None
    org_id: str | None
    reasoning_effort: str | None = None
    verbosity: str | None = None


class ProviderService:
    """Business-logic facade for LLM provider management.

    Composes the persistence + Vault + tester collaborators behind a
    narrow surface the router can call without knowing about
    transactions, masking or audit emission.
    """

    def __init__(
        self,
        *,
        pool: Any,
        vault_client: VaultClient,
        repo: LLMProviderRepository,
        override_repo: DeptOverrideRepository,
        connection_tester: ConnectionTester,
        audit_sink: _AuditSink,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._pool = pool
        self._vault = vault_client
        self._repo = repo
        self._override_repo = override_repo
        self._tester = connection_tester
        self._audit = audit_sink
        self._clock = clock or _utc_now

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    async def list_providers(self) -> list[LLMProviderConfigDTO]:
        """Return every provider with credentials masked.

        Reads every row, fetches each provider's Vault payload, and
        projects through :func:`mask`. Vault read failures degrade
        gracefully to an empty masked credential — the DTO still
        renders so the operator sees the row in the UI and can
        diagnose the missing credential separately.
        """

        async with self._pool.acquire() as conn:
            rows = await self._repo.list_all(conn)
        return [
            await self._row_to_dto(row, fetch_credentials=True) for row in rows
        ]

    async def get_provider(
        self, provider_id: UUID
    ) -> LLMProviderConfigDTO | None:
        """Return one provider with credentials masked, or ``None``."""

        async with self._pool.acquire() as conn:
            row = await self._repo.get(conn, provider_id)
        if row is None:
            return None
        return await self._row_to_dto(row, fetch_credentials=True)

    # -----------------------------------------------------------------
    # Mutations
    # -----------------------------------------------------------------

    async def create(
        self, payload: _ProviderCreateInput, *, actor_id: str
    ) -> LLMProviderConfigDTO:
        """Insert a fresh provider row + write credentials to Vault.

        Wraps the two writes in a single asyncpg transaction so a
        Vault failure rolls back the row and leaves the catalogue
        free of orphaned half-built providers (R3.5). Rollback
        failure is logged at ERROR with ``{provider_id, exception_class}``
        but the 502 still surfaces (R3.6).
        """

        provider_id = uuid4()
        credential_payload = _vault_payload_for_create(payload)

        async with self._pool.acquire() as conn:
            tx = conn.transaction()
            await tx.start()
            row: LLMProviderRow | None = None
            try:
                row = await self._repo.insert(
                    conn,
                    provider_id=provider_id,
                    provider_type=payload.provider_type,
                    name=payload.name,
                    model=payload.model,
                    context_length=payload.context_length,
                    base_url=payload.base_url,
                    reasoning_effort=payload.reasoning_effort,
                    verbosity=payload.verbosity,
                )
                await self._vault.write_llm_credentials(
                    provider_id=provider_id, payload=credential_payload
                )
            except VaultWriteError as exc:
                await self._safe_rollback(tx, provider_id)
                raise VaultWriteFailed(provider_id, exc) from exc
            except Exception:
                await self._safe_rollback(tx, provider_id)
                raise
            else:
                await tx.commit()

        assert row is not None
        dto = await self._row_to_dto(row, credentials=credential_payload)
        await self._emit_audit(
            action="llm_provider_created",
            actor_id=actor_id,
            resource=f"llm_provider:{provider_id}",
            result="ok",
            payload={
                "provider_id": str(provider_id),
                "provider_type": payload.provider_type,
                "name": payload.name,
                "model": payload.model,
            },
        )
        return dto

    async def update(
        self,
        provider_id: UUID,
        patch: ProviderUpdate,
        *,
        actor_id: str,
    ) -> LLMProviderConfigDTO | None:
        """Merge *patch* over the persisted row.

        Touches Vault only when at least one credential field
        (``api_key`` / ``org_id``) is present in the patch (R4.6).
        When *only* credential fields change the Postgres update is
        a no-op COALESCE; ``updated_at`` still moves to ``now()`` so
        the UI can render the latest configuration moment.

        Returns ``None`` when no row exists for *provider_id*.
        """

        credentials_changed = (
            patch.api_key is not None or patch.org_id is not None
        )

        async with self._pool.acquire() as conn:
            existing = await self._repo.get(conn, provider_id)
            if existing is None:
                return None

            tx = conn.transaction()
            await tx.start()
            updated_row: LLMProviderRow | None = None
            merged_credentials: dict[str, str] | None = None
            try:
                updated_row = await self._repo.update(
                    conn, provider_id, patch
                )
                if credentials_changed:
                    # Merge the patch over the existing Vault payload
                    # so an operator can rotate the api_key without
                    # losing the org_id (and vice versa).
                    current = await self._vault.read_llm_credentials(
                        provider_id=provider_id
                    )
                    merged_credentials = dict(current)
                    if patch.api_key is not None:
                        merged_credentials["api_key"] = patch.api_key
                    if patch.org_id is not None:
                        # An empty string clears the optional org_id
                        # (the operator removed it through the form).
                        if patch.org_id:
                            merged_credentials["org_id"] = patch.org_id
                        else:
                            merged_credentials.pop("org_id", None)
                    await self._vault.write_llm_credentials(
                        provider_id=provider_id,
                        payload=merged_credentials,
                    )
            except VaultWriteError as exc:
                await self._safe_rollback(tx, provider_id)
                raise VaultWriteFailed(provider_id, exc) from exc
            except Exception:
                await self._safe_rollback(tx, provider_id)
                raise
            else:
                await tx.commit()

        if updated_row is None:
            return None

        dto = await self._row_to_dto(
            updated_row,
            credentials=merged_credentials,
            fetch_credentials=merged_credentials is None,
        )

        await self._emit_audit(
            action=(
                "llm_provider_credentials_rotated"
                if credentials_changed
                else "llm_provider_updated"
            ),
            actor_id=actor_id,
            resource=f"llm_provider:{provider_id}",
            result="ok",
            payload={
                "provider_id": str(provider_id),
                "updated_fields": _patch_field_names(patch),
            },
        )
        return dto

    async def delete(
        self, provider_id: UUID, *, actor_id: str
    ) -> bool:
        """Delete a provider after enforcing referential safety.

        Returns ``False`` when no row exists for *provider_id*; the
        router maps this to HTTP 404.  Otherwise:

        * If any dept pins the provider, raises :class:`ProviderInUse`
          BEFORE touching Vault (R1.7).
        * Calls :meth:`VaultClient.delete_llm_credentials`; on failure
          raises :class:`VaultDeleteFailed`, leaves the row in place,
          and emits ``llm_provider_delete_vault_failed`` audit (R3.7).
        * Only on a successful Vault delete does the row get removed
          and a ``llm_provider_deleted`` audit event emitted.
        """

        async with self._pool.acquire() as conn:
            existing = await self._repo.get(conn, provider_id)
            if existing is None:
                return False
            dept_ids = await self._repo.overrides_referencing(conn, provider_id)
            if dept_ids:
                raise ProviderInUse(provider_id, dept_ids)

        # Vault delete BEFORE the Postgres delete so the row survives
        # a Vault outage and the operator can retry.
        try:
            await self._vault.delete_llm_credentials(provider_id=provider_id)
        except VaultWriteError as exc:
            await self._emit_audit(
                action="llm_provider_delete_vault_failed",
                actor_id=actor_id,
                resource=f"llm_provider:{provider_id}",
                result="error",
                payload={
                    "provider_id": str(provider_id),
                    "vault_status_code": exc.status_code,
                },
            )
            raise VaultDeleteFailed(provider_id, exc) from exc

        async with self._pool.acquire() as conn:
            await self._repo.delete(conn, provider_id)

        await self._emit_audit(
            action="llm_provider_deleted",
            actor_id=actor_id,
            resource=f"llm_provider:{provider_id}",
            result="ok",
            payload={"provider_id": str(provider_id)},
        )
        return True

    # -----------------------------------------------------------------
    # Connection test flows
    # -----------------------------------------------------------------

    async def test_saved(
        self, provider_id: UUID, *, actor_id: str
    ) -> ConnectionTestResult:
        """Run a connection test against a persisted provider.

        Reads credentials from Vault at request time (no caching) so a
        recent rotation takes effect immediately; persists
        ``last_tested_at`` + ``last_test_error`` via
        :meth:`LLMProviderRepository.update_test_result`; emits
        ``llm_provider_tested`` audit event.
        """

        async with self._pool.acquire() as conn:
            row = await self._repo.get(conn, provider_id)
        if row is None:
            return ConnectionTestResult(
                success=False,
                latency_ms=0,
                model=None,
                error=ConnectionTestError(
                    status_code=None,
                    message=f"provider {provider_id} not found",
                ),
            )

        credentials = await self._vault.read_llm_credentials(
            provider_id=provider_id
        )
        req = TestRequest(
            provider_type=row.provider_type,  # type: ignore[arg-type]
            model=row.model,
            base_url=row.base_url,
            api_key=credentials.get("api_key"),
            org_id=credentials.get("org_id"),
            reasoning_effort=row.reasoning_effort,
            verbosity=row.verbosity,
            provider_id=provider_id,
        )
        result = await self._tester.run(req)
        sanitised = _sanitise_result(result)

        async with self._pool.acquire() as conn:
            await self._repo.update_test_result(
                conn,
                provider_id,
                last_tested_at=self._clock(),
                last_test_error=(
                    sanitised.error.message if sanitised.error else None
                ),
            )

        await self._emit_audit(
            action="llm_provider_tested",
            actor_id=actor_id,
            resource=f"llm_provider:{provider_id}",
            result="ok" if sanitised.success else "error",
            payload={
                "provider_id": str(provider_id),
                "provider_type": row.provider_type,
                "success": sanitised.success,
                "latency_ms": sanitised.latency_ms,
                "error_message": (
                    sanitised.error.message if sanitised.error else None
                ),
            },
        )
        return sanitised

    async def test_unsaved(
        self, payload: UnsavedTestRequest, *, actor_id: str
    ) -> ConnectionTestResult:
        """Run a connection test against an unsaved provider config.

        The handler does not touch Postgres or Vault — the caller's
        credentials are used directly. Used by the UI's "Test
        Connection" button before the operator commits the form.
        """

        req = TestRequest(
            provider_type=payload.provider_type,
            model=payload.model,
            base_url=(
                str(payload.base_url) if payload.base_url is not None else None
            ),
            api_key=payload.api_key,
            org_id=payload.org_id,
            reasoning_effort=payload.reasoning_effort,
            verbosity=payload.verbosity,
            provider_id=None,
        )
        result = await self._tester.run(req)
        sanitised = _sanitise_result(result)

        await self._emit_audit(
            action="llm_provider_test_unsaved",
            actor_id=actor_id,
            resource="llm_provider:unsaved",
            result="ok" if sanitised.success else "error",
            payload={
                "provider_type": payload.provider_type,
                "model": payload.model,
                "success": sanitised.success,
                "latency_ms": sanitised.latency_ms,
                "error_message": (
                    sanitised.error.message if sanitised.error else None
                ),
            },
        )
        return sanitised

    # -----------------------------------------------------------------
    # Department override flows
    # -----------------------------------------------------------------

    async def get_override(self, dept_id: str) -> DeptOverrideDTO:
        """Return the per-dept override (or the documented null shape).

        Never returns ``None`` — when the dept has no pin we surface
        ``DeptOverrideDTO(dept_id=..., provider=None)`` so the UI
        can render a clean "no override" panel.
        """

        async with self._pool.acquire() as conn:
            override = await self._override_repo.get(conn, dept_id)
            if override is None:
                return DeptOverrideDTO(dept_id=dept_id, provider=None)
            row = await self._repo.get(conn, override.provider_id)
        if row is None:
            return DeptOverrideDTO(dept_id=dept_id, provider=None)
        dto = await self._row_to_dto(row, fetch_credentials=True)
        return DeptOverrideDTO(dept_id=dept_id, provider=dto)

    async def set_override(
        self,
        dept_id: str,
        provider_id: UUID | None,
        *,
        actor_id: str,
    ) -> DeptOverrideDTO:
        """Pin / unpin a department's LLM provider.

        Passing ``provider_id=None`` deletes the override row (no-op
        if absent). Passing a UUID upserts the override, after
        validating that the target provider exists (raise
        :class:`ProviderNotFound`) and is active (raise
        :class:`ProviderInactive`).
        """

        if provider_id is None:
            async with self._pool.acquire() as conn:
                await self._override_repo.delete(conn, dept_id)
            await self._emit_audit(
                action="dept_llm_provider_unpinned",
                actor_id=actor_id,
                dept_id=dept_id,
                resource=f"dept:{dept_id}",
                result="ok",
                payload={"actor_user_id": actor_id, "dept_id": dept_id},
            )
            return DeptOverrideDTO(dept_id=dept_id, provider=None)

        async with self._pool.acquire() as conn:
            row = await self._repo.get(conn, provider_id)
            if row is None:
                raise ProviderNotFound(provider_id)
            if row.status != "active":
                raise ProviderInactive(provider_id)
            await self._override_repo.upsert(conn, dept_id, provider_id)
        dto = await self._row_to_dto(row, fetch_credentials=True)

        await self._emit_audit(
            action="dept_llm_provider_pinned",
            actor_id=actor_id,
            dept_id=dept_id,
            resource=f"dept:{dept_id}",
            result="ok",
            payload={
                "actor_user_id": actor_id,
                "dept_id": dept_id,
                "provider_id": str(provider_id),
            },
        )
        return DeptOverrideDTO(dept_id=dept_id, provider=dto)

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    async def _row_to_dto(
        self,
        row: LLMProviderRow,
        *,
        credentials: dict[str, str] | None = None,
        fetch_credentials: bool = False,
    ) -> LLMProviderConfigDTO:
        """Project a row + credential payload into the masked DTO."""

        payload = credentials
        if payload is None and fetch_credentials:
            try:
                payload = await self._vault.read_llm_credentials(
                    provider_id=row.id
                )
            except VaultWriteError as exc:
                # Surface the row even when the Vault payload is
                # unreadable; the masked credentials collapse to "…"
                # and the operator can investigate via the per-row
                # last_test_error / audit log.
                _LOG.warning(
                    "llm_provider_vault_read_failed provider_id=%s status=%s",
                    row.id,
                    exc.status_code,
                )
                payload = {}
        payload = payload or {}
        return LLMProviderConfigDTO(
            id=row.id,
            provider_type=row.provider_type,  # type: ignore[arg-type]
            name=row.name,
            model=row.model,
            context_length=row.context_length,
            base_url=row.base_url,
            status=row.status,  # type: ignore[arg-type]
            reasoning_effort=row.reasoning_effort,  # type: ignore[arg-type]
            verbosity=row.verbosity,  # type: ignore[arg-type]
            api_key_masked=mask(payload.get("api_key")),
            org_id_masked=(
                mask(payload["org_id"]) if "org_id" in payload else None
            ),
            last_tested_at=row.last_tested_at,
            last_test_error=row.last_test_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def _safe_rollback(self, tx: Any, provider_id: UUID) -> None:
        """Roll back *tx*; log + swallow failures so 502 still surfaces."""

        try:
            await tx.rollback()
        except Exception as exc:  # noqa: BLE001 - rollback path
            _LOG.error(
                "llm_provider_rollback_failed provider_id=%s exception=%s",
                provider_id,
                exc.__class__.__name__,
            )

    async def _emit_audit(
        self,
        *,
        action: str,
        actor_id: str,
        resource: str,
        result: str,
        payload: dict[str, Any],
        dept_id: str | None = None,
    ) -> None:
        """Best-effort audit emission — sink failures never escape (R12.7)."""

        event = AuditEvent(
            actor_id=actor_id,
            actor_role="admin",
            dept_id=dept_id if dept_id is not None else DUMMY_AUDIT_DEPT,
            action=action,
            resource=resource,
            result=result,  # type: ignore[arg-type]
            timestamp=self._clock(),
            payload=payload,
        )
        try:
            await self._audit.write(event)
        except Exception as exc:  # noqa: BLE001 - sink swallow per R12.7
            _LOG.warning(
                "llm_provider_audit_emit_failed action=%s err=%s",
                action,
                exc.__class__.__name__,
            )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _vault_payload_for_create(payload: _ProviderCreateInput) -> dict[str, str]:
    """Build the credential dict that gets written to Vault on create.

    The payload shape mirrors the design's "Vault path layout" table:
    vLLM may omit ``api_key`` entirely; OpenAI carries optional
    ``org_id``; Anthropic / Gemini are bare ``{api_key}``.
    """

    out: dict[str, str] = {}
    if payload.api_key:
        out["api_key"] = payload.api_key
    if payload.org_id:
        out["org_id"] = payload.org_id
    return out


def _patch_field_names(patch: ProviderUpdate) -> list[str]:
    """Return the list of fields the operator actually set on *patch*."""

    return [
        name
        for name in (
            "name",
            "model",
            "context_length",
            "base_url",
            "api_key",
            "org_id",
            "status",
            "reasoning_effort",
            "verbosity",
        )
        if getattr(patch, name) is not None
    ]


def _sanitise_result(result: ConnectionTestResult) -> ConnectionTestResult:
    """Apply :func:`redact_text` to the error message before persistence."""

    if result.error is None:
        return result
    redacted = redact_text(result.error.message)
    if redacted == result.error.message:
        return result
    return ConnectionTestResult(
        success=result.success,
        latency_ms=result.latency_ms,
        model=result.model,
        error=ConnectionTestError(
            status_code=result.error.status_code,
            message=redacted,
        ),
    )
