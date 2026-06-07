"""Atomic per-service department credential orchestrator.

This module owns the orchestration helper used by the
``/admin/departments/{id}/credentials/{service}`` and
``/admin/departments/{id}/probe`` endpoints.  Unlike
``DepartmentCreateOrchestrator`` (which provisions a *new* department
with all of its bots in a single shot), this service mutates a
**single** ``(dept_id, service)`` pair on an *existing* department.

Atomic flow (mirrors ``design.md`` §"Komponent Etkileşim Sırası -
Atomic Dept Credential Add"):

    1. Write the new credential to the staging Vault path
       ``vault:atlassian/_staging/<request_id>/<service>``.
    2. Run :meth:`ProbeRunner.run(scope="org", dept_id, service, ...)`
       against the staged credential.  A failure here short-circuits
       the flow with staging cleanup + ``dept_credential_add_failed``
       audit + raised :class:`DeptCredentialOperationError`.
    3. Inside a single ``with_dept_session`` transaction, UPSERT the
       ``automation.department_bots`` row with ``credential_ref``
       pointing at the *final* path
       ``vault:atlassian/<dept_id>/<service>``.  Once the SQL succeeds,
       move the staged secret to the final path (read → write → delete
       staging).  Any failure inside the block triggers Vault rollback
       (final path delete + staging delete) and a SQL ROLLBACK via
       ``with_dept_session``.

The helper is deliberately HTTP-framework agnostic: a thin FastAPI
router parses the request, builds an
:class:`AddCredentialRequest`, dispatches to :meth:`add_or_update`,
and translates the resulting :class:`DeptCredentialOperationError`
into HTTP status codes.

Staging pattern reuse
---------------------

This service does **not** duplicate the staging / probe / promote
machinery from :mod:`automation_service.admin.dept_create`.  Instead
it imports and calls:

* :func:`automation_service.staging.staging_vault_path`
* :func:`automation_service.staging.final_vault_path`
* :func:`automation_service.staging.build_credential_payload`
* :func:`automation_service.staging.scrub_plain_text_token`
* :func:`automation_service.staging.validate_dept_id`
* :class:`automation_service.probe.ProbeRunner`

The duplicate-row safety, plain-text-token hygiene, and probe artifact
cleanup all reuse the same primitives already covered by the shared
suite.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Sequence,
)
from urllib.parse import urlparse

from audit_logger import AuditEvent, AuditLogger
from db_shared import AsyncConnection, with_dept_session
from vault_client import VaultClient, VaultPath

from automation_service.probe import (
    AtlassianProbeClient,
    BotIdentityProbeResult,
    ProbeResult,
    ProbeRunner,
    ProbeService,
    ProbeTargets,
    ResolvedCredential,
    probe_bot_identity,
)
from automation_service.staging import (
    VALID_SERVICES,
    build_credential_payload,
    final_vault_path,
    scrub_plain_text_token,
    staging_vault_path,
    validate_dept_id,
)

__all__ = [
    "AddCredentialRequest",
    "AddCredentialResult",
    "DeptCredentialOperationError",
    "DeptCredentialService",
    "DepartmentNotFoundError",
    "ProbeOutcome",
    "ProbeRunOutcome",
    "RemoveCredentialResult",
]

_LOG = logging.getLogger(__name__)

# A connection factory - the orchestrator never owns pool lifecycle; the
# caller (FastAPI router or test harness) hands it a callable that
# returns a fresh asyncpg-shaped :class:`AsyncConnection` per call.
ConnectionFactory = Callable[[], Awaitable[AsyncConnection]]


# ---------------------------------------------------------------------------
# Errors surfaced to the caller (the router maps these to HTTP statuses)
# ---------------------------------------------------------------------------


class DepartmentNotFoundError(LookupError):
    """Raised when ``automation.departments`` has no row for *dept_id*.

    This service only mutates *existing* departments; create flow is
    owned by ``DepartmentCreateOrchestrator``.  The router maps this
    to HTTP 404.
    """

    def __init__(self, dept_id: str) -> None:
        super().__init__(f"department with id={dept_id!r} not found")
        self.dept_id = dept_id


class DeptCredentialOperationError(RuntimeError):
    """Raised when the atomic add/update/remove flow could not complete.

    Carries the reason marker recorded in the
    ``dept_credential_add_failed`` audit row so callers can format a
    deterministic 502 body.

    Attributes:
        reason: Stable failure marker (eg. ``"staging_write_failed"``,
            ``"probe_failed:read_failed"``,
            ``"db_or_vault_error:UniqueViolationError"``).
        service: The Atlassian service the operation was acting on.
        detail: Best-effort human-readable detail; sanitised so it
            never carries credential material.
    """

    def __init__(
        self,
        *,
        reason: str,
        service: ProbeService,
        detail: str | None = None,
    ) -> None:
        super().__init__(
            f"dept credential operation failed for service={service!r}: "
            f"reason={reason}"
        )
        self.reason = reason
        self.service = service
        self.detail = detail


# ---------------------------------------------------------------------------
# Request / result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AddCredentialRequest:
    """Validated input for :meth:`DeptCredentialService.add_or_update`.

    Attributes:
        dept_id: Existing department id; must already exist in
            ``automation.departments``.
        service: Atlassian surface the credential authenticates
            against - one of :data:`VALID_SERVICES`.
        url: Atlassian site / Bitbucket workspace URL.
        username: Email or login the bot authenticates as.
        personal_token: Plain-text API token; **must** be supplied as
            a :class:`bytearray` so the orchestrator can zero it on
            the heap once the value is in Vault.
        account_id: Optional pre-known ``accountId``.  When ``None``
            the probe runner's auto-fetch fills it in.
        deployment: Bitbucket deployment kind.  Only meaningful when
            ``service == "bitbucket"``; ignored for the others.
        probe_targets: Optional Bitbucket workspace / repo +
            Confluence space metadata required by the runner for the
            non-Jira write probes.  Defaults to a derivation pulled
            from ``automation.repo_mappings`` /
            ``automation.department_space_keys`` when the caller does
            not supply an override.
    """

    dept_id: str
    service: ProbeService
    url: str
    username: str
    personal_token: bytearray
    account_id: str | None = None
    deployment: Literal["cloud", "server", "dc"] | None = None
    probe_targets: ProbeTargets | None = None


@dataclass(frozen=True, slots=True)
class AddCredentialResult:
    """Successful add/update response (router serialises this to JSON).

    Attributes:
        dept_id: The department whose row was upserted.
        service: The Atlassian service the credential covers.
        account_id: Resolved ``accountId``.  Equal to the request's
            ``account_id`` when supplied, otherwise the probe-runner's
            auto-fetched value.  When the inline bot identity probe
            succeeds, this is updated to the freshly probed value.
        last_probe_at: UTC timestamp the read+write probe completed.
        vault_path: The *final* (post-promotion) Vault path stored on
            the ``credential_ref`` column.  Returned as a plain string
            (mask edilmiş - never the secret value).
        outcome: ``"created"`` for first-time inserts,
            ``"updated"`` when the ``(dept_id, service)`` row already
            existed and we replaced its ``credential_ref``.
        account_id_probe_status: Status of the inline bot identity
            probe.  ``"ok"`` when the probe resolved an
            account_id, ``"failed"`` when the probe could not resolve
            it, ``None`` when the probe was not attempted (should not
            happen in normal flow).
        account_id_probe_error: Human-readable error description when
            ``account_id_probe_status == "failed"``.  ``None`` on
            success.
    """

    dept_id: str
    service: ProbeService
    account_id: str | None
    last_probe_at: datetime
    vault_path: str
    outcome: Literal["created", "updated"]
    account_id_probe_status: Literal["ok", "failed"] | None = None
    account_id_probe_error: str | None = None


@dataclass(frozen=True, slots=True)
class RemoveCredentialResult:
    """Successful remove response.

    Attributes:
        dept_id: The department whose row was deleted.
        service: The Atlassian service the credential covered.
        existed: ``True`` when a row was actually deleted, ``False``
            when no matching row was present (idempotent semantics -
            the router still returns 200).
    """

    dept_id: str
    service: ProbeService
    existed: bool


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Per-service probe outcome carried inside :class:`ProbeRunOutcome`."""

    service: ProbeService
    status: Literal["ok", "failed"]
    error: str | None = None
    account_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeRunOutcome:
    """Result of :meth:`DeptCredentialService.probe`.

    Mirrors the response shape documented in
    ``design.md`` §"Endpoint sözleşmesi" - a list of per-service
    results so the same payload covers both single-service and
    "all-services" probe calls.
    """

    dept_id: str
    results: tuple[ProbeOutcome, ...]
    probed_at: datetime


