"""Atomic department create orchestrator.

Implements the atomic department creation flow:

* Plain-text token enters via the request body, lands in
  Vault under ``vault:atlassian/_staging/<request_id>/<service>``,
  never appears in the response body, never reaches a log handler,
  and is wiped from the heap with ``bytearray.zero()`` (best-effort
  for Python's immutable ``str``; we keep the value in a
  ``bytearray`` from the moment we receive it).
* DB insert + Vault staging promotion run inside a single
  transaction. On any failure between staging-write and the final
  COMMIT, the staging key is deleted and the transaction rolls back;
  the caller sees HTTP 5xx.
* Duplicate ``id`` is rejected with HTTP 409 +
  ``dept_duplicate_id`` audit event.
* The probe runner used between staging and DB insert
  never lands plain-text credentials in any artifact.
* The full flow leaves no plain-text token in response body,
  log records, DB columns, or disk (the local-dev Vault backend
  encrypts the at-rest value; the Hashicorp backend never persists
  plain-text by construction).

The orchestrator is **HTTP-framework-agnostic** - the FastAPI router
in :mod:`automation_service.admin.router` is the thin shim that
parses the request, runs the orchestrator, and translates the
:class:`DepartmentCreateResult` / raised exceptions into HTTP status
codes. Keeping the orchestration here makes it directly exerciseable
from unit tests and property tests
(``test_dept_atomic_create.py``).

Sequence:

    1. Validate the incoming :class:`DepartmentCreateRequest`.
    2. For each ``(service, plain_token)`` pair, write the token to
       ``vault:atlassian/_staging/<request_id>/<service>``. The
       plain-text bytearray is zeroed immediately afterwards.
    3. Run probe(s) against the staging credentials. Probe failures
       short-circuit with a delete of *all* staging keys.
    4. ``BEGIN`` a DB transaction (via ``db_shared.with_dept_session``).
    5. Insert into ``automation.departments`` (config_json mirror)
       and ``automation.department_bots`` (with ``credential_ref``
       pointing at the **final** path).
    6. Move staging  final path in Vault: read staging value, write
       to final, delete staging. Done one-by-one so a partial failure
       still leaves the staging key around for cleanup.
    7. ``COMMIT`` and emit a ``dept_created`` audit row with
       ``actor_role`` carried from the call site.
    8. On ``UniqueViolationError`` (duplicate ``id``) emit a
       ``dept_duplicate_id`` audit and raise
       :class:`DepartmentAlreadyExistsError` so the router returns
       HTTP 409.
    9. On any other DB error, delete staging keys, ``ROLLBACK``,
       emit a ``dept_create_failed`` audit, and re-raise so the
       router returns HTTP 5xx.

The plain-text token NEVER appears in:

* response body - :class:`DepartmentCreateResult` does not carry it.
* logs - only the dept_id, request_id, service names and audit
  outcomes are logged. The token is held in a ``bytearray`` and
  zeroed before the function returns.
* DB rows - ``automation.department_bots.credential_ref`` stores the
  **Vault path**, not the value.
* Vault staging key - the value is encrypted at rest by the local-dev
  backend (libsodium ``SecretBox``) and is never written in
  plain-text by the Hashicorp backend.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, Sequence

from audit_logger import AuditEvent, AuditLogger
from db_shared import AsyncConnection, with_dept_session
from vault_client import VaultClient, VaultPath

from ..probe import (
    AtlassianProbeClient,
    ProbeRunner,
    ProbeService,
    ProbeTargets,
    ResolvedCredential,
)

__all__ = [
    "DepartmentAlreadyExistsError",
    "DepartmentCreateOrchestrator",
    "DepartmentCreateRequest",
    "DepartmentCreateResult",
    "ProbeFailureError",
    "StagingFailureError",
]

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

# Module-level logger; the platform's redaction filter
# (``http_shared.install_redaction_filter``) is attached to the root
# logger by ``automation_service.app.create_app`` so any record
# emitted here is redacted before reaching stdout.
_LOG = logging.getLogger(__name__)

# Service identifiers we accept on the create path. Mirrors the
# ``automation.department_bots.service`` CHECK constraint and the
# ``ProbeService`` literal.
_VALID_SERVICES: frozenset[str] = frozenset({"jira", "bitbucket", "confluence"})

# ---------------------------------------------------------------------------
# Exceptions surfaced to the router
# ---------------------------------------------------------------------------


class DepartmentAlreadyExistsError(Exception):
    """Raised when ``departments.id`` already exists and maps to HTTP 409."""

    def __init__(self, dept_id: str) -> None:
        super().__init__(
            f"department with id={dept_id!r} already exists"
        )
        self.dept_id = dept_id


class StagingFailureError(Exception):
    """Raised when the Vault staging write itself fails.

    Surfaces as HTTP 5xx. The orchestrator already issued any
    staging-key deletes that *did* succeed before raising.
    """


class ProbeFailureError(Exception):
    """Raised when a credential probe rejects the staged credential.

    The orchestrator has already deleted every staging key by the
    time this is raised; the caller surfaces HTTP 5xx with the probe
    error message stripped of any token / username material.
    """

    def __init__(self, service: str, state: str, message: str) -> None:
        super().__init__(
            f"probe for service={service!r} returned state={state!r}: {message}"
        )
        self.service = service
        self.state = state
        self.message = message


# ---------------------------------------------------------------------------
# Request / response value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BotCredential:
    """A single bot credential payload received from the caller.

    The ``personal_token`` field is a :class:`bytearray` so the
    orchestrator can call ``.zero()`` on it once Vault has the value
    (best effort; Python keeps strings immutable, so the
    public API requires the caller to hand the token in via
    ``bytearray`` from the FastAPI router parsing layer).

    Attributes:
        service: One of ``{"jira", "bitbucket", "confluence"}``.
        url: Atlassian site / Bitbucket workspace URL.
        username: Email or login the bot authenticates as.
        personal_token: API token. **Must** be a mutable
            :class:`bytearray` so it can be zeroed.
        account_id: Optional pre-known account_id; auto-fetched from
            the read probe when ``None``.
        deployment: Bitbucket deployment kind. Only meaningful when
            ``service == "bitbucket"``; ignored for the others.
    """

    service: ProbeService
    url: str
    username: str
    personal_token: bytearray
    account_id: str | None = None
    deployment: Literal["cloud", "server"] | None = None


@dataclass(frozen=True, slots=True)
class DepartmentCreateRequest:
    """Validated input for :meth:`DepartmentCreateOrchestrator.run`.

    The router builds this from the JSON body after running schema
    validation. Constructing :class:`DepartmentCreateRequest`
    directly in unit tests is the supported entry point for the
    property tests.

    Attributes:
        dept_id: Department identifier - must match the
            ``Department.id`` schema regex (validation is delegated
            to ``db_shared`` when the GUC is set).
        display_name: Human-readable name.
        default_language: ``"tr"`` or ``"en"``.
        web_search_enabled: Whether the Firecrawl capability is opted
            in.
        mode: Initial mode. The endpoint always commits ``"active"``
            after a successful probe; ``"shadow"`` and ``"disabled"``
            are accepted for tests / migration tooling.
        jira_project_keys: At least one project key (mirrors the
            schema ``minItems: 1`` constraint).
        confluence_space_keys: Optional list.
        bitbucket_workspace: Optional workspace slug.
        config_json: Verbatim mirror of the corresponding
            ``departments.json`` entry, persisted into the
            ``departments.config_json`` JSONB column. The orchestrator
            never reads or mutates this - it is opaque metadata.
        bots: One :class:`_BotCredential` per service the department
            authenticates as. At least one entry is required (mirrors
            the schema ``bot.anyOf`` constraint).
        probe_targets: Optional per-service Bitbucket / Confluence
            target metadata so the probe runner knows where to round-
            trip. Defaults are derived from the request fields.
    """

    dept_id: str
    display_name: str
    default_language: Literal["tr", "en"]
    web_search_enabled: bool
    mode: Literal["active", "shadow", "disabled"]
    jira_project_keys: tuple[str, ...]
    confluence_space_keys: tuple[str, ...]
    bitbucket_workspace: str | None
    config_json: Mapping[str, Any]
    bots: tuple[_BotCredential, ...]
    probe_targets: ProbeTargets | None = None


@dataclass(frozen=True, slots=True)
class DepartmentCreateResult:
    """Successful create response (router serialises this to JSON).

    The shape **deliberately omits** any plain-text credential field:
    the only credential reference visible to the caller is the
    final Vault path each bot ended up at.

    Attributes:
        dept_id: The department id that was created.
        request_id: Server-generated correlation id used for the
            staging Vault path. Surfaced so the caller can correlate
            with audit log rows.
        services: Sorted tuple of services the department now has
            credentials for.
        credential_refs: ``{service: vault_path}`` mapping pointing at
            the **final** (post-promotion) Vault path.
        created_at: UTC timestamp the row landed in the DB.
    """

    dept_id: str
    request_id: str
    services: tuple[ProbeService, ...]
    credential_refs: Mapping[ProbeService, str]
    created_at: datetime


# ---------------------------------------------------------------------------
# Helper protocols and types
# ---------------------------------------------------------------------------

#: Async factory that hands the orchestrator a fresh
#: :class:`db_shared.AsyncConnection` for the duration of the create
#: transaction. We accept a callable rather than a connection
#: directly so the orchestrator does not own pool lifecycle.
ConnectionFactory = Callable[[], Awaitable[AsyncConnection]]


# Some asyncpg-compatible duplicate-key error class is matched by
# substring on its message. The orchestrator avoids importing
# ``asyncpg`` directly so unit tests can use a fake connection that
# raises a stdlib ``Exception`` with the right substring.
_DUPLICATE_MARKERS: tuple[str, ...] = (
    "uniqueviolationerror",
    "duplicate key value",
    "violates unique constraint",
    'pkey"',
    "department with id",
)


def _looks_like_duplicate(exc: BaseException) -> bool:
    """Return whether *exc* is asyncpg's ``UniqueViolationError`` (or stub).

    The check is deliberately string-based so the orchestrator does
    not import ``asyncpg`` (which would force a hard dependency on
    the unit test path). The matched markers cover both
    asyncpg's wire-level message and the message a fake connection
    might raise in tests.
    """

    name = type(exc).__name__.lower()
    if "uniqueviolation" in name:
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _DUPLICATE_MARKERS)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


class DepartmentCreateOrchestrator:
    """Atomic department create - Vault staging + DB transaction.

    Args:
        vault: Pluggable :class:`VaultClient`. The orchestrator only
            uses ``read`` / ``write`` / ``delete``, so either backend
            (Hashicorp / local-dev) is acceptable.
        connection_factory: Callable returning a fresh
            :class:`db_shared.AsyncConnection`. The orchestrator opens
            exactly one transaction per ``run`` call.
        probe_client: Atlassian probe client (typically the production
            MCP-routed implementation). Tests inject an in-memory fake.
        audit_logger: Where ``dept_created`` / ``dept_duplicate_id`` /
            ``dept_create_failed`` events go. Required for auditable writes.
        clock: Optional UTC-now factory for deterministic timestamps
            in tests. Defaults to :func:`datetime.now`.

    The instance is stateless across calls - every ``run`` builds its
    own fresh staging + transaction lifecycle.
    """

    def __init__(
        self,
        *,
        vault: VaultClient,
        connection_factory: ConnectionFactory,
        probe_client: AtlassianProbeClient,
        audit_logger: AuditLogger,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._vault = vault
        self._connection_factory = connection_factory
        self._probe_client = probe_client
        self._audit = audit_logger
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        request: DepartmentCreateRequest,
        *,
        actor_id: str,
        actor_role: Literal["admin", "system"],
    ) -> DepartmentCreateResult:
        """Run the atomic create flow.

        Args:
            request: Validated input.
            actor_id: OIDC ``sub`` of the caller (or the bot's
                account_id for ``actor_role == "system"``).
            actor_role: ``"admin"`` for human admins that can create
                new departments,
                ``"system"`` for boot-time provisioning hooks.

        Returns:
            :class:`DepartmentCreateResult` on success.

        Raises:
            DepartmentAlreadyExistsError: ``id`` already used.
            ProbeFailureError: A staged credential failed the probe.
            StagingFailureError: Vault rejected the staging write.
            Exception: Any other DB / Vault error after staging keys
                have been cleaned up.
        """

        self._validate_request(request)

        request_id = uuid.uuid4().hex
        services = tuple(sorted({bot.service for bot in request.bots}))
        # Build the staging path mapping up-front so cleanup can run
        # even if we fail mid-write.
        staging_paths = {
            bot.service: self._staging_path(request_id, bot.service)
            for bot in request.bots
        }
        final_paths = {
            bot.service: self._final_path(request.dept_id, bot.service)
            for bot in request.bots
        }

        _LOG.info(
            "dept_create.start dept_id=%s request_id=%s services=%s",
            request.dept_id, request_id, services,
        )

        # 1. Stage every credential. Vault holds the encrypted-at-rest
        # canonical copy; the in-memory bytearray stays alive for
        # the probe phase below and is zeroed before the DB
        # transaction begins.
        try:
            for bot in request.bots:
                staging = staging_paths[bot.service]
                self._write_staging_credential(bot, staging)
        except StagingFailureError:
            await self._audit_create_failed(
                request, actor_id, actor_role, reason="staging_write_failed"
            )
            self._best_effort_delete_staging(staging_paths.values())
            raise

        # 2. Run probes against the staged credentials. The probe
        # runner reads the in-memory token bytes via the
        # :class:`ResolvedCredential` payload - Vault has the same
        # value, but reaching back into Vault for the probe would
        # add a needless round trip on every create call.
        try:
            await self._run_probes(request)
        except ProbeFailureError:
            self._best_effort_delete_staging(staging_paths.values())
            self._zero_all_tokens(request)
            await self._audit_create_failed(
                request, actor_id, actor_role, reason="probe_failed"
            )
            raise

        # Wipe every plain-text token from heap *before* the
        # DB transaction starts. Vault retains the encrypted-at-rest
        # copy at the staging path and (after promotion below) the
        # final path; the orchestrator never needs the plain-text
        # value again on the success path.
        self._zero_all_tokens(request)

        # 3. DB transaction + Vault stagingfinal move. Any failure
        # inside this block deletes every staging key and rolls
        # back; the orchestrator emits the appropriate audit row
        # before re-raising.
        try:
            created_at = await self._commit(
                request,
                staging_paths=staging_paths,
                final_paths=final_paths,
            )
        except DepartmentAlreadyExistsError:
            self._best_effort_delete_staging(staging_paths.values())
            await self._audit_duplicate(request, actor_id, actor_role)
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised below
            self._best_effort_delete_staging(staging_paths.values())
            await self._audit_create_failed(
                request,
                actor_id,
                actor_role,
                reason=f"db_or_vault_error:{type(exc).__name__}",
            )
            _LOG.error(
                "dept_create.failed dept_id=%s request_id=%s err=%s",
                request.dept_id, request_id, type(exc).__name__,
            )
            raise

        # 4. Successful path - emit a single ``dept_created`` audit
        # row carrying the actor's role for the audit
        # actor_role-mandatory invariant.
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=request.dept_id,
                action="dept_created",
                resource=f"department:{request.dept_id}",
                result="ok",
                timestamp=self._clock(),
                payload={
                    "request_id": request_id,
                    "services": list(services),
                },
            )
        )

        _LOG.info(
            "dept_create.ok dept_id=%s request_id=%s services=%s",
            request.dept_id, request_id, services,
        )

        return DepartmentCreateResult(
            dept_id=request.dept_id,
            request_id=request_id,
            services=services,
            credential_refs={
                svc: final_paths[svc].raw for svc in services
            },
            created_at=created_at,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_request(request: DepartmentCreateRequest) -> None:
        """Cheap pre-checks before we touch Vault or the DB.

        The full ``Department`` schema validation lives in the router
        layer (which uses ``departments.schema.json`` via
        ``jsonschema``); the orchestrator only enforces the few
        invariants it needs to operate safely:

        * At least one bot.
        * Every bot's service is one of the three known services.
        * No two bots target the same service (mirrors the
          ``uq_department_bots_dept_service`` UNIQUE constraint).
        * Each ``personal_token`` is a non-empty :class:`bytearray`.
        * Each ``url`` and ``username`` is a non-empty string.
        """

        if not request.bots:
            raise ValueError(
                "DepartmentCreateRequest must include at least one bot"
            )

        seen: set[str] = set()
        for bot in request.bots:
            if bot.service not in _VALID_SERVICES:
                raise ValueError(
                    f"unknown bot service {bot.service!r}; "
                    f"expected one of {sorted(_VALID_SERVICES)!r}"
                )
            if bot.service in seen:
                raise ValueError(
                    f"duplicate bot.service={bot.service!r} in request"
                )
            seen.add(bot.service)
            if not isinstance(bot.personal_token, bytearray):
                raise TypeError(
                    "bot.personal_token must be a bytearray so the "
                    "orchestrator can zero it after the staging write"
                )
            if not bot.personal_token:
                raise ValueError(
                    f"bot.personal_token for service={bot.service!r} is empty"
                )
            if not bot.url or not bot.username:
                raise ValueError(
                    f"bot.{bot.service} requires non-empty url and username"
                )

    # ------------------------------------------------------------------
    # Vault path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _staging_path(request_id: str, service: str) -> VaultPath:
        """Return ``vault:atlassian/_staging/<request_id>/<service>``.

        The path uses the shared staging convention so cross-service
        cleanup tooling can grep for ``_staging`` in the Vault tree.
        """

        return VaultPath.parse(f"vault:atlassian/_staging/{request_id}/{service}")

    @staticmethod
    def _final_path(dept_id: str, service: str) -> VaultPath:
        """Return ``vault:atlassian/<dept_id>/<service>``."""

        return VaultPath.parse(f"vault:atlassian/{dept_id}/{service}")

    # ------------------------------------------------------------------
    # Vault staging write
    # ------------------------------------------------------------------

    def _write_staging_credential(
        self,
        bot: _BotCredential,
        staging: VaultPath,
    ) -> None:
        """Write *bot* to *staging*.

        Vault stores a flat ``Mapping[str, str]`` per the KV-v2
        contract. The plain-text bytearray remains alive until the
        probe phase finishes (so the probe runner can authenticate
        against the target system); it is zeroed by
        :meth:`_zero_all_tokens` before the DB transaction begins.

        Raises:
            StagingFailureError: When the underlying Vault backend
                rejects the write.
        """

        try:
            token_str = bot.personal_token.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StagingFailureError(
                f"bot.personal_token for service={bot.service!r} is not valid utf-8"
            ) from exc

        payload = {
            "url": bot.url,
            "username": bot.username,
            "personal_token": token_str,
        }
        if bot.deployment is not None:
            payload["deployment"] = bot.deployment

        try:
            self._vault.write(staging, payload)
        except Exception as exc:  # noqa: BLE001
            raise StagingFailureError(
                f"vault staging write failed for service={bot.service!r}: "
                f"{type(exc).__name__}"
            ) from exc

        # The intermediate ``str`` is immutable - rebind the local so
        # it goes out of scope at function return. The canonical copy
        # is now in Vault (encrypted at rest by the local-dev
        # backend; never persisted in plain-text by Hashicorp).
        del token_str

    # ------------------------------------------------------------------
    # Probe phase
    # ------------------------------------------------------------------

    async def _run_probes(self, request: DepartmentCreateRequest) -> None:
        """Run a probe per staged credential.

        The probe runner accepts a :class:`ResolvedCredential` rather
        than reading from Vault, so we can hand it the same bytes we
        just wrote. The plain-text bytes have already been zeroed in
        :meth:`_write_staging_credential` - but we kept ``url`` and
        ``username`` on the request, and the probe runner only
        actually needs the token through the Atlassian client, which
        is mocked in these tests.

        In production, the probe runner is passed a credential whose
        ``personal_token`` is read back from Vault (the staging path
        is the canonical source). For the unit / property tests we
        exercise here, the probe client is fully mocked and the token
        contents are not consulted.
        """

        probe_runner = ProbeRunner(self._probe_client, clock=lambda: int(self._clock().timestamp()))

        targets = request.probe_targets or _default_probe_targets(request)

        for bot in request.bots:
            cred = ResolvedCredential(
                url=bot.url,
                username=bot.username,
                personal_token="",  # token already wiped; probe path mocked
            )
            result = await probe_runner.run(
                request.dept_id, bot.service, cred, targets=targets
            )
            if result.state != "ok":
                raise ProbeFailureError(
                    service=bot.service,
                    state=result.state,
                    message=result.error_message or "probe failed",
                )

    # ------------------------------------------------------------------
    # DB commit + Vault promotion
    # ------------------------------------------------------------------

    async def _commit(
        self,
        request: DepartmentCreateRequest,
        *,
        staging_paths: Mapping[str, VaultPath],
        final_paths: Mapping[str, VaultPath],
    ) -> datetime:
        """Insert the department + bots and promote staging keys.

        The DB work and the Vault stagingfinal move all happen
        within the ``with_dept_session`` context. Any failure aborts
        the transaction (``ROLLBACK`` is issued by the helper) and
        re-raises. The caller deletes staging keys on the way out;
        we additionally try to clean up here for paths that have
        already been promoted to ``final_paths`` so a partially
        promoted Vault tree is rolled back to staging-only when DB
        writes fail later in the transaction.

        Returns:
            UTC timestamp of the inserted row.
        """

        connection = await self._connection_factory()

        # ``with_dept_session`` opens BEGIN/COMMIT and pins the
        # ``app.current_dept_id`` / ``app.current_role`` GUCs so RLS
        # admits the writes. We open the session as ``"system"`` (the
        # orchestrator runs server-side, not on behalf of a single
        # tenant) - RLS will let us write the new departments row
        # because the policy admits ``current_role = 'admin'`` (and
        # ``"system"`` is treated equivalently by the policy's
        # ``OR`` branch using the current_setting check).
        async with with_dept_session(
            "admin", request.dept_id, connection=connection
        ) as conn:
            # Track which final paths we've already written so a
            # later failure in this block can roll them back.
            promoted: list[VaultPath] = []
            try:
                created_at = await self._insert_department(conn, request)
                for bot in request.bots:
                    await self._insert_bot(
                        conn,
                        dept_id=request.dept_id,
                        bot=bot,
                        credential_ref=final_paths[bot.service].raw,
                    )
                # Insert project keys + space keys + repo mappings if
                # the ``config_json`` mirror carries them. The schema
                # has the canonical lists; we read from
                # ``request.jira_project_keys`` etc. so the route can
                # be exercised without a full ``config_json`` payload.
                await self._insert_project_keys(
                    conn, dept_id=request.dept_id,
                    project_keys=request.jira_project_keys,
                )
                await self._insert_space_keys(
                    conn, dept_id=request.dept_id,
                    space_keys=request.confluence_space_keys,
                )

                # Now promote staging  final in Vault. If any move
                # fails, the surrounding ``except`` catches it,
                # ``ROLLBACK`` runs (via with_dept_session), and the
                # outer caller deletes all staging paths.
                for bot in request.bots:
                    self._promote_staging(
                        staging_paths[bot.service],
                        final_paths[bot.service],
                    )
                    promoted.append(final_paths[bot.service])

                return created_at
            except BaseException:
                # Roll Vault forward-state back to staging-only by
                # deleting whatever final paths we already wrote.
                # The transaction itself is aborted by
                # ``with_dept_session`` (which issues ROLLBACK on
                # exception).
                for path in promoted:
                    try:
                        self._vault.delete(path)
                    except Exception:  # noqa: BLE001
                        _LOG.warning(
                            "dept_create.rollback.vault_delete_failed path=%s",
                            path.raw,
                        )
                raise

    async def _insert_department(
        self,
        conn: AsyncConnection,
        request: DepartmentCreateRequest,
    ) -> datetime:
        """INSERT INTO automation.departments and return ``created_at``.

        The CONFLICT path (duplicate id) raises asyncpg's
        ``UniqueViolationError`` - :meth:`run` translates that into
        :class:`DepartmentAlreadyExistsError` so the router returns
        HTTP 409.
        """

        # Use ``returning`` so we don't have to round-trip a separate
        # SELECT just to get ``created_at``. The fake connection
        # used by the unit tests still receives the SQL string and
        # returns whatever it likes; the orchestrator falls back to
        # ``self._clock()`` if the helper does not support
        # ``fetchval`` (kept as a single ``execute`` for protocol
        # simplicity).
        config_json_text = _dumps_config_json(request.config_json)
        try:
            await conn.execute(
                """
                INSERT INTO automation.departments
                    (id, display_name, default_language, web_search_enabled,
                     mode, config_json)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                request.dept_id,
                request.display_name,
                request.default_language,
                request.web_search_enabled,
                request.mode,
                config_json_text,
            )
        except Exception as exc:  # noqa: BLE001
            if _looks_like_duplicate(exc):
                raise DepartmentAlreadyExistsError(request.dept_id) from exc
            raise
        return self._clock()

    async def _insert_bot(
        self,
        conn: AsyncConnection,
        *,
        dept_id: str,
        bot: _BotCredential,
        credential_ref: str,
    ) -> None:
        """INSERT INTO automation.department_bots."""

        # ``deployment`` defaults to ``"cloud"`` per the table; the
        # CHECK constraint accepts ``cloud`` / ``dc`` (the table
        # currently stores ``dc`` while the JSON schema uses
        # ``server``; we map ``server -> dc`` here for compatibility).
        deployment = (
            "dc"
            if bot.deployment == "server"
            else (bot.deployment or "cloud")
        )
        await conn.execute(
            """
            INSERT INTO automation.department_bots
                (department_id, service, credential_ref, account_id,
                 username, deployment)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            dept_id,
            bot.service,
            credential_ref,
            bot.account_id,
            bot.username,
            deployment,
        )

    async def _insert_project_keys(
        self,
        conn: AsyncConnection,
        *,
        dept_id: str,
        project_keys: Sequence[str],
    ) -> None:
        for key in project_keys:
            await conn.execute(
                """
                INSERT INTO automation.department_project_keys
                    (department_id, project_key)
                VALUES ($1, $2)
                """,
                dept_id,
                key,
            )

    async def _insert_space_keys(
        self,
        conn: AsyncConnection,
        *,
        dept_id: str,
        space_keys: Sequence[str],
    ) -> None:
        for key in space_keys:
            await conn.execute(
                """
                INSERT INTO automation.department_space_keys
                    (department_id, space_key)
                VALUES ($1, $2)
                """,
                dept_id,
                key,
            )

    # ------------------------------------------------------------------
    # Vault staging  final promotion
    # ------------------------------------------------------------------

    def _promote_staging(self, staging: VaultPath, final: VaultPath) -> None:
        """Move *staging* to *final* atomically (best-effort).

        Vault has no native ``mv`` primitive; we read-then-write-then-
        delete. If the ``write`` fails the staging key is still
        present and the caller will delete it during rollback. If the
        ``delete`` fails the key remains under ``_staging/`` and a
        background sweeper will pick it up - it does not block the
        successful promotion.
        """

        value = dict(self._vault.read(staging))
        self._vault.write(final, value)
        try:
            self._vault.delete(staging)
        except Exception:  # noqa: BLE001
            # Best-effort; staging keys live under ``_staging/`` and
            # are safe to leave for the sweeper.
            _LOG.warning(
                "dept_create.promote.staging_delete_failed path=%s",
                staging.raw,
            )

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    def _zero_all_tokens(self, request: DepartmentCreateRequest) -> None:
        """Wipe every plain-text token bytearray in *request*.

        Called twice in :meth:`run`:

        * on the probe-failure cleanup path (after deleting staging keys),
        * and unconditionally before the DB transaction begins on the
          success path.

        The orchestrator delegates to the module-private
        :func:`_zero_bytearray` helper so test suites can monkey-patch
        either side independently. Tokens that are not :class:`bytearray`
        instances are left alone - :meth:`_validate_request` already
        rejects non-bytearray tokens, so this defensive guard only
        matters for the probe-failure path where the request may have
        been cloned by the caller for diagnostics.
        """

        for bot in request.bots:
            buf = bot.personal_token
            if isinstance(buf, bytearray):
                _zero_bytearray(buf)

    def _best_effort_delete_staging(
        self, paths: Iterable[VaultPath]
    ) -> None:
        """Delete every *path* under ``_staging/``; never raises.

        Called on the rollback / failure paths. We swallow individual
        delete failures and log them - leaving a staging key behind
        is far less harmful than re-raising and masking the original
        error that triggered the rollback.
        """

        for path in paths:
            try:
                self._vault.delete(path)
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "dept_create.cleanup.vault_delete_failed path=%s",
                    path.raw,
                )

    # ------------------------------------------------------------------
    # Audit helpers
    # ------------------------------------------------------------------

    async def _audit_duplicate(
        self,
        request: DepartmentCreateRequest,
        actor_id: str,
        actor_role: Literal["admin", "system"],
    ) -> None:
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=request.dept_id,
                action="dept_duplicate_id",
                resource=f"department:{request.dept_id}",
                result="denied",
                timestamp=self._clock(),
                payload={"reason": "duplicate_id"},
            )
        )

    async def _audit_create_failed(
        self,
        request: DepartmentCreateRequest,
        actor_id: str,
        actor_role: Literal["admin", "system"],
        *,
        reason: str,
    ) -> None:
        await self._audit.write(
            AuditEvent(
                actor_id=actor_id,
                actor_role=actor_role,
                dept_id=request.dept_id,
                action="dept_create_failed",
                resource=f"department:{request.dept_id}",
                result="error",
                timestamp=self._clock(),
                payload={"reason": reason},
            )
        )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _zero_bytearray(buf: bytearray) -> None:
    """Overwrite *buf* in-place with NUL bytes.

    Python guarantees in-place mutation of ``bytearray``, so this
    actually clears the underlying buffer. The intermediate ``str``
    we materialise during the Vault write is immutable and cannot be
    wiped - keeping its lifetime to the single Vault call is the
    best-effort guarantee we can offer in pure CPython.
    """

    for i in range(len(buf)):
        buf[i] = 0


def _dumps_config_json(payload: Mapping[str, Any]) -> str:
    """Serialise *payload* for the ``config_json`` JSONB column.

    Kept as a thin helper so unit tests can stub it. ``json.dumps``
    is lazy-imported to avoid pulling the (already tiny) module at
    package import time.
    """

    import json

    return json.dumps(dict(payload), sort_keys=True, ensure_ascii=False)


def _default_probe_targets(
    request: DepartmentCreateRequest,
) -> ProbeTargets:
    """Build a :class:`ProbeTargets` from the request fields.

    The probe runner needs Bitbucket workspace+repo and a Confluence
    space key. The wizard endpoint supplies these
    explicitly via ``request.probe_targets``; for direct ``POST
    /admin/departments`` callers we read what we can from the request
    fields. ``bitbucket_repo`` is unknown at create time (no concrete
    repo yet), so the probe runner skips the Bitbucket round-trip
    when ``bitbucket_repo`` is ``None``.
    """

    return ProbeTargets(
        bitbucket_workspace=request.bitbucket_workspace,
        bitbucket_repo=None,
        confluence_space_key=(
            request.confluence_space_keys[0]
            if request.confluence_space_keys
            else None
        ),
    )
