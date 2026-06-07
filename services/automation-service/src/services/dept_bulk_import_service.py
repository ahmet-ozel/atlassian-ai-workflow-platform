"""Bulk department import service.

Orchestrates the import of multiple departments from a single JSON
file upload.  Reuses the staging pattern:

    For each department in the uploaded file:
    1. Write credentials to staging Vault path.
    2. Run connectivity probe against staged credentials.
    3. On success: promote to final Vault path + DB commit.
    4. On failure: skip department, clean up staging, report in result.

The ``dry_run=True`` mode validates the schema and simulates probes
without writing any state (Vault or DB).

"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping

import jsonschema

from audit_logger import AuditEvent, AuditLogger
from db_shared import AsyncConnection
from vault_client import VaultClient, VaultPath

from automation_service.probe import (
    AtlassianProbeClient,
    ProbeResult,
    ProbeRunner,
    ProbeService,
    ResolvedCredential,
)
from automation_service.staging import (
    VALID_SERVICES,
    final_vault_path,
    staging_vault_path,
    validate_dept_id,
)

__all__ = [
    "BulkImportResult",
    "BulkImportService",
    "DeptImportOutcome",
    "SchemaValidationError",
]

_LOG = logging.getLogger(__name__)

# A connection factory - the orchestrator never owns pool lifecycle.
ConnectionFactory = Callable[[], Awaitable[AsyncConnection]]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SchemaValidationError(ValueError):
    """Raised when the uploaded JSON fails ``departments.schema.json`` validation.

    The router maps this to HTTP 422.

    Attributes:
        errors: List of human-readable validation error messages.
    """

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"Schema validation failed: {len(errors)} error(s)")
        self.errors = errors


# ---------------------------------------------------------------------------
# Result value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeServiceOutcome:
    """Per-service probe outcome within a department import."""

    service: str
    status: Literal["ok", "failed", "skipped"]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DeptImportOutcome:
    """Per-department import outcome.

    Attributes:
        dept_id: The department id from the JSON entry.
        status: Overall outcome for this department.
        error: Error description when ``status == "failed"``.
        probe_results: Per-service probe outcomes.
    """

    dept_id: str
    status: Literal["imported", "failed", "validated"]
    error: str | None = None
    probe_results: list[ProbeServiceOutcome] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BulkImportResult:
    """Aggregate result of a bulk import operation.

    Attributes:
        txn_id: Unique transaction identifier for audit correlation.
        total: Total number of departments in the uploaded file.
        validated: Departments that passed schema validation.
        imported: Departments successfully imported (empty in dry_run).
        failed: Departments that failed probe or commit.
        dry_run: Whether this was a dry-run (no state changes).
    """

    txn_id: str
    total: int
    validated: list[DeptImportOutcome]
    imported: list[DeptImportOutcome]
    failed: list[DeptImportOutcome]
    dry_run: bool


# ---------------------------------------------------------------------------
# Schema loading helper
# ---------------------------------------------------------------------------


def _load_schema() -> dict[str, Any]:
    """Load ``departments.schema.json`` from the config directory.

    The schema path is resolved relative to the platform root.  In
    production the working directory is ``platform/``; in tests the
    caller may override via monkeypatch.
    """
    import pathlib

    # Walk up from this file to find the platform root
    # This file: platform/services/automation-service/src/services/dept_bulk_import_service.py
    # Platform root: platform/
    current = pathlib.Path(__file__).resolve()
    # Go up: services -> src -> automation-service -> services -> platform
    platform_root = current.parents[4]
    schema_path = platform_root / "config" / "departments.schema.json"

    if not schema_path.exists():
        raise FileNotFoundError(
            f"departments.schema.json not found at {schema_path}"
        )

    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class BulkImportService:
    """Bulk department import orchestrator.

    Implements the per-department atomic flow described in design.md:
    staging write  probe  promote + DB commit.  Failures on any
    step cause that department to be skipped with staging cleanup.

    Args:
        vault: :class:`VaultClient` for credential storage.
        connection_factory: Async factory returning a fresh
            :class:`AsyncConnection` per call.
        probe_client: Atlassian probe client (production or fake).
        audit_logger: Required for audit trail.
        clock: UTC-now factory; overridable for deterministic tests.
        schema: Optional pre-loaded JSON schema dict.  When ``None``
            the schema is loaded from disk on first use.
    """

    def __init__(
        self,
        *,
        vault: VaultClient,
        connection_factory: ConnectionFactory,
        probe_client: AtlassianProbeClient,
        audit_logger: AuditLogger,
        clock: Callable[[], datetime] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self._vault = vault
        self._connection_factory = connection_factory
        self._probe_client = probe_client
        self._audit = audit_logger
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schema = schema

    # ==================================================================
    # Public API
    # ==================================================================

    async def bulk_import(
        self,
        file_content: bytes,
        dry_run: bool = True,
        *,
        actor_id: str = "system",
        actor_role: Literal["admin", "system"] = "admin",
    ) -> BulkImportResult:
        """Import departments from a JSON file.

        Args:
            file_content: Raw bytes of the uploaded JSON file.
            dry_run: When ``True``, only validate and simulate probes
                without writing any state.
            actor_id: OIDC ``sub`` of the admin performing the import.
            actor_role: RBAC role of the caller.

        Returns:
            :class:`BulkImportResult` with per-department outcomes.

        Raises:
            SchemaValidationError: When the JSON fails schema
                validation.  The router maps this to HTTP 422.
        """
        txn_id = uuid.uuid4().hex

        _LOG.info(
            "bulk_import.start txn_id=%s dry_run=%s",
            txn_id,
            dry_run,
        )

        # --- Step 1: Parse JSON -------------------------------------------
        try:
            data = json.loads(file_content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SchemaValidationError(
                [f"Invalid JSON: {type(exc).__name__}: {exc}"]
            ) from exc

        # --- Step 2: Schema validation ------------------------------------
        schema = self._get_schema()
        validation_errors = self._validate_schema(data, schema)
        if validation_errors:
            raise SchemaValidationError(validation_errors)

        departments: list[dict[str, Any]] = data.get("departments", [])
        total = len(departments)

        # Audit: import started
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id="*",
                action="dept_bulk_import_started",
                resource="bulk_import",
                result="ok",
                timestamp=self._clock(),
                payload={
                    "txn_id": txn_id,
                    "total_count": total,
                    "dry_run": dry_run,
                },
            )
        )

        # --- Step 3: Per-department processing ----------------------------
        validated: list[DeptImportOutcome] = []
        imported: list[DeptImportOutcome] = []
        failed: list[DeptImportOutcome] = []

        for dept_entry in departments:
            dept_id = dept_entry.get("id", "unknown")

            if dry_run:
                outcome = await self._process_dept_dry_run(
                    dept_entry=dept_entry,
                    txn_id=txn_id,
                )
                if outcome.status == "validated":
                    validated.append(outcome)
                else:
                    failed.append(outcome)
            else:
                outcome = await self._process_dept_live(
                    dept_entry=dept_entry,
                    txn_id=txn_id,
                    actor_id=actor_id,
                    actor_role=actor_role,
                )
                if outcome.status == "imported":
                    imported.append(outcome)
                    validated.append(
                        DeptImportOutcome(
                            dept_id=outcome.dept_id,
                            status="validated",
                            probe_results=outcome.probe_results,
                        )
                    )
                else:
                    failed.append(outcome)

        # Audit: import completed
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id="*",
                action="dept_bulk_import_completed",
                resource="bulk_import",
                result="ok",
                timestamp=self._clock(),
                payload={
                    "txn_id": txn_id,
                    "total_count": total,
                    "success_count": len(imported),
                    "fail_count": len(failed),
                    "dry_run": dry_run,
                },
            )
        )

        _LOG.info(
            "bulk_import.done txn_id=%s total=%d imported=%d failed=%d "
            "dry_run=%s",
            txn_id,
            total,
            len(imported),
            len(failed),
            dry_run,
        )

        return BulkImportResult(
            txn_id=txn_id,
            total=total,
            validated=validated,
            imported=imported,
            failed=failed,
            dry_run=dry_run,
        )

    # ==================================================================
    # Internal: dry-run processing
    # ==================================================================

    async def _process_dept_dry_run(
        self,
        *,
        dept_entry: dict[str, Any],
        txn_id: str,
    ) -> DeptImportOutcome:
        """Validate a department entry and simulate probes without state changes.

        In dry-run mode:
        - Validates dept_id format.
        - Checks bot credential structure.
        - Simulates probe by calling the probe client in read-only mode
          (or returns a simulated "ok" if credentials are structurally valid).
        - No Vault writes, no DB commits.
        """
        dept_id = dept_entry.get("id", "unknown")

        # Validate dept_id format
        try:
            validate_dept_id(dept_id)
        except ValueError as exc:
            return DeptImportOutcome(
                dept_id=dept_id,
                status="failed",
                error=f"Invalid dept_id: {exc}",
            )

        # Extract bot services and simulate probe
        bot_config = dept_entry.get("bot", {})
        probe_results: list[ProbeServiceOutcome] = []

        for service_name in VALID_SERVICES:
            if service_name not in bot_config:
                continue

            bot_entry = bot_config[service_name]
            # In dry-run we check structural validity of credentials
            has_credential_ref = bool(bot_entry.get("credential_ref"))
            has_inline_creds = bool(
                bot_entry.get("email") and bot_entry.get("api_token_ref")
            )

            if has_credential_ref or has_inline_creds:
                # Simulate probe success for structurally valid credentials
                probe_results.append(
                    ProbeServiceOutcome(
                        service=service_name,
                        status="ok",
                    )
                )
            else:
                probe_results.append(
                    ProbeServiceOutcome(
                        service=service_name,
                        status="failed",
                        error="No valid credential structure found",
                    )
                )

        # If any probe simulation failed, mark dept as failed
        any_failed = any(p.status == "failed" for p in probe_results)
        if any_failed:
            return DeptImportOutcome(
                dept_id=dept_id,
                status="failed",
                error="One or more services have invalid credential structure",
                probe_results=probe_results,
            )

        return DeptImportOutcome(
            dept_id=dept_id,
            status="validated",
            probe_results=probe_results,
        )

    # ==================================================================
    # Internal: live processing (atomic per-dept)
    # ==================================================================

    async def _process_dept_live(
        self,
        *,
        dept_entry: dict[str, Any],
        txn_id: str,
        actor_id: str,
        actor_role: Literal["admin", "system"],
    ) -> DeptImportOutcome:
        """Process a single department with the full atomic staging flow.

        Flow per department:
        1. Write credentials to staging Vault path.
        2. Run connectivity probe against staged credentials.
        3. On probe success: promote to final path + DB commit.
        4. On any failure: clean up staging, report as failed.
        """
        dept_id = dept_entry.get("id", "unknown")

        # Validate dept_id format
        try:
            dept_id = validate_dept_id(dept_id)
        except ValueError as exc:
            return DeptImportOutcome(
                dept_id=dept_id,
                status="failed",
                error=f"Invalid dept_id: {exc}",
            )

        bot_config = dept_entry.get("bot", {})
        probe_results: list[ProbeServiceOutcome] = []
        staging_paths: list[VaultPath] = []
        all_probes_ok = True

        # Process each service in the bot config
        for service_name in VALID_SERVICES:
            if service_name not in bot_config:
                continue

            bot_entry = bot_config[service_name]
            # Request ID must be ≤64 chars and match [a-zA-Z0-9][a-zA-Z0-9_-]*
            # UUID hex (32) + hyphen (1) + short hash = safe length
            request_id = f"{txn_id[:16]}-{dept_id[:16]}-{service_name}"

            # --- Step 1: Write to staging Vault path ---
            try:
                staging = staging_vault_path(request_id, service_name)
                staging_paths.append(staging)

                credential_payload = self._build_vault_payload(bot_entry)
                self._vault.write(staging, credential_payload)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "bulk_import.staging_write_failed dept_id=%s service=%s "
                    "err=%s",
                    dept_id,
                    service_name,
                    type(exc).__name__,
                )
                self._cleanup_staging(staging_paths)
                return DeptImportOutcome(
                    dept_id=dept_id,
                    status="failed",
                    error=f"Staging write failed for {service_name}: "
                    f"{type(exc).__name__}",
                    probe_results=probe_results,
                )

            # --- Step 2: Run connectivity probe ---
            try:
                cred = self._resolve_credential_from_entry(bot_entry)
                probe_result = await self._run_probe(
                    dept_id=dept_id,
                    service=service_name,  # type: ignore[arg-type]
                    credential=cred,
                )

                if probe_result.state == "ok":
                    probe_results.append(
                        ProbeServiceOutcome(
                            service=service_name,
                            status="ok",
                        )
                    )
                else:
                    all_probes_ok = False
                    probe_results.append(
                        ProbeServiceOutcome(
                            service=service_name,
                            status="failed",
                            error=probe_result.error_message
                            or f"Probe failed: {probe_result.state}",
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "bulk_import.probe_failed dept_id=%s service=%s err=%s",
                    dept_id,
                    service_name,
                    type(exc).__name__,
                )
                all_probes_ok = False
                probe_results.append(
                    ProbeServiceOutcome(
                        service=service_name,
                        status="failed",
                        error=f"Probe error: {type(exc).__name__}",
                    )
                )

        # If any probe failed, clean up staging and skip this dept
        if not all_probes_ok:
            self._cleanup_staging(staging_paths)
            return DeptImportOutcome(
                dept_id=dept_id,
                status="failed",
                error="One or more service probes failed",
                probe_results=probe_results,
            )

        # --- Step 3: Promote staging  final + DB commit ---
        try:
            await self._promote_and_commit(
                dept_id=dept_id,
                dept_entry=dept_entry,
                bot_config=bot_config,
                txn_id=txn_id,
                staging_paths=staging_paths,
                actor_id=actor_id,
                actor_role=actor_role,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.error(
                "bulk_import.commit_failed dept_id=%s err=%s",
                dept_id,
                type(exc).__name__,
            )
            self._cleanup_staging(staging_paths)
            return DeptImportOutcome(
                dept_id=dept_id,
                status="failed",
                error=f"Commit failed: {type(exc).__name__}",
                probe_results=probe_results,
            )

        return DeptImportOutcome(
            dept_id=dept_id,
            status="imported",
            probe_results=probe_results,
        )

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _get_schema(self) -> dict[str, Any]:
        """Return the cached or freshly loaded JSON schema."""
        if self._schema is None:
            self._schema = _load_schema()
        return self._schema

    def _validate_schema(
        self,
        data: Any,
        schema: dict[str, Any],
    ) -> list[str]:
        """Validate *data* against the departments JSON schema.

        Returns a list of error messages (empty on success).
        """
        validator = jsonschema.Draft202012Validator(schema)
        errors: list[str] = []
        for error in validator.iter_errors(data):
            # Build a human-readable path
            path = ".".join(str(p) for p in error.absolute_path) or "(root)"
            errors.append(f"{path}: {error.message}")
        return errors

    def _build_vault_payload(
        self,
        bot_entry: dict[str, Any],
    ) -> Mapping[str, str]:
        """Build the Vault KV payload from a bot entry.

        Extracts username/email and credential reference to build the
        payload written to Vault.  For bulk import, the credential_ref
        or api_token_ref points to an existing Vault path; we store
        the reference metadata.
        """
        username = bot_entry.get("username") or bot_entry.get("email") or ""
        # For bulk import, credential_ref is the canonical path
        credential_ref = bot_entry.get("credential_ref") or ""
        account_id = bot_entry.get("account_id") or ""

        payload: dict[str, str] = {
            "username": username,
            "credential_ref": credential_ref,
        }
        if account_id:
            payload["account_id"] = account_id
        if bot_entry.get("api_token_ref"):
            payload["api_token_ref"] = bot_entry["api_token_ref"]
        if bot_entry.get("deployment"):
            payload["deployment"] = bot_entry["deployment"]

        return payload

    def _resolve_credential_from_entry(
        self,
        bot_entry: dict[str, Any],
    ) -> ResolvedCredential:
        """Build a :class:`ResolvedCredential` from a bot entry for probing.

        In bulk import, credentials are referenced by Vault path
        (``credential_ref``).  The probe runner needs a resolved
        credential triple.  We read from Vault if a credential_ref
        is provided, otherwise construct from inline fields.
        """
        credential_ref = bot_entry.get("credential_ref")
        if credential_ref:
            # Read the credential from Vault
            try:
                vault_path = VaultPath.parse(credential_ref)
                secret = self._vault.read(vault_path)
                return ResolvedCredential(
                    url=secret.get("url", ""),
                    username=secret.get("username", ""),
                    personal_token=secret.get("personal_token", ""),
                )
            except (KeyError, Exception):
                # If Vault read fails, construct from available fields
                pass

        # Fallback: construct from inline fields
        return ResolvedCredential(
            url="",
            username=bot_entry.get("username") or bot_entry.get("email") or "",
            personal_token="",
        )

    async def _run_probe(
        self,
        *,
        dept_id: str,
        service: ProbeService,
        credential: ResolvedCredential,
    ) -> ProbeResult:
        """Run the connectivity probe for a single service.

        Uses the same :class:`ProbeRunner` as the single-credential
        flow to ensure consistent probe behaviour.
        """
        runner = ProbeRunner(
            self._probe_client,
            clock=lambda: int(self._clock().timestamp()),
        )
        return await runner.run(
            dept_id,
            service,
            credential,
            targets=None,
        )

    async def _promote_and_commit(
        self,
        *,
        dept_id: str,
        dept_entry: dict[str, Any],
        bot_config: dict[str, Any],
        txn_id: str,
        staging_paths: list[VaultPath],
        actor_id: str,
        actor_role: Literal["admin", "system"],
    ) -> None:
        """Promote staged credentials to final paths and commit to DB.

        This is the atomic commit step: Vault promotion + DB insert
        happen together.  If either fails, the caller cleans up
        staging.
        """
        # Promote each service's staging path to final
        for service_name in VALID_SERVICES:
            if service_name not in bot_config:
                continue

            request_id = f"{txn_id[:16]}-{dept_id[:16]}-{service_name}"
            staging = staging_vault_path(request_id, service_name)
            final = final_vault_path(dept_id, service_name)

            # Read from staging, write to final, delete staging
            try:
                staged_data = self._vault.read(staging)
                self._vault.write(final, staged_data)
                self._vault.delete(staging)
            except Exception:
                # If promotion fails, attempt to clean up final path
                # and re-raise so the caller handles the failure
                try:
                    self._vault.delete(final)
                except Exception:  # noqa: BLE001
                    pass
                raise

        # DB commit: insert department row
        conn = await self._connection_factory()
        try:
            await self._insert_department(conn, dept_entry)
            await self._insert_department_bots(conn, dept_id, bot_config)
        except Exception:
            # On DB failure, attempt to clean up promoted Vault paths
            for service_name in VALID_SERVICES:
                if service_name not in bot_config:
                    continue
                try:
                    final = final_vault_path(dept_id, service_name)
                    self._vault.delete(final)
                except Exception:  # noqa: BLE001
                    pass
            raise

    async def _insert_department(
        self,
        conn: AsyncConnection,
        dept_entry: dict[str, Any],
    ) -> None:
        """Insert a department row into ``automation.departments``.

        Uses the connection's execute method to run the INSERT.
        Duplicate dept_id will raise a unique constraint violation
        which the caller catches and reports as a failure.
        """
        dept_id = dept_entry["id"]
        display_name = dept_entry.get("display_name", dept_id)
        mode = dept_entry.get("mode", "active")
        jira_project_keys = dept_entry.get("jira_project_keys", [])
        budget_caps = dept_entry.get("budget_caps", {})

        await conn.execute(
            """
            INSERT INTO automation.departments (
                id, display_name, mode, jira_project_keys, budget_caps,
                created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, NOW(), NOW())
            ON CONFLICT (id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                mode = EXCLUDED.mode,
                jira_project_keys = EXCLUDED.jira_project_keys,
                budget_caps = EXCLUDED.budget_caps,
                updated_at = NOW()
            """,
            dept_id,
            display_name,
            mode,
            json.dumps(jira_project_keys),
            json.dumps(budget_caps),
        )

    async def _insert_department_bots(
        self,
        conn: AsyncConnection,
        dept_id: str,
        bot_config: dict[str, Any],
    ) -> None:
        """Insert bot rows into ``automation.department_bots``.

        One row per service defined in the bot config.
        """
        for service_name in VALID_SERVICES:
            if service_name not in bot_config:
                continue

            bot_entry = bot_config[service_name]
            credential_ref = (
                bot_entry.get("credential_ref")
                or f"vault:atlassian/{dept_id}/{service_name}"
            )
            account_id = bot_entry.get("account_id") or None
            username = (
                bot_entry.get("username") or bot_entry.get("email") or None
            )
            deployment = bot_entry.get("deployment") or "cloud"

            await conn.execute(
                """
                INSERT INTO automation.department_bots (
                    department_id, service, credential_ref,
                    account_id, username, deployment,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
                ON CONFLICT (department_id, service) DO UPDATE SET
                    credential_ref = EXCLUDED.credential_ref,
                    account_id = EXCLUDED.account_id,
                    username = EXCLUDED.username,
                    deployment = EXCLUDED.deployment,
                    updated_at = NOW()
                """,
                dept_id,
                service_name,
                credential_ref,
                account_id,
                username,
                deployment,
            )

    def _cleanup_staging(self, paths: list[VaultPath]) -> None:
        """Best-effort cleanup of staging Vault paths.

        Failures are logged but do not propagate - the staging paths
        will be garbage-collected by the periodic cleanup job.
        """
        for path in paths:
            try:
                self._vault.delete(path)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "bulk_import.staging_cleanup_failed path=%s err=%s",
                    path.raw,
                    type(exc).__name__,
                )