# ---------------------------------------------------------------------------
# Optional protocols for collaborators we do not own
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeptBotRow:
    """Minimal projection of ``automation.department_bots`` we care about."""

    department_id: str
    service: str
    credential_ref: str
    account_id: str | None
    username: str | None
    deployment: str


def _first_payload_str(
    payload: Mapping[str, str],
    *keys: str,
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_bitbucket_url(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def _probe_targets_from_payload(
    bot: _DeptBotRow,
    payload: Mapping[str, str],
) -> ProbeTargets | None:
    url = _first_payload_str(payload, "url") or ""
    workspace = _first_payload_str(payload, "bitbucket_workspace", "workspace")
    repo = _first_payload_str(payload, "bitbucket_repo", "repo", "repo_slug")
    if bot.service == "bitbucket" and (not workspace or not repo):
        parsed_workspace, parsed_repo = _parse_bitbucket_url(url)
        workspace = workspace or parsed_workspace
        repo = repo or parsed_repo

    space_key = _first_payload_str(payload, "confluence_space_key", "space_key")
    if bot.service == "confluence" and not space_key:
        space_key = "__auto__"

    if not any((workspace, repo, space_key)):
        return None
    return ProbeTargets(
        bitbucket_workspace=workspace,
        bitbucket_repo=repo,
        confluence_space_key=space_key,
    )


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class DeptCredentialService:
    """Atomic credential CRUD for an *existing* department.

    Args:
        vault: :class:`VaultClient` - same backend the foundation
            ``DepartmentCreateOrchestrator`` uses, so dev-mode
            encrypted-at-rest semantics carry through unchanged.
        connection_factory: Async factory returning a fresh
            :class:`AsyncConnection` per call.  The orchestrator opens
            **one** ``with_dept_session`` block per mutation.
        probe_client: Atlassian probe client (production = MCP-routed
            wrapper; tests inject a fake satisfying
            :class:`AtlassianProbeClient`).
        audit_logger: Required.  Every mutation writes at least one
            audit row; failure paths add ``dept_credential_add_failed``.
        clock: UTC-now factory; defaults to :func:`datetime.now(UTC)`.
            Overridable so unit tests get deterministic
            ``last_probe_at`` timestamps.
        actor_role_for_session: ``actor_role`` used when opening the
            ``with_dept_session`` transaction.  Defaults to
            ``"system"`` so the call works under the policy's global
            bypass; the router can pass ``"admin"`` instead so the RLS
            audit trail records the human admin's role.

    The instance is stateless across calls - every public method
    builds its own staging path / probe runner / audit row.
    """

    def __init__(
        self,
        *,
        vault: VaultClient,
        connection_factory: ConnectionFactory,
        probe_client: AtlassianProbeClient,
        audit_logger: AuditLogger,
        clock: Callable[[], datetime] | None = None,
        actor_role_for_session: Literal["admin", "system"] = "system",
    ) -> None:
        self._vault = vault
        self._connection_factory = connection_factory
        self._probe_client = probe_client
        self._audit = audit_logger
        self._clock = (
            clock if clock is not None else (lambda: datetime.now(timezone.utc))
        )
        self._actor_role_for_session: Literal["admin", "system"] = (
            actor_role_for_session
        )

    # ==================================================================
    # add_or_update
    # ==================================================================

    async def add_or_update(
        self,
        request: AddCredentialRequest,
        *,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
    ) -> AddCredentialResult:
        """Run the atomic add/update flow for a single ``(dept_id, service)``.

        See module docstring for the full sequence.  This method is
        the canonical entry point invoked by the
        ``POST /admin/departments/{id}/credentials/{service}`` router.

        Args:
            request: Validated :class:`AddCredentialRequest`.
            actor_id: OIDC ``sub`` of the human admin (or the bot
                ``account_id`` for ``actor_role == "system"``).
            actor_role: RBAC role of the caller - recorded on every
                audit row so the actor role is traceable end-to-end.

        Returns:
            :class:`AddCredentialResult` on success.

        Raises:
            DepartmentNotFoundError: dept_id does not exist.
            DeptCredentialOperationError: Any failure in staging /
                probe / DB / promotion.  All staging keys have been
                cleaned up by the time this is raised.
            ValueError: Request fails the cheap structural validation
                (eg. unknown service, empty token).
        """

        self._validate_request(request)
        dept_id = validate_dept_id(request.dept_id)
        service = request.service

        request_id = uuid.uuid4().hex
        staging = staging_vault_path(request_id, service)
        final = final_vault_path(dept_id, service)

        _LOG.info(
            "dept_credential.add_or_update.start dept_id=%s service=%s "
            "request_id=%s",
            dept_id,
            service,
            request_id,
        )

        # --- Step 1 - staging write -----------------------------------
        try:
            self._write_staging_credential(request, staging)
        except Exception as exc:  # noqa: BLE001
            self._best_effort_delete_staging((staging,))
            await self._audit_add_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason="staging_write_failed",
            )
            raise DeptCredentialOperationError(
                reason="staging_write_failed",
                service=service,
                detail=type(exc).__name__,
            ) from exc

        # --- Step 2 - probe -------------------------------------------
        try:
            probe_result = await self._run_probe(
                dept_id=dept_id,
                service=service,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001 - probe runner already
            # surfaces failures via ``state``; an exception here is a
            # client-protocol breakage, not a credential rejection.
            self._best_effort_delete_staging((staging,))
            self._zero_request_token(request)
            await self._audit_add_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=f"probe_runner_error:{type(exc).__name__}",
            )
            raise DeptCredentialOperationError(
                reason=f"probe_runner_error:{type(exc).__name__}",
                service=service,
                detail=str(exc) or None,
            ) from exc

        if probe_result.state != "ok":
            self._best_effort_delete_staging((staging,))
            self._zero_request_token(request)
            reason = f"probe_failed:{probe_result.state}"
            await self._audit_add_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=reason,
            )
            raise DeptCredentialOperationError(
                reason=reason,
                service=service,
                detail=probe_result.error_message,
            )

        # The probe phase is the last consumer of the plain-text
        # token - wipe the bytearray before the DB transaction begins.
        # Vault retains the encrypted-at-rest staging copy.
        self._zero_request_token(request)

        resolved_account_id = (
            request.account_id or probe_result.auto_fetched_account_id
        )

        # --- Step 3 - DB upsert + Vault staging→final move ------------
        try:
            outcome = await self._commit(
                dept_id=dept_id,
                service=service,
                request=request,
                staging=staging,
                final=final,
                resolved_account_id=resolved_account_id,
            )
        except DepartmentNotFoundError:
            self._best_effort_delete_staging((staging,))
            await self._audit_add_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason="department_not_found",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised below
            self._best_effort_delete_staging((staging,))
            await self._audit_add_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=f"db_or_vault_error:{type(exc).__name__}",
            )
            _LOG.error(
                "dept_credential.add_or_update.failed dept_id=%s service=%s "
                "request_id=%s err=%s",
                dept_id,
                service,
                request_id,
                type(exc).__name__,
            )
            raise DeptCredentialOperationError(
                reason=f"db_or_vault_error:{type(exc).__name__}",
                service=service,
                detail=str(exc) or None,
            ) from exc

        # --- Step 4 - success audit -----------------------------------
        action: Literal["dept_credential_added", "dept_credential_updated"] = (
            "dept_credential_added" if outcome == "created" else "dept_credential_updated"
        )
        last_probe_at = self._clock()
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=dept_id,
                action=action,
                resource=f"dept_credential:{dept_id}:{service}",
                result="ok",
                timestamp=last_probe_at,
                payload={
                    "request_id": request_id,
                    "service": service,
                    "vault_path": final.raw,
                    "account_id": resolved_account_id,
                },
            )
        )

        # --- Step 5 - inline bot identity probe -----------------------
        # After Vault write + DB upsert succeed, probe the bot's
        # account_id via Atlassian /myself (Jira/Confluence) or /user
        # (Bitbucket).  This is best-effort: failure does not roll
        # back the credential write - the response carries
        # account_id_probe_status so the caller knows the outcome.
        identity_probe = await self._run_bot_identity_probe(
            dept_id=dept_id,
            service=service,
            request=request,
            actor_id=actor_id,
            actor_role=actor_role,
        )

        # If the identity probe succeeded, update the resolved
        # account_id to the freshly probed value (it may differ from
        # the one the credential probe auto-fetched if the request
        # carried a stale manual value).
        final_account_id = resolved_account_id
        if identity_probe.success and identity_probe.account_id:
            final_account_id = identity_probe.account_id

        _LOG.info(
            "dept_credential.add_or_update.ok dept_id=%s service=%s "
            "request_id=%s outcome=%s probe_status=%s",
            dept_id,
            service,
            request_id,
            outcome,
            "ok" if identity_probe.success else "failed",
        )

        return AddCredentialResult(
            dept_id=dept_id,
            service=service,
            account_id=final_account_id,
            last_probe_at=last_probe_at,
            vault_path=final.raw,
            outcome=outcome,
            account_id_probe_status="ok" if identity_probe.success else "failed",
            account_id_probe_error=identity_probe.error,
        )

    # ==================================================================
    # remove
    # ==================================================================

    async def remove(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
    ) -> RemoveCredentialResult:
        """Delete the ``(dept_id, service)`` row + Vault final path.

        Idempotent: returning ``existed=False`` is acceptable when the
        caller is replaying a previous DELETE.  The Vault delete is
        always attempted; per the :class:`VaultClient` contract a
        missing key is a no-op.

        Audit:
            * ``dept_credential_removed`` on success (``existed=True``
              or ``existed=False``).
            * ``dept_credential_add_failed`` (``reason="remove_failed:..."``)
              on unexpected DB or Vault errors so the failure-path
              audit shape stays uniform across all mutations.

        Args:
            dept_id: Department whose bot row should be deleted.
            service: Atlassian surface to remove.
            actor_id: OIDC ``sub`` / bot account_id of the caller.
            actor_role: RBAC role of the caller.

        Returns:
            :class:`RemoveCredentialResult`.

        Raises:
            DepartmentNotFoundError: dept_id does not exist.  We do
                **not** silently no-op here - a router-level RBAC
                check needs the department to exist before it can
                authorise the call, so reaching this code path with
                an unknown dept_id is a contract violation worth
                surfacing as 404.
            DeptCredentialOperationError: Unexpected DB or Vault
                error.  The Vault final path may or may not have
                been deleted; the audit row records the failure so
                an admin can re-run the call.
        """

        validated = validate_dept_id(dept_id)
        if service not in VALID_SERVICES:
            raise ValueError(
                f"service must be one of {sorted(VALID_SERVICES)!r}; "
                f"got {service!r}"
            )

        final = final_vault_path(validated, service)

        try:
            existed = await self._delete_bot_row(validated, service)
        except DepartmentNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._audit_add_failed(
                dept_id=validated,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=f"remove_failed:{type(exc).__name__}",
            )
            raise DeptCredentialOperationError(
                reason=f"remove_failed:{type(exc).__name__}",
                service=service,
                detail=str(exc) or None,
            ) from exc

        # Vault delete is best-effort idempotent - see the
        # :class:`VaultClient` protocol contract.  Failure here is
        # logged + audited but does not block the SQL delete which
        # already committed.
        try:
            self._vault.delete(final)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "dept_credential.remove.vault_delete_failed dept_id=%s "
                "service=%s path=%s err=%s",
                validated,
                service,
                final.raw,
                type(exc).__name__,
            )
            await self._audit_add_failed(
                dept_id=validated,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                reason=f"remove_vault_delete_failed:{type(exc).__name__}",
            )
            # The DB row is already gone; surface the partial failure
            # so an admin can re-run the cleanup endpoint.
            raise DeptCredentialOperationError(
                reason=f"remove_vault_delete_failed:{type(exc).__name__}",
                service=service,
                detail=str(exc) or None,
            ) from exc

        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=validated,
                action="dept_credential_removed",
                resource=f"dept_credential:{validated}:{service}",
                result="ok",
                timestamp=self._clock(),
                payload={
                    "service": service,
                    "vault_path": final.raw,
                    "existed": existed,
                },
            )
        )

        return RemoveCredentialResult(
            dept_id=validated,
            service=service,
            existed=existed,
        )

    # ==================================================================
    # probe
    # ==================================================================

    async def probe(
        self,
        *,
        dept_id: str,
        service: ProbeService | None = None,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
    ) -> ProbeRunOutcome:
        """Re-run connectivity probes for one or all services on *dept_id*.

        ``service=None`` probes every registered service for the
        department; otherwise only the specified service is probed
        (404 surface lives in the router - this method silently
        returns an empty result list when *service* is set but no row
        exists).

        Each per-service result writes a single
        ``dept_credential_probed`` audit row carrying ``status`` and
        an optional sanitised ``error`` summary.

        Args:
            dept_id: Existing department.
            service: Optional single service filter.
            actor_id: Caller's OIDC ``sub`` / bot account.
            actor_role: Caller's RBAC role.

        Returns:
            :class:`ProbeRunOutcome` with one :class:`ProbeOutcome`
            per probed service.

        Raises:
            DepartmentNotFoundError: dept_id does not exist.
        """

        validated = validate_dept_id(dept_id)

        bots = await self._list_bots(validated)
        if service is not None:
            if service not in VALID_SERVICES:
                raise ValueError(
                    f"service must be one of {sorted(VALID_SERVICES)!r}; "
                    f"got {service!r}"
                )
            bots = tuple(b for b in bots if b.service == service)

        results: list[ProbeOutcome] = []
        probed_at = self._clock()

        for bot in bots:
            try:
                payload = self._read_credential_payload(bot)
                cred = self._resolved_credential_from_payload(bot, payload)
                targets = _probe_targets_from_payload(bot, payload)
                probe_result = await ProbeRunner(
                    self._probe_client,
                    clock=lambda: int(self._clock().timestamp()),
                ).run(validated, bot.service, cred, targets=targets)
            except Exception as exc:  # noqa: BLE001
                outcome = ProbeOutcome(
                    service=bot.service,  # type: ignore[arg-type]
                    status="failed",
                    error=f"probe_runner_error:{type(exc).__name__}",
                )
            else:
                if probe_result.state == "ok":
                    outcome = ProbeOutcome(
                        service=bot.service,  # type: ignore[arg-type]
                        status="ok",
                        account_id=probe_result.auto_fetched_account_id
                        or bot.account_id,
                    )
                else:
                    outcome = ProbeOutcome(
                        service=bot.service,  # type: ignore[arg-type]
                        status="failed",
                        error=(
                            probe_result.error_message
                            or f"probe_state:{probe_result.state}"
                        ),
                    )

            results.append(outcome)

            await self._audit.write(
                AuditEvent(
                    actor_id=actor_id,
                    actor_role=actor_role,
                    dept_id=validated,
                    action="dept_credential_probed",
                    resource=f"dept_credential:{validated}:{bot.service}",
                    result="ok" if outcome.status == "ok" else "error",
                    timestamp=probed_at,
                    payload={
                        "service": bot.service,
                        "status": outcome.status,
                        "error": outcome.error,
                        "account_id": outcome.account_id,
                    },
                )
            )

        return ProbeRunOutcome(
            dept_id=validated,
            results=tuple(results),
            probed_at=probed_at,
        )

    def _read_credential_payload(self, bot: _DeptBotRow) -> Mapping[str, str]:
        """Read the bot's final Vault credential payload."""

        raw_ref = (bot.credential_ref or "").strip()
        path = (
            VaultPath.parse(raw_ref)
            if raw_ref
            else final_vault_path(bot.department_id, bot.service)
        )
        return self._vault.read(path)

    @staticmethod
    def _resolved_credential_from_payload(
        bot: _DeptBotRow,
        payload: Mapping[str, str],
    ) -> ResolvedCredential:
        """Build a probe credential from Vault without exposing tokens."""

        url = _first_payload_str(payload, "url")
        username = _first_payload_str(payload, "username", "email") or bot.username
        token = _first_payload_str(
            payload,
            "personal_token",
            "api_token",
            "token",
            "app_password",
        )
        if not url or not username or not token:
            raise ValueError("credential payload is incomplete")
        return ResolvedCredential(
            url=url,
            username=username,
            personal_token=token,
        )

    # ==================================================================
    # Internals
    # ==================================================================

    @staticmethod
    def _validate_request(request: AddCredentialRequest) -> None:
        """Cheap structural pre-checks.

        The router runs schema validation first; this guard makes the
        method safe to call directly from tests / CLI tools.
        """

        if request.service not in VALID_SERVICES:
            raise ValueError(
                f"service must be one of {sorted(VALID_SERVICES)!r}; "
                f"got {request.service!r}"
            )
        if not isinstance(request.personal_token, bytearray):
            raise TypeError(
                "AddCredentialRequest.personal_token must be a bytearray "
                "so the orchestrator can zero it after the staging write"
            )
        if not request.personal_token:
            raise ValueError(
                "AddCredentialRequest.personal_token must be non-empty"
            )
        if not request.url or not request.username:
            raise ValueError(
                "AddCredentialRequest requires non-empty url and username"
            )

    # ------------------------------------------------------------------
    # Vault staging write
    # ------------------------------------------------------------------

    def _write_staging_credential(
        self,
        request: AddCredentialRequest,
        staging: VaultPath,
    ) -> None:
        """Write the staged credential to *staging*.

        The Vault payload mirrors the foundation's
        :func:`automation_service.staging.build_credential_payload`
        plus the optional ``url`` / ``deployment`` fields the
        production probe client expects.
        """

        try:
            token_str = bytes(request.personal_token).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"personal_token for service={request.service!r} is not "
                "valid utf-8"
            ) from exc

        payload = dict(
            build_credential_payload(
                username=request.username,
                personal_token=token_str,
                account_id=request.account_id,
            )
        )
        # ``build_credential_payload`` does not carry the URL or
        # deployment kind - the probe runner / DB resolver need them
        # so we add them here.
        payload["url"] = request.url
        if request.deployment is not None:
            # Map the schema-level "server" to the DB-level "dc" so
            # the same value can roundtrip through both surfaces.
            payload["deployment"] = (
                "dc" if request.deployment == "server" else request.deployment
            )
        if request.probe_targets is not None:
            targets = request.probe_targets
            if targets.bitbucket_workspace:
                payload["bitbucket_workspace"] = targets.bitbucket_workspace
            if targets.bitbucket_repo:
                payload["bitbucket_repo"] = targets.bitbucket_repo
            if targets.confluence_space_key:
                payload["confluence_space_key"] = targets.confluence_space_key

        self._vault.write(staging, payload)
        # Drop the intermediate immutable str - the canonical copy is
        # now in Vault (encrypted at rest by the local-dev backend).
        del token_str

    # ------------------------------------------------------------------
    # Probe phase
    # ------------------------------------------------------------------

    async def _run_probe(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        request: AddCredentialRequest,
    ) -> ProbeResult:
        """Run a single read+write probe against the staged credential.

        Constructs a :class:`ResolvedCredential` from the request
        fields rather than reading back from Vault - the canonical
        copy is already in the staging path, and this avoids an
        extra round trip per call.
        """

        token_str = bytes(request.personal_token).decode("utf-8")
        try:
            cred = ResolvedCredential(
                url=request.url,
                username=request.username,
                personal_token=token_str,
            )
            runner = ProbeRunner(
                self._probe_client,
                clock=lambda: int(self._clock().timestamp()),
            )
            return await runner.run(
                dept_id, service, cred, targets=request.probe_targets
            )
        finally:
            # Drop the intermediate ``str``; it is immutable so the
            # best we can do is shorten its lifetime.  The canonical
            # plain-text token still lives in
            # ``request.personal_token`` (a ``bytearray``) and gets
            # zeroed by :meth:`_zero_request_token` once the probe
            # phase concludes.
            del token_str

    # ------------------------------------------------------------------
    # Inline bot identity probe
    # ------------------------------------------------------------------

    async def _run_bot_identity_probe(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        request: AddCredentialRequest,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
    ) -> BotIdentityProbeResult:
        """Run the inline bot identity probe after credential commit.

        After Vault write + DB upsert succeed, we issue a lightweight
        read-only Atlassian call to resolve the bot's ``account_id``.
        The result is surfaced in the HTTP response via
        ``account_id_probe_status`` / ``account_id_probe_error``.

        On success, the resolved ``account_id`` is upserted into
        ``automation.department_bot_identity`` and an audit event
        ``bot_account_id_probed`` is written.

        On failure, an audit event ``bot_account_id_probe_failed``
        is written.  The credential write is **not** rolled back -
        the probe is best-effort.

        .. important:: **Invariant: idempotent config**

           The resolved ``account_id`` is persisted **only** to the
           ``automation.department_bot_identity`` Postgres table.
           ``config/departments.json`` is **never** written to by this
           method - empty ``account_id`` entries in the JSON file
           remain as-is.  Runtime-resolved identities live in the DB;
           the JSON file is the operator's static declaration.

        Note: The plain-text token has already been zeroed by this
        point in the flow.  We read the credential back from Vault
        (the final path) to construct the
        :class:`ResolvedCredential` for the probe call.
        """

        # Read the credential from the final Vault path to construct
        # the ResolvedCredential.  The token was zeroed from the
        # request bytearray, so we must read from Vault.
        final = final_vault_path(dept_id, service)
        try:
            vault_data = dict(self._vault.read(final))
            cred = ResolvedCredential(
                url=vault_data.get("url", request.url),
                username=vault_data.get("username", request.username),
                personal_token=vault_data.get("personal_token", ""),
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "dept_credential.bot_identity_probe.vault_read_failed "
                "dept_id=%s service=%s err=%s",
                dept_id,
                service,
                type(exc).__name__,
            )
            error_msg = f"vault_read_failed: {type(exc).__name__}"
            await self._audit_bot_identity_probe_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                error=error_msg,
            )
            return BotIdentityProbeResult(success=False, error=error_msg)

        # Run the identity probe
        result = await probe_bot_identity(
            dept_id=dept_id,
            service=service,
            client=self._probe_client,
            cred=cred,
        )

        if result.success and result.account_id:
            # Upsert into department_bot_identity table
            try:
                connection = await self._connection_factory()
                async with with_dept_session(
                    self._actor_role_for_session, dept_id, connection=connection
                ) as conn:
                    await conn.execute(
                        """
                        INSERT INTO automation.department_bot_identity
                            (dept_id, service, account_id, probed_at, probe_status)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (dept_id, service) DO UPDATE
                        SET account_id   = EXCLUDED.account_id,
                            probed_at    = EXCLUDED.probed_at,
                            probe_status = EXCLUDED.probe_status
                        """,
                        dept_id,
                        service,
                        result.account_id,
                        self._clock(),
                        "ok",
                    )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "dept_credential.bot_identity_probe.upsert_failed "
                    "dept_id=%s service=%s err=%s",
                    dept_id,
                    service,
                    type(exc).__name__,
                )
                # DB upsert failure does not invalidate the probe
                # result - the account_id was resolved, we just
                # couldn't persist it.  Still report success to the
                # caller.

            # Audit success
            await self._audit.write(
                AuditEvent(
                    actor_id=actor_id,
                    actor_role=actor_role,
                    dept_id=dept_id,
                    action="bot_account_id_probed",
                    resource=f"dept_bot_identity:{dept_id}:{service}",
                    result="ok",
                    timestamp=self._clock(),
                    payload={
                        "dept_id": dept_id,
                        "service": service,
                        "resolved_account_id": result.account_id,
                    },
                )
            )
        else:
            # Audit failure
            await self._audit_bot_identity_probe_failed(
                dept_id=dept_id,
                service=service,
                actor_id=actor_id,
                actor_role=actor_role,
                error=result.error or "unknown",
            )

        return result

    async def _audit_bot_identity_probe_failed(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
        error: str,
    ) -> None:
        """Write the ``bot_account_id_probe_failed`` audit row."""

        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=dept_id,
                action="bot_account_id_probe_failed",
                resource=f"dept_bot_identity:{dept_id}:{service}",
                result="error",
                timestamp=self._clock(),
                payload={
                    "dept_id": dept_id,
                    "service": service,
                    "error_type": error,
                },
            )
        )

    # ------------------------------------------------------------------
    # DB upsert + Vault staging→final move
    # ------------------------------------------------------------------

    async def _commit(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        request: AddCredentialRequest,
        staging: VaultPath,
        final: VaultPath,
        resolved_account_id: str | None,
    ) -> Literal["created", "updated"]:
        """Run the SQL UPSERT and Vault staging→final move atomically.

        Atomicity contract: any failure - staging→final promotion
        error, INSERT failure, **or SQL ``COMMIT`` failure raised at the
        context-manager exit** - must roll the system back to the
        pre-call state.  In particular the Vault tree must be
        restored to its prior shape:

        * If the pair was previously registered, the final path is
          rewritten with the *prior* value.
        * If the pair was new, the final path is deleted.

        SQL flow inside the single ``with_dept_session`` block:

            1. ``SELECT 1 FROM automation.departments WHERE id=$1``
               - surface 404 *before* mutating Vault on the success
               path.
            2. ``INSERT ... ON CONFLICT (department_id, service) DO
               UPDATE`` - UPSERT the bot row.
            3. Promote staging → final via
               :meth:`_promote_staging`.

        ``with_dept_session.__aexit__`` issues the ``COMMIT`` after
        the body returns; a COMMIT failure surfaces *outside* the
        ``async with`` body, so the rollback wrapper around the
        whole block (``try`` / ``except BaseException``) is what
        guarantees the Vault tree gets restored on that path -
        the inner block alone cannot react to a failure that does
        not raise until the context manager exits.
        """

        deployment = (
            "dc"
            if request.deployment == "server"
            else (request.deployment or "cloud")
        )

        # Snapshot the prior final value BEFORE we mutate anything.
        # ``KeyError`` here means the pair was never registered;
        # ``None`` is the marker for "no prior value, so on failure
        # the rollback should DELETE the final path".  Any other
        # exception is a real Vault error and propagates upward
        # (the outer ``add_or_update`` cleans up the staging key
        # in that case).
        prior_final_snapshot: Mapping[str, str] | None
        try:
            prior_final_snapshot = dict(self._vault.read(final))
        except KeyError:
            prior_final_snapshot = None

        connection = await self._connection_factory()
        promoted = False
        existed = False
        try:
            async with with_dept_session(
                self._actor_role_for_session, dept_id, connection=connection
            ) as conn:
                # 1. Confirm the dept exists; surface 404 cleanly so
                #    the router does not have to introspect the SQL
                #    error.
                row = await self._fetchrow(
                    conn,
                    "SELECT 1 FROM automation.departments WHERE id = $1",
                    dept_id,
                )
                if row is None:
                    raise DepartmentNotFoundError(dept_id)

                # 2. UPSERT the bot row.  ``credential_ref`` points at
                #    the *final* path even though the value still
                #    lives at the staging path until we promote
                #    below - by the time SQL COMMIT runs the value
                #    will be at the final path.
                existed = await self._row_exists(conn, dept_id, service)
                await self._upsert_bot_row(
                    conn,
                    dept_id=dept_id,
                    service=service,
                    credential_ref=final.raw,
                    account_id=resolved_account_id,
                    username=request.username,
                    deployment=deployment,
                )

                # 3. Promote staging → final.  Once this returns we
                #    record ``promoted=True`` so the outer rollback
                #    knows to restore the snapshot if SQL COMMIT
                #    later fails.
                self._promote_staging(staging, final)
                promoted = True
            # ``COMMIT`` runs as the context manager exits cleanly;
            # if it fails the exception propagates and the outer
            # ``except BaseException`` below restores Vault.
        except BaseException:
            if promoted:
                self._rollback_final_path(final, prior_final_snapshot)
            raise

        return "updated" if existed else "created"

    def _rollback_final_path(
        self,
        final: VaultPath,
        prior_snapshot: Mapping[str, str] | None,
    ) -> None:
        """Restore *final* to its pre-call state on any commit-time failure.

        Called by :meth:`_commit` when the staging→final promotion
        already ran but the surrounding SQL transaction was rolled
        back (eg. because COMMIT failed).  Restoration semantics:

        * ``prior_snapshot is None`` → the pair was never
          registered, so DELETE the new final value.
        * ``prior_snapshot`` is a mapping → rewrite the prior
          value at *final* so a previously-registered credential
          is observably unchanged after the failed call.

        Failures here are logged but never raised - the caller is
        already propagating the original exception and must not
        have it shadowed by a rollback hiccup.  An admin can
        re-run the original operation; the staging key (if any)
        is cleaned up by the outer ``add_or_update`` failure
        path so the Vault tree never carries stale staging data
        after a completed call.
        """

        try:
            if prior_snapshot is None:
                self._vault.delete(final)
            else:
                self._vault.write(final, dict(prior_snapshot))
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "dept_credential.commit.rollback_final_path_failed "
                "path=%s prior=%s",
                final.raw,
                "absent" if prior_snapshot is None else "present",
            )

    async def _row_exists(
        self,
        conn: AsyncConnection,
        dept_id: str,
        service: ProbeService,
    ) -> bool:
        """Return whether a ``department_bots`` row exists for the pair."""

        row = await self._fetchrow(
            conn,
            """
            SELECT 1 FROM automation.department_bots
            WHERE department_id = $1 AND service = $2
            """,
            dept_id,
            service,
        )
        return row is not None

    @staticmethod
    async def _upsert_bot_row(
        conn: AsyncConnection,
        *,
        dept_id: str,
        service: ProbeService,
        credential_ref: str,
        account_id: str | None,
        username: str | None,
        deployment: str,
    ) -> None:
        """Run the canonical INSERT ... ON CONFLICT DO UPDATE SQL.

        Mirrors the column set + CHECK constraints declared in
        ``infra/postgres/10_automation.sql`` §"2. department_bots".
        """

        await conn.execute(
            """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref, account_id,
                 username, deployment)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (department_id, service) DO UPDATE
            SET credential_ref = EXCLUDED.credential_ref,
                account_id     = EXCLUDED.account_id,
                username       = EXCLUDED.username,
                deployment     = EXCLUDED.deployment
            """,
            dept_id,
            service,
            credential_ref,
            account_id,
            username,
            deployment,
        )

    async def _delete_bot_row(
        self,
        dept_id: str,
        service: ProbeService,
    ) -> bool:
        """Delete the bot row + return whether it existed.

        Wrapped in a ``with_dept_session`` block so RLS sees the
        admin / system role and the GUC-driven policies admit the
        delete.
        """

        connection = await self._connection_factory()
        async with with_dept_session(
            self._actor_role_for_session, dept_id, connection=connection
        ) as conn:
            row = await self._fetchrow(
                conn,
                "SELECT 1 FROM automation.departments WHERE id = $1",
                dept_id,
            )
            if row is None:
                raise DepartmentNotFoundError(dept_id)

            existed = await self._row_exists(conn, dept_id, service)
            if existed:
                await conn.execute(
                    """
                    DELETE FROM automation.department_bots
                    WHERE department_id = $1 AND service = $2
                    """,
                    dept_id,
                    service,
                )
            return existed

    async def _list_bots(self, dept_id: str) -> tuple[_DeptBotRow, ...]:
        """Return every ``department_bots`` row for *dept_id*."""

        connection = await self._connection_factory()
        async with with_dept_session(
            self._actor_role_for_session, dept_id, connection=connection
        ) as conn:
            row = await self._fetchrow(
                conn,
                "SELECT 1 FROM automation.departments WHERE id = $1",
                dept_id,
            )
            if row is None:
                raise DepartmentNotFoundError(dept_id)

            rows = await self._fetch(
                conn,
                """
                SELECT department_id, service, credential_ref,
                       account_id, username, deployment
                FROM automation.department_bots
                WHERE department_id = $1
                ORDER BY service
                """,
                dept_id,
            )

        return tuple(
            _DeptBotRow(
                department_id=r["department_id"],
                service=r["service"],
                credential_ref=r["credential_ref"],
                account_id=r["account_id"],
                username=r["username"],
                deployment=r["deployment"],
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Vault staging → final promotion
    # ------------------------------------------------------------------

    def _promote_staging(self, staging: VaultPath, final: VaultPath) -> None:
        """Move *staging* → *final* (read → write → delete staging).

        Mirrors :meth:`DepartmentCreateOrchestrator._promote_staging`
        - Vault has no native ``mv`` primitive, so we read-then-write-
        then-delete.  A failure inside ``write`` leaves the staging
        key around for the outer rollback to delete; a failure inside
        ``delete`` is logged but not raised (the staging key is safe
        to leave for the background sweeper).
        """

        value = dict(self._vault.read(staging))
        self._vault.write(final, value)
        try:
            self._vault.delete(staging)
        except Exception:  # noqa: BLE001
            _LOG.warning(
                "dept_credential.promote.staging_delete_failed path=%s",
                staging.raw,
            )

    def _best_effort_delete_staging(
        self, paths: Iterable[VaultPath]
    ) -> None:
        """Delete every *path* under ``_staging/``; never raises.

        Called on every failure path.  Identical contract to
        :meth:`DepartmentCreateOrchestrator._best_effort_delete_staging`
        - individual delete failures are logged and swallowed so the
        outer caller can re-raise the original error.
        """

        for path in paths:
            try:
                self._vault.delete(path)
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "dept_credential.cleanup.vault_delete_failed path=%s",
                    path.raw,
                )

    # ------------------------------------------------------------------
    # Plain-text hygiene
    # ------------------------------------------------------------------

    @staticmethod
    def _zero_request_token(request: AddCredentialRequest) -> None:
        """Zero ``request.personal_token`` in-place."""

        scrub_plain_text_token(request.personal_token)

    # ------------------------------------------------------------------
    # Audit helper
    # ------------------------------------------------------------------

    async def _audit_add_failed(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        actor_id: str,
        actor_role: Literal["admin", "dept_admin", "system"],
        reason: str,
    ) -> None:
        """Write the canonical ``dept_credential_add_failed`` row."""

        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=dept_id,
                action="dept_credential_add_failed",
                resource=f"dept_credential:{dept_id}:{service}",
                result="error",
                timestamp=self._clock(),
                payload={"service": service, "reason": reason},
            )
        )

    # ------------------------------------------------------------------
    # Async DB helpers - protocol-agnostic shims
    # ------------------------------------------------------------------

    @staticmethod
    async def _fetchrow(
        conn: AsyncConnection,
        query: str,
        *args: Any,
    ) -> Any:
        """Compatibility shim: :class:`AsyncConnection` only declares
        :meth:`execute`, but real asyncpg connections expose
        :meth:`fetchrow` / :meth:`fetch`.  Tests may inject a fake that
        only implements :meth:`execute`; in that case we fall back to
        pretending the row does not exist (the fake is responsible for
        recording the SQL).
        """

        fetchrow = getattr(conn, "fetchrow", None)
        if fetchrow is None:
            await conn.execute(query, *args)
            return None
        return await fetchrow(query, *args)

    @staticmethod
    async def _fetch(
        conn: AsyncConnection,
        query: str,
        *args: Any,
    ) -> Sequence[Mapping[str, Any]]:
        """Same compatibility shim as :meth:`_fetchrow`, for SELECTs
        that may return multiple rows.
        """

        fetch = getattr(conn, "fetch", None)
        if fetch is None:
            await conn.execute(query, *args)
            return ()
        return await fetch(query, *args)
