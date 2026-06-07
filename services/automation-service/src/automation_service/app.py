"""FastAPI application factory for the ``automation-service``.

This module owns the canonical FastAPI ``app`` object for the
automation-service. It exposes a minimal, dependency-free
``GET /healthz`` liveness probe and
keeps the legacy ``GET /readyz`` readiness contract.

Acceptance criteria:

* automation-service opens an HTTP surface and
  must expose a healthcheck endpoint that the Compose stack can probe
  (``health_endpoint = "/healthz"`` in ``services.manifest.json``).
* service-local ``.env.example`` is aligned with
  the master env-reference categories (Postgres, Vault, Temporal,
  Webhook, OIDC, …); see the sibling ``.env.example`` for the
  enumerated values.

Additional wiring attaches the webhook routers, the ``/admin/*`` endpoints
and the Temporal startup hook on top of this foundation. The factory
``create_app()`` is the single entry point so tests can spin up an
isolated app without touching module-level state.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI, Response

# ``http_shared`` ships the credential / secret redaction filter that
# every service must install on its root logger before uvicorn / FastAPI
# attach their own handlers. The helper is
# idempotent so re-importing this module under uvicorn ``--reload`` is
# safe; new handlers added later inherit redaction via the root
# logger's filter chain.
from http_shared import SecurityHeadersMiddleware, install_redaction_filter

# ``observability.TraceMiddleware`` extracts (or generates) the
# ``X-Trace-Id`` header for every inbound request and binds the
# trace_id onto the per-request :mod:`contextvars` context so
# downstream code (including the MCP-client request hook in
# :mod:`http_shared.client`) can correlate logs across services
# without threading the value through every function call
# for cross-service request tracing.
from observability import TraceMiddleware

# Re-use the existing pydantic-settings model rather than duplicating
# the schema. ``src.config`` lives one package up so the import works
# whether the service is run as ``uvicorn src.main:app`` or via the
# ``automation_service`` package import. The settings model carries
# the service env surface from
# ``services/automation-service/.env.example``.
from src.config import Settings

# Shared infrastructure collaborators the lifespan handler constructs
# during startup. The imports live at module top
# level so any wiring-time mistake (missing dependency, mistyped
# attribute) surfaces at import rather than at the first request.
from audit_logger import AuditLogger
from auth_shared.oidc import OIDCConfig, OIDCValidator
from temporal_client import TemporalClient
from vault_client import factory as vault_factory

from .atlassian import AtlassianProbeClient
from .audit_writer import AsyncpgAuditEventsWriter
from .inbound.common import (
    InboundContext,
    InboundDeptResolver,
    SlackSignatureVerifier,
    utc_now,
    verify_slack_signature,
)
from .processed_events import ProcessedEventsRepo
from vault_client import VaultPath

# Per-router ``*EndpointDeps`` containers built by the ``_wire_*``
# helpers below. ``RepoSyncEndpointDeps`` ships from
# :mod:`automation_service.api.repo_sync`; the production wiring also
# depends on :class:`temporal_shared.RepoMapping` to fold the
# departments-registry rows into the dataclass shape the diff helper
# expects (the dry-run / apply paths both consume tuples of
# ``RepoMapping`` instances rather than raw DB rows or JSON).
from .api import (
    CancelEndpointDeps,
    IssueRef,
    PoReviewEndpointDeps,
    RepoSyncEndpointDeps,
    WebhooksEndpointDeps,
)
from temporal_shared import (
    InvalidWorkflowIdError,
    RepoMapping,
    parse_workflow_id,
)

# Webhook filter chain - the production wiring builds a
# single :class:`WebhookFilterChain` from the shared Vault / DB /
# audit collaborators and folds it into the
# :class:`WebhooksEndpointDeps` container the router pulls off
# ``app.state.webhooks`` at request time.
from .webhook_filters import WebhookFilterChain
from .processed_events import ProcessedEventsRepo as _ProcessedEventsRepo
from vault_client import verify_webhook_hmac

# Webhook v2 / webhooks_handlers - the Jira issue webhook
# ``POST /webhooks/jira/issue_*`` handlers pull their collaborators
# off ``app.state.webhook_v2`` (a :class:`WebhookContext`).
from .webhooks_handlers import (
    DeptResolver as WebhookV2DeptResolver,
    JiraCommenter as WebhookV2JiraCommenter,
    WebhookContext,
)


class _PoolConnectionLease:
    """Asyncpg connection wrapper released by db_shared.with_dept_session."""

    __db_shared_release_on_exit__ = True

    def __init__(self, pool: Any, connection: Any) -> None:
        self._pool = pool
        self._connection = connection
        self._released = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._connection.execute(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._connection.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._connection.fetch(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._connection.fetchval(query, *args)

    async def __db_shared_release__(self) -> None:
        if self._released:
            return
        self._released = True
        await self._pool.release(self._connection)

# Diff-summary cache repo - the PO Review router consumes a
# :class:`DiffSummaryProvider` (typically the asyncpg-backed
# :class:`DiffSummaryCacheRepo`) to serve cached LLM diff summaries.
from .diff_summary_cache import DiffSummaryCacheRepo

# Per-router *EndpointDeps types and their orchestrators.  The
# ``_wire_*`` helpers below build one container per router from the
# shared singletons constructed in the lifespan handler; the imports
# live at module top level so a missing ``services.*`` / ``routers.*``
# package surfaces at import time rather than during the first
# request.  ``routers`` and ``services`` are sibling top-level
# packages under ``services/automation-service/src`` (see
# ``pyproject.toml`` -> ``[tool.hatch.build.targets.wheel].packages``)
# and resolve the same way the routers themselves do (e.g.
# ``src/routers/dept_credentials.py`` does
# ``from services.dept_credential_service import ...``).
from routers.dept_credentials import DeptCredentialEndpointDeps
from services.dept_credential_service import DeptCredentialService

# ``AdminEndpointDeps`` / ``DepartmentCreateOrchestrator`` -
# the production wiring for ``app.state.admin`` builds the orchestrator
# from the shared Vault / DB / probe / audit collaborators and folds it
# into the dataclass shape the admin router pulls off ``app.state.admin``
# at request time. The orchestrator's ``clock`` argument is left at its
# default (``datetime.now(timezone.utc)``) and the dataclass's ``clock``
# field defaults to ``None`` so the lifespan call site stays minimal.
from .admin.dept_create import DepartmentCreateOrchestrator
from .admin.router import AdminEndpointDeps

__all__ = ["app", "create_app", "lifespan", "wire_webhook_pipeline"]


_LOG = logging.getLogger(__name__)


async def _close_quietly(
    name: str,
    coro_factory: Callable[[], Awaitable[None]],
) -> None:
    """Best-effort close for an owned resource during lifespan shutdown.

    Awaits ``coro_factory()`` inside a single ``try/except`` block,
    logs a WARNING with the resource ``name`` and full traceback on
    any failure, and **never re-raises**. This is the shutdown
    primitive the lifespan handler uses to release the asyncpg pool,
    the shared :class:`httpx.AsyncClient` and the :class:`TemporalClient`
    without one resource's close error blocking the others
    during shutdown.

    A missing ``close`` attribute (which raises :class:`AttributeError`
    when ``coro_factory`` is invoked - for example because the
    ``TemporalClient`` wrapper does not expose ``close``) is treated as
    a successful "no-close": the call is logged at WARNING and shutdown
    continues. This keeps the lifespan tolerant of resource wrappers
    that do not require an explicit close (see the resource classification
    "Vault / Postgres / Temporal classification" table).

    Parameters
    ----------
    name:
        Human-readable resource identifier (e.g. ``"pool"``,
        ``"http_client"``, ``"temporal"``) used solely for the log
        message; does not affect control flow.
    coro_factory:
        Zero-argument callable that returns the awaitable performing
        the close. Pass the bound method itself
        (e.g. ``pool.close``), not the awaitable it produces, so the
        coroutine is created lazily inside the ``try`` block and
        ``AttributeError`` raised by the lookup is captured here.
    """

    try:
        await coro_factory()
    except Exception:  # noqa: BLE001 - intentional broad catch for shutdown
        _LOG.warning(
            "shutdown.close_failed resource=%s",
            name,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Inbound channel collaborators
#
# These adapter classes are the production implementations the
# ``_wire_inbound`` helper builds during lifespan startup; they are kept
# at module-private scope because the ``InboundContext`` shape exposed
# in :mod:`automation_service.inbound.common` is the public contract,
# not the concrete adapter types here. Test fixtures continue to inject
# their own duck-typed fakes against the protocols on
# ``app.state.inbound`` directly.
# ---------------------------------------------------------------------------


class _AsyncpgInboundDeptResolver:
    """Production :class:`InboundDeptResolver` over an asyncpg pool.

    The resolver holds the shared :class:`asyncpg.Pool` so future
    schema additions (Slack workspace → dept mapping, inbound email
    address → dept mapping) can be implemented without re-plumbing the
    lifespan handler. Until those mapping tables exist the resolver
    returns ``None`` for every lookup; the inbound route translates an
    unresolved dept into a 400 ``inbound_dept_unresolved`` response,
    which is the documented behaviour for an unmapped channel signal
    for an unmapped channel signal.

    The class implements the runtime-checkable
    :class:`InboundDeptResolver` ``Protocol`` structurally - the
    resolver does not inherit from it, since :func:`isinstance` against
    a runtime-checkable protocol verifies attribute presence at the
    call site (the inbound route never inspects the resolver type, it
    just awaits its async methods).
    """

    __slots__ = ("_pool",)

    def __init__(self, *, pool: object) -> None:
        self._pool = pool

    async def resolve_for_slack(
        self, *, team_id: str | None, channel_id: str | None
    ) -> str | None:
        """Return the dept id for a Slack ``team_id``/``channel_id``.

        Returns ``None`` until the Slack workspace mapping table is
        added to the schema; the inbound route surfaces this as a
        400 ``inbound_dept_unresolved`` response.
        """

        # Pool reference held for forward compatibility - see class
        # docstring. The lookup is a no-op until the mapping table
        # exists; structuring the call site this way means the
        # production wiring already routes through the shared pool
        # the day the mapping table ships.
        return None

    async def resolve_for_email(self, *, recipient: str) -> str | None:
        """Return the dept id for an inbound email *recipient*.

        Returns ``None`` until the inbound-email mapping table is
        added to the schema; mirrors :meth:`resolve_for_slack`.
        """

        return None


class _VaultBackedSlackSignatureVerifier:
    """Production :class:`SlackSignatureVerifier` over a Vault client.

    Resolves the per-dept Slack signing secret from Vault at request
    time and verifies the inbound payload via
    :func:`verify_slack_signature`. The secret is fetched lazily on
    every call so an operator can rotate
    ``vault:notifications/slack_inbound/<dept_id>`` without restarting
    the service.

    Vault paths follow the convention documented in
    ``services/automation-service/.env.example`` - the active secret
    lives at ``vault:notifications/slack_inbound/<dept_id>`` (or
    ``.../_default`` for the URL-verification handshake) under the
    flat ``"secret"`` key, mirroring the
    ``vault:webhooks/<provider>/<dept_id>`` shape the other webhook
    handlers consume.

    Verification returns ``False`` for any structural failure
    (missing secret, malformed payload at the Vault path, missing
    ``"secret"`` key) so the inbound route emits a single
    ``inbound_slack_hmac_failed`` audit event regardless of root
    cause - see :func:`verify_slack_signature` for the rationale.
    """

    __slots__ = ("_vault",)

    #: Vault path prefix for inbound Slack signing secrets. Matches
    #: ``SLACK_INBOUND_SIGNING_SECRET_VAULT_PREFIX`` in the
    #: per-service ``.env.example`` and the docstring on
    #: :class:`SlackSignatureVerifier`.
    _PREFIX: str = "vault:notifications/slack_inbound"

    #: Path segment used when ``dept_id`` is ``None`` (the Slack
    #: URL-verification handshake runs before the dept resolver).
    _DEFAULT_SEGMENT: str = "_default"

    #: Flat KV key holding the signing-secret bytes. Matches the
    #: ``{"secret": "<value>"}`` shape used by
    #: :func:`vault_client.verify_webhook_hmac` so dev / prod backends
    #: can share rotation tooling.
    _SECRET_KEY: str = "secret"

    def __init__(self, *, vault: object) -> None:
        self._vault = vault

    async def verify(
        self,
        *,
        dept_id: str | None,
        timestamp: str,
        raw_body: bytes,
        signature: str,
        now: datetime,
    ) -> bool:
        """Return ``True`` iff *signature* is valid for the dept."""

        secret = self._read_secret(dept_id)
        if secret is None:
            return False
        return verify_slack_signature(
            secret=secret,
            timestamp=timestamp,
            raw_body=raw_body,
            signature=signature,
            now=now,
        )

    # ---- internals -----------------------------------------------------

    def _read_secret(self, dept_id: str | None) -> bytes | None:
        """Resolve the signing secret bytes from Vault.

        Returns ``None`` (the verifier's ``False`` branch) when the
        path does not exist, when the payload is missing the
        ``"secret"`` key, or when the Vault backend raises any error.
        Distinct failure modes are deliberately collapsed onto the
        same return value so the caller emits a single audit shape.
        """

        segment = dept_id if dept_id else self._DEFAULT_SEGMENT
        try:
            path = VaultPath.parse(f"{self._PREFIX}/{segment}")
        except ValueError:
            # ``dept_id`` should always satisfy the dept-id pattern,
            # but a malformed value (e.g. coming from a hand-built
            # mapping) must not crash the request handler.
            return None
        try:
            payload = self._vault.read(path)
        except KeyError:
            return None
        except Exception:  # noqa: BLE001 - Vault adapter raised
            _LOG.warning(
                "inbound.slack_secret_read_failed dept_id=%s",
                dept_id,
                exc_info=True,
            )
            return None
        secret_str = payload.get(self._SECRET_KEY)
        if not isinstance(secret_str, str) or not secret_str:
            return None
        return secret_str.encode("utf-8")


# ---------------------------------------------------------------------------
# Repo-sync collaborators
#
# The :func:`_wire_repo_sync` helper builds two collaborators from the
# shared infrastructure: an asyncpg-backed departments-registry adapter
# that satisfies :class:`SupportsDepartmentsRepo`, and a Bitbucket-scan
# callable that will route through ``mcp_client.atlassian_client
# .bitbucket_list_repos``. The MCP-routed helper follows the contract
# documented on :class:`automation_service.atlassian.AtlassianProbeClient`.
# Until that transport is wired, the production callable raises
# :class:`NotImplementedError` with a clear pointer at the missing
# wiring; tests inject their own ``BitbucketRepoScanner`` fakes via
# the ``app.state.repo_sync`` override path so the absence of the MCP
# routing never blocks the test suite or the rest of lifespan startup.
# ---------------------------------------------------------------------------


class _AsyncpgDepartmentsRepo:
    """Production :class:`SupportsDepartmentsRepo` over an asyncpg pool.

    Reads and writes the ``automation.repo_mappings`` table - the
    canonical Postgres mirror of the dept's ``repo_mappings`` array
    documented in ``config/departments.json``. The ``slug`` field
    consumed by the diff helper is sourced from
    ``bitbucket_repo`` (the canonical Bitbucket repo slug as it
    appears in the URL), and the ``name`` field is set to the same
    value because the schema does not currently carry a separate
    human-readable name column. The :func:`_build_new_mapping_list`
    helper inside the router preserves the operator-customised
    ``name`` for entries that survive a sync; rows we add via the
    ``apply=true`` mode use the slug as the placeholder name so the
    resulting JSON document stays valid.

    The adapter is kept module-private and minimal because the
    :class:`SupportsDepartmentsRepo` protocol pins the public contract;
    test fixtures inject their own duck-typed fakes against the
    protocol on ``app.state.repo_sync`` directly. The atomic-replace
    apply-mode atomic replace behaviour is achieved
    by running the DELETE + INSERT inside a single transaction so a
    concurrent reader either sees the old list or the new list, never
    a partially-rewritten one.

    Attributes
    ----------
    _pool:
        Shared :class:`asyncpg.Pool` owned by the lifespan handler.
        Held by reference; the adapter does not own the pool's
        lifecycle.
    """

    __slots__ = ("_pool",)

    def __init__(self, *, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def list_repo_mappings(
        self, dept_id: str
    ) -> tuple[RepoMapping, ...]:
        """Return the dept's current ``repo_mappings`` as a tuple.

        Reads ``automation.repo_mappings`` filtered by
        ``department_id`` and projects each row into a
        :class:`temporal_shared.RepoMapping` instance. The result is
        ordered by ``bitbucket_repo`` so two consecutive reads over
        the same data produce an identical tuple - the diff helper
        does not require this, but it keeps the audit-row payload
        deterministic across replays.
        """

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT bitbucket_repo
                FROM automation.repo_mappings
                WHERE department_id = $1
                ORDER BY bitbucket_repo
                """,
                dept_id,
            )
        return tuple(
            RepoMapping(name=str(r["bitbucket_repo"]), slug=str(r["bitbucket_repo"]))
            for r in rows
        )

    async def update_repo_mappings(
        self, dept_id: str, new_mappings: tuple[RepoMapping, ...]
    ) -> None:
        """Atomically replace the dept's ``repo_mappings`` rows.

        Runs the DELETE + INSERT inside a single transaction so a
        concurrent reader either sees the pre-sync list or the
        post-sync list, never a partially rewritten one
        ("atomic replace").

        The ``bitbucket_workspace``, ``jira_project_key`` and
        ``default_branch`` columns are not part of the diff helper's
        scope; the adapter inserts the new rows with the slug echoed
        into ``bitbucket_workspace`` + ``bitbucket_repo`` and the
        ``jira_project_key`` defaulting to the slug as well. A future
        schema migration will move these columns into a richer
        per-mapping shape; until
        then the values are deliberately conservative so an apply-mode
        run produces a syntactically valid table even when the operator
        has not yet edited the per-row metadata.
        """

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM automation.repo_mappings
                    WHERE department_id = $1
                    """,
                    dept_id,
                )
                if new_mappings:
                    await conn.executemany(
                        """
                        INSERT INTO automation.repo_mappings
                            (department_id, bitbucket_workspace,
                             bitbucket_repo, jira_project_key)
                        VALUES ($1, $2, $3, $4)
                        """,
                        [
                            (dept_id, m.slug, m.slug, m.slug)
                            for m in new_mappings
                        ],
                    )


async def _bitbucket_list_repos_via_mcp(dept_id: str) -> list[dict[str, str]]:
    """Stand-in for ``mcp_client.atlassian_client.bitbucket_list_repos``.

    The production ``bitbucket_scanner`` routes through
    ``mcp_client.atlassian_client.bitbucket_list_repos`` - a helper
    that talks to the ``atlassian_unified`` MCP service to enumerate
    the dept's Bitbucket workspace. The HTTP wiring follows the same
    contract :class:`automation_service.atlassian.AtlassianProbeClient`
    documents for its Bitbucket / Jira / Confluence methods.

    Until that lands, calling this helper raises
    :class:`NotImplementedError` with a pointer at the missing wiring
    so any caller that mistakenly reaches the production scanner in
    this build fails loudly rather than silently returning an empty
    list (which would make the diff helper silently mark every
    existing mapping as ``removed``). Tests bypass this entirely by
    pre-populating ``app.state.repo_sync`` with a hand-rolled
    :class:`BitbucketRepoScanner` fake.

    Args:
        dept_id: Department identifier; used by the future MCP wiring
            to resolve the per-dept Bitbucket workspace + bot
            credentials. Captured here so the signature exactly
            matches the :class:`BitbucketRepoScanner` Protocol the
            ``RepoSyncEndpointDeps`` container declares.

    Raises:
        NotImplementedError: Always, until the MCP transport is wired.
    """

    raise NotImplementedError(
        "bitbucket_list_repos MCP routing is not wired in this build "
        f"(dept_id={dept_id!r}). Until the HTTP wiring is available, "
        "production traffic "
        "must NOT exercise the repo-mappings sync endpoint. Tests "
        "inject a hand-rolled BitbucketRepoScanner via "
        "app.state.repo_sync override."
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Production startup / shutdown handler for the automation-service.

    Constructs the shared infrastructure for the service: asyncpg
    pool, Vault client, audit logger, shared
    HTTP client, Temporal client, Atlassian probe client, OIDC
    validator, processed-events repo) once at startup, stash every
    object on :attr:`app.state` so the per-router ``_wire_*`` helpers
    can fan them out into ``*EndpointDeps`` containers, and
    release the pool / HTTP client / Temporal client cleanly on
    shutdown.

    Startup is split into construction and state publication:

    * **Phase A - fail-fast construction.** The handler builds every
      shared singleton in dependency order. Each successful step
      appends its closer to a local ``cleanup`` list; any exception
      walks the list in reverse under :func:`_close_quietly` and
      re-raises so uvicorn aborts startup.
    * **Phase B - stash on ``app.state``.** Once construction
      succeeds, the handler parks every singleton on
      :attr:`app.state` so shutdown can reach them and the per-router
      wiring helpers can wire
      ``*EndpointDeps`` containers from them.

    Phase C (per-router wiring + ``wire_webhook_pipeline``) and
    Phase D (the ``yield`` plus reverse-order shutdown) are added by
    the shutdown block. Until then the handler ends with a temporary ``yield``
    so the function remains a valid asynccontextmanager generator -
    registering it on FastAPI is the application factory's responsibility.

    Args:
        app: The FastAPI application produced by :func:`create_app`.
            Must already carry the resolved :class:`Settings` on
            ``app.state.settings`` (set by :func:`create_app` per
            application setup).

    Yields:
        ``None``: control returns to FastAPI for traffic serving.
            The yield is a placeholder until the shutdown block wraps it in
            the production ``try / finally``.
    """

    settings: Settings = app.state.settings

    # Each successful construction step appends its closer to this
    # list. On failure we walk the list in reverse via
    # ``_close_quietly`` so a half-built process never escapes.
    cleanup: list[Callable[[], Awaitable[None]]] = []

    try:
        # 1. Shared HTTP client - used by Vault, Atlassian probe,
        #    OIDC validator and any future outbound HTTP collaborator.
        http_client = httpx.AsyncClient(timeout=10.0)
        cleanup.append(http_client.aclose)

        # 2. Asyncpg pool - sized for the per-process request rate
        #    target (min=1 / max=8) with a 10s
        #    command timeout so a stuck statement never wedges a
        #    request worker indefinitely.
        pool = await asyncpg.create_pool(
            dsn=settings.postgres_dsn,
            min_size=1,
            max_size=8,
            command_timeout=10.0,
        )
        cleanup.append(pool.close)

        # 3. Vault client - env-driven backend selection
        #    (``VAULT_BACKEND=hashicorp`` vs ``local-dev``). The
        #    factory is synchronous and the resulting client does
        #    not require an explicit close, so no cleanup entry is
        #    appended (the Hashicorp backend reuses the shared HTTP
        #    client which is closed above).
        vault = vault_factory.make_client(os.environ)

        # 4. Audit logger - wraps the asyncpg-backed events writer
        #    so the application-layer ``actor_role`` invariant
        #    fires before any SQL runs. No
        #    explicit close: the logger holds the pool by reference
        #    and the pool is closed via the entry already on the
        #    cleanup stack.
        audit_logger = AuditLogger(writer=AsyncpgAuditEventsWriter(pool=pool))

        # 5. Temporal client - connects lazily to the cluster.
        #    ``connect()`` is awaited here so a Temporal outage
        #    aborts startup (fail-fast) instead of surfacing as a
        #    500 on the first webhook.  The ``TemporalClient``
        #    wrapper does not currently expose a ``close``
        #    coroutine; ``_close_quietly`` tolerates that case by
        #    catching the ``AttributeError`` raised at the factory
        #    call site (see the unit-test contract pinned by
        #    ``test_lifespan_close_quietly``).  Wrapping the
        #    attribute lookup in a lambda defers it to shutdown
        #    time so the AttributeError surfaces inside the helper
        #    rather than at append time.
        temporal = TemporalClient(
            host=settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
        await temporal.connect()

        async def _close_temporal() -> None:
            close_fn = getattr(temporal, "close", None)
            if close_fn is None:
                return
            await close_fn()

        cleanup.append(_close_temporal)

        # 6. Atlassian probe client - concrete shell built from the
        #    shared HTTP client + MCP routing metadata (see
        #    :mod:`automation_service.atlassian` for the wiring
        #    contract).
        probe_client = AtlassianProbeClient(
            http_client,
            settings.mcp_base_url,
            settings.client_source,
        )

        # 7. OIDC validator - env-driven configuration honouring
        #    ``AUTH_PROVIDER`` (``oidc`` vs ``local``).  The
        #    validator does not own any resource that needs
        #    closing.
        oidc_validator = OIDCValidator(OIDCConfig.from_env(os.environ))

        # 8. Processed events repo - webhook ``delivery_id`` ledger
        #    backed by the shared pool.
        processed_events = ProcessedEventsRepo(pool=pool)

        # 9. Connection-factory closure - each call returns a fresh
        #    pool-acquired connection so the credential / department
        #    orchestrators can run ``with_dept_session(...)`` blocks
        #    against pool-managed connections.  Releasing the
        #    connection back to the pool is the caller's
        #    responsibility.
        async def connection_factory() -> object:
            return _PoolConnectionLease(pool, await pool.acquire())

    except BaseException:
        # Walk the cleanup stack in reverse so the most-recently-
        # constructed resource is closed first.  Each closer runs
        # under ``_close_quietly`` so one failure cannot block the
        # rest, and the original exception is re-raised once the
        # stack has been drained.
        for closer in reversed(cleanup):
            await _close_quietly(getattr(closer, "__qualname__", "resource"), closer)
        raise

    # Phase B - stash every shared singleton on ``app.state`` so the
    # per-router wiring helpers can fan them out into
    # ``*EndpointDeps`` containers without re-reading any environment
    # variable, and shutdown can reach them.
    app.state.pool = pool
    app.state.http_client = http_client
    app.state.vault = vault
    app.state.audit_logger = audit_logger
    app.state.temporal = temporal
    app.state.probe_client = probe_client
    app.state.oidc_validator = oidc_validator
    app.state.processed_events = processed_events
    app.state.connection_factory = connection_factory

    # ------------------------------------------------------------------
    # Phase C - fan the shared singletons out into per-router
    # ``*EndpointDeps`` containers and stash them on ``app.state.<slot>``.
    #
    # Each ``_wire_*`` helper opens with the "skip if slot already set"
    # guard so test code that pre-populates ``app.state.<slot>`` before
    # entering the lifespan keeps observing its own container
    # The production construction bodies honour the guard
    # and otherwise return without populating the slot, so the lifespan
    # remains importable / runnable without losing the override
    # contract.
    #
    # ``wire_webhook_pipeline`` is the existing helper from this module
    # and already builds the
    # Event_Dedup → Loop_Guard → Webhook_Dispatcher chain from the
    # shared collaborators, so we call it directly here.
    # ------------------------------------------------------------------
    _wire_dept_credentials(
        app,
        vault=vault,
        pool=pool,
        audit_logger=audit_logger,
        probe_client=probe_client,
        connection_factory=connection_factory,
    )
    _wire_admin(
        app,
        vault=vault,
        pool=pool,
        audit_logger=audit_logger,
        probe_client=probe_client,
        temporal=temporal,
        connection_factory=connection_factory,
    )
    _wire_webhooks(
        app,
        pool=pool,
        vault=vault,
        audit_logger=audit_logger,
        processed_events=processed_events,
        temporal=temporal,
    )
    _wire_cancel(
        app,
        pool=pool,
        audit_logger=audit_logger,
        oidc_validator=oidc_validator,
        temporal=temporal,
    )
    _wire_repo_sync(
        app,
        pool=pool,
        audit_logger=audit_logger,
        oidc_validator=oidc_validator,
        http_client=http_client,
    )
    _wire_po_review(
        app,
        pool=pool,
        audit_logger=audit_logger,
        oidc_validator=oidc_validator,
        vault=vault,
        probe_client=probe_client,
    )
    _wire_inbound(
        app,
        pool=pool,
        vault=vault,
        audit_logger=audit_logger,
        temporal=temporal,
    )
    _wire_webhook_v2(
        app,
        pool=pool,
        vault=vault,
        audit_logger=audit_logger,
        temporal=temporal,
    )

    # The webhook pipeline helper has its own idempotency story (it
    # always overwrites ``app.state.webhook_pipeline``); the
    # "test override wins" property for that slot is enforced by the
    # caller; the slot name is parameterized
    # set ``{..., "webhook_pipeline"}`` and the test pre-populates
    # the slot before entering the lifespan, so we mirror the guard
    # here by skipping the call when the slot is already set.
    if getattr(app.state, "webhook_pipeline", None) is None:
        wire_webhook_pipeline(
            app,
            db=pool,
            temporal=temporal,
            audit_logger=audit_logger,
            vault=vault,
        )

    # ------------------------------------------------------------------
    # Phase D - yield to FastAPI for traffic serving, then close every
    # owned resource in reverse construction order under
    # ``_close_quietly``. The ``finally`` block must never re-raise so
    # one resource's close error cannot block the others
    # for the resource teardown sequence.
    # ------------------------------------------------------------------
    try:
        yield
    finally:
        # Close in reverse order: temporal first (most-recently
        # connected), then the shared HTTP client, then the asyncpg
        # pool. ``_close_quietly`` swallows + logs each failure so the
        # subsequent close still runs.
        await _close_quietly("temporal", _close_temporal)
        await _close_quietly("http_client", http_client.aclose)
        await _close_quietly("pool", pool.close)


# ----------------------------------------------------------------------
# Per-router ``_wire_*`` helpers.
#
# Each helper:
#
# 1. Honours the "skip if slot already set" override contract so
#    test override behavior holds: tests that pre-populate
#    ``app.state.<slot>`` before entering the lifespan keep observing
#    their own container.
# 2. Otherwise returns without populating the slot until the matching
#    ``*EndpointDeps`` construction can be built against the existing
#    dataclass shape; the lifespan signature does not change.
#
# The helpers are intentionally module-private (leading underscore) and
# accept their collaborators as keyword-only arguments so the lifespan
# call sites above stay self-documenting and the eventual production
# bodies can drop dependencies they no longer need without breaking
# call-site compatibility.
# ----------------------------------------------------------------------


def _wire_dept_credentials(
    app: FastAPI,
    *,
    vault: object,
    pool: object,
    audit_logger: object,
    probe_client: object,
    connection_factory: Callable[[], Awaitable[object]],
) -> None:
    """Wire ``app.state.dept_credentials``.

    Builds the :class:`DeptCredentialEndpointDeps` container unless a
    test has already installed an override.
    """

    if getattr(app.state, "dept_credentials", None) is not None:
        return  # test override wins
    service = DeptCredentialService(
        vault=vault,
        connection_factory=connection_factory,
        probe_client=probe_client,
        audit_logger=audit_logger,
    )
    app.state.dept_credentials = DeptCredentialEndpointDeps(
        service=service,
        connection_factory=connection_factory,
        audit_logger=audit_logger,
    )


def _wire_admin(
    app: FastAPI,
    *,
    vault: object,
    pool: object,
    audit_logger: object,
    probe_client: object,
    temporal: object,
    connection_factory: Callable[[], Awaitable[object]],
) -> None:
    """Wire ``app.state.admin`` with the production ``AdminEndpointDeps``.

    Builds the :class:`DepartmentCreateOrchestrator` from the shared
    Vault client, asyncpg-backed connection factory, Atlassian probe
    client and audit logger, then folds it into the
    :class:`AdminEndpointDeps` dataclass the admin router pulls from
    ``app.state.admin`` at request time. The shared ``temporal`` client
    is forwarded as ``temporal_client`` (used by the disable endpoint
    to signal long-running workflows); the ``clock`` field is left at
    its dataclass default so the orchestrator's own
    ``datetime.now(timezone.utc)`` clock is used.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.admin`` (eg. via :class:`TestClient`
    fixtures injecting a hand-rolled :class:`AdminEndpointDeps`) keeps
    observing its own container after lifespan startup completes
    for test overrides.
    """

    if getattr(app.state, "admin", None) is not None:
        return  # test override wins
    orchestrator = DepartmentCreateOrchestrator(
        vault=vault,
        connection_factory=connection_factory,
        probe_client=probe_client,
        audit_logger=audit_logger,
    )
    app.state.admin = AdminEndpointDeps(
        orchestrator=orchestrator,
        vault=vault,
        audit_logger=audit_logger,
        connection_factory=connection_factory,
        probe_client=probe_client,
        temporal_client=temporal,
    )


def _wire_webhooks(
    app: FastAPI,
    *,
    pool: object,
    vault: object,
    audit_logger: object,
    processed_events: object,
    temporal: object,
) -> None:
    """Wire ``app.state.webhooks`` with the production ``WebhooksEndpointDeps``.

    Builds the :class:`WebhookFilterChain` once from the shared
    Vault HMAC verifier + dept resolver + ``processed_events``
    probe + mention-set / iter-count / reporter lookups + default
    burst window, and folds it with the shared
    :class:`ProcessedEventsRepo` + Temporal client + audit logger
    into the :class:`WebhooksEndpointDeps` container the webhooks
    router pulls from ``app.state.webhooks`` at request time.

    The chain's collaborators are intentionally minimal until the
    per-dept secret cache and the iteration / mention tracking tables
    are available. The ``verify_hmac``
    closure reads the event's stashed ``(body, signature)`` payload
    and dispatches to :func:`vault_client.verify_webhook_hmac`
    against the shared :class:`VaultClient` - but only when the
    event carries a resolvable dept slug (``project_key`` /
    ``repo_slug``); otherwise it returns ``False`` so the chain
    raises :class:`WebhookHmacInvalidError` and the router maps to
    HTTP 401.  The ``resolve_dept`` closure currently returns
    ``None`` until the dept-by-project / dept-by-repo Postgres
    snapshot lands; the chain then raises
    :class:`WebhookDeptUnresolvedError` and the router maps to 400.
    The remaining synchronous callbacks (mention-set / iter-count /
    reporter / bot account id snapshot) all return empty / zero
    defaults so the chain's mid-chain stages stay conservative
    (loop guard / first-iter exception will not falsely fire while
    the lookup table is empty).

    Tests bypass this entirely by pre-populating
    ``app.state.webhooks`` with a hand-rolled
    :class:`WebhooksEndpointDeps` carrying their own chain + fakes
    before entering the lifespan.
    """

    if getattr(app.state, "webhooks", None) is not None:
        return  # test override wins

    # Import locally so the chain's HMAC-input extractor stays a
    # private helper of :mod:`automation_service.api.webhooks`.
    from .api.webhooks import _extract_hmac_inputs

    def _verify_hmac(event: Any) -> bool:
        """Verify the HMAC signature stashed on the event.

        Extracts ``(body, signature)`` via the same private envelope
        the production endpoint stamps onto the raw payload, then
        delegates to :func:`vault_client.verify_webhook_hmac` against
        the shared :class:`VaultClient`.  Returns ``False`` when the
        event carries no resolvable dept slug or the signature does
        not match the per-dept secret in Vault.
        """

        body, signature = _extract_hmac_inputs(event)
        if not body or not signature:
            return False
        dept_slug = event.project_key or event.repo_slug
        if not isinstance(dept_slug, str) or not dept_slug:
            return False
        try:
            return verify_webhook_hmac(
                vault,
                event.provider,
                dept_slug,
                body,
                signature,
                utc_now(),
            )
        except Exception:  # noqa: BLE001 - verifier surfaces False on any failure
            _LOG.warning(
                "webhook.hmac_verify_failed provider=%s",
                event.provider,
                exc_info=True,
            )
            return False

    def _resolve_dept(event: Any) -> str | None:
        """Resolve the event to a ``dept_id`` (placeholder).

        Returns ``None`` until the dept-by-project / dept-by-repo
        snapshot ships.  The chain then raises
        :class:`WebhookDeptUnresolvedError` and the router maps to
        HTTP 400 ``"webhook_dept_unresolved"`` - the documented
        behaviour for an unresolved event.
        Pool held by reference for forward compatibility.
        """

        _ = pool, event
        return None

    def _bot_account_ids() -> frozenset[str]:
        """Return the dept-wide bot account id snapshot (placeholder)."""

        return frozenset()

    def _is_processed(delivery_id: str) -> bool:
        """Synchronous probe for ``automation.processed_events``.

        The chain's replay-dedup stage runs synchronously; the real
        idempotency guarantee comes from the subsequent
        :meth:`ProcessedEventsRepo.claim` call on the dispatch path,
        which is async and atomic.  Returning ``False`` defers all
        dedup work to that claim - consistent with the test fixtures
        in ``tests/unit/test_webhooks.py``.
        """

        _ = delivery_id
        return False

    def _mention_set_for(issue_key: str) -> frozenset[str]:
        """Per-issue mention set (placeholder)."""

        _ = issue_key
        return frozenset()

    def _iter_count_for(issue_key: str) -> int:
        """Per-issue iteration counter (placeholder - defaults to 0)."""

        _ = issue_key
        return 0

    def _reporter_for(issue_key: str) -> str:
        """Per-issue reporter resolver (placeholder - empty string)."""

        _ = issue_key
        return ""

    chain = WebhookFilterChain(
        verify_hmac=_verify_hmac,
        resolve_dept=_resolve_dept,
        bot_account_ids=_bot_account_ids,
        is_processed=_is_processed,
        mention_set_for=_mention_set_for,
        iter_count_for=_iter_count_for,
        reporter_for=_reporter_for,
    )
    app.state.webhooks = WebhooksEndpointDeps(
        chain=chain,
        processed_events=processed_events,
        workflow_client=temporal,
        audit_logger=audit_logger,
    )


def _wire_cancel(
    app: FastAPI,
    *,
    pool: object,
    audit_logger: object,
    oidc_validator: object,
    temporal: object,
) -> None:
    """Wire ``app.state.cancel`` with the production ``CancelEndpointDeps``.

    Builds the ``issue_lookup`` closure over the shared asyncpg
    ``pool`` and folds it with the shared OIDC validator + Temporal
    client + audit logger into the
    :class:`CancelEndpointDeps` dataclass the cancel router pulls
    from ``app.state.cancel`` at request time.

    The closure parses the workflow_id via
    :func:`temporal_shared.parse_workflow_id` and returns ``None``
    when the workflow_id does not match the canonical Jira /
    Bitbucket format - the router translates that into a HTTP 404.
    The reporter / past_assignees lookup against the live Jira
    issue is not wired here; until that lookup is available the
    closure returns ``None`` for every
    well-formed workflow_id so the endpoint emits a 404 rather than
    a half-built ``IssueRef``. Tests bypass this entirely by
    pre-populating ``app.state.cancel`` with a hand-rolled
    :class:`CancelEndpointDeps`.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.cancel`` keeps observing its own
    container after lifespan startup completes.
    """

    if getattr(app.state, "cancel", None) is not None:
        return  # test override wins

    async def _issue_lookup(workflow_id: str) -> IssueRef | None:
        """Resolve a workflow_id to its underlying :class:`IssueRef`.

        Closes over the shared asyncpg ``pool`` (held by reference
        so future schema additions - for example
        ``automation.workflow_issues`` - can be wired without
        re-plumbing the lifespan handler).  The current schema does
        not persist a reporter / past_assignees mapping, so the
        lookup short-circuits to ``None`` after validating the
        workflow_id format.

        Returning ``None`` causes the cancel endpoint to respond
        with HTTP 404 ``"no issue found for workflow_id=..."`` -
        the documented behaviour for an unresolved workflow id.
        """

        try:
            parse_workflow_id(workflow_id)
        except InvalidWorkflowIdError:
            return None
        # Pool reference held for forward compatibility - the lookup
        # is a no-op until the issue-mapping table ships.  Structuring
        # the call site this way means the production wiring already
        # routes through the shared pool the day the table exists.
        _ = pool
        return None

    app.state.cancel = CancelEndpointDeps(
        oidc_validator=oidc_validator,
        issue_lookup=_issue_lookup,
        temporal_client=temporal,
        audit_logger=audit_logger,
        clock=utc_now,
    )


def _wire_repo_sync(
    app: FastAPI,
    *,
    pool: object,
    audit_logger: object,
    oidc_validator: object,
    http_client: object,
) -> None:
    """Wire ``app.state.repo_sync`` with the production ``RepoSyncEndpointDeps``.

    Builds the two collaborators the
    :class:`automation_service.api.repo_sync.RepoSyncEndpointDeps`
    container declares - an asyncpg-backed
    :class:`SupportsDepartmentsRepo` adapter and a Bitbucket-scan
    callable bound to ``mcp_client.atlassian_client.bitbucket_list_repos``
    - folds them with the shared OIDC validator + audit logger + the
    canonical :func:`utc_now` clock used by the rest of the routers,
    and parks the result on ``app.state.repo_sync`` for the
    :func:`automation_service.api.repo_sync.sync_repo_mappings`
    endpoint to consume.

    The Bitbucket scanner is a plain reference to
    :func:`_bitbucket_list_repos_via_mcp`, which is the stand-in for
    the MCP-routed helper. Until the MCP transport is wired, production traffic
    that reaches this endpoint will surface a 502 ``bitbucket scan
    failed`` response after the scanner raises
    :class:`NotImplementedError`; the router's own error handling
    converts the exception into the documented gateway-error shape
    so the overall service contract stays intact. Tests bypass the
    MCP path entirely by pre-populating ``app.state.repo_sync`` with
    a hand-rolled :class:`BitbucketRepoScanner` fake before entering
    the lifespan.

    The ``http_client`` parameter is currently unused - kept on the
    helper signature so the lifespan call site stays self-documenting
    and the eventual MCP-routed scanner (which will issue HTTP calls
    through the shared client) can drop in without re-plumbing the
    handler.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.repo_sync`` (eg. via :class:`TestClient`
    fixtures injecting a hand-rolled :class:`RepoSyncEndpointDeps`)
    keeps observing its own container after lifespan startup completes
    for test overrides.
    """

    if getattr(app.state, "repo_sync", None) is not None:
        return  # test override wins
    departments_repo = _AsyncpgDepartmentsRepo(pool=pool)
    app.state.repo_sync = RepoSyncEndpointDeps(
        oidc_validator=oidc_validator,
        bitbucket_scanner=_bitbucket_list_repos_via_mcp,
        departments_repo=departments_repo,
        audit_logger=audit_logger,
        clock=utc_now,
    )


def _wire_po_review(
    app: FastAPI,
    *,
    pool: object,
    audit_logger: object,
    oidc_validator: object,
    vault: object,
    probe_client: object,
) -> None:
    """Wire ``app.state.po_review`` with production Bitbucket scanners.

    Builds the per-endpoint collaborators (branch / PR scanners, bot
    account id snapshot, asyncpg-backed :class:`DiffSummaryCacheRepo`,
    LLM diff callback, Bitbucket actions adapter) and folds them
    with the shared OIDC validator + audit logger + canonical
    :func:`utc_now` clock into the :class:`PoReviewEndpointDeps`
    dataclass the PO Review router pulls from ``app.state.po_review``
    at request time.

    The scanners resolve each department's Bitbucket workspace/repo
    from ``automation.repo_mappings`` and its bot credential from
    Vault (via ``automation.department_bots.credential_ref``), then
    issue read-only Bitbucket Cloud REST calls through the shared
    :class:`AtlassianProbeClient`. A department with no Bitbucket
    mapping yields an empty list rather than an error so the inbox /
    orphan-branch panels degrade gracefully.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.po_review`` keeps observing its own
    container after lifespan startup completes.
    """

    if getattr(app.state, "po_review", None) is not None:
        return  # test override wins

    async def _resolve_bitbucket_target(
        dept_id: str,
    ) -> tuple[Any, str, str] | None:
        """Return ``(credential, workspace, repo)`` for *dept_id*.

        ``None`` when the dept has no Bitbucket bot credential or no
        repo mapping - the caller treats this as "nothing to scan".
        """

        async with pool.acquire() as conn:  # type: ignore[attr-defined]
            bot = await conn.fetchrow(
                """
                SELECT credential_ref, username
                FROM automation.department_bots
                WHERE department_id = $1 AND service = 'bitbucket'
                """,
                dept_id,
            )
            mapping = await conn.fetchrow(
                """
                SELECT bitbucket_workspace, bitbucket_repo
                FROM automation.repo_mappings
                WHERE department_id = $1
                ORDER BY bitbucket_repo
                LIMIT 1
                """,
                dept_id,
            )
        if bot is None or mapping is None:
            return None
        workspace = str(mapping["bitbucket_workspace"] or "")
        repo = str(mapping["bitbucket_repo"] or "")
        if not workspace or not repo:
            return None

        ref = str(bot["credential_ref"] or "")
        if not ref:
            return None
        try:
            secret = vault.read(VaultPath.parse(ref))  # type: ignore[attr-defined]
        except (KeyError, ValueError):
            return None
        if not secret:
            return None

        class _Cred:
            __slots__ = ("url", "username", "personal_token")

            def __init__(self, payload: Any) -> None:
                self.url = str(payload.get("url") or "")
                self.username = str(
                    payload.get("username") or payload.get("email") or ""
                )
                self.personal_token = str(
                    payload.get("personal_token")
                    or payload.get("api_token")
                    or payload.get("token")
                    or payload.get("app_password")
                    or ""
                )

        return _Cred(secret), workspace, repo

    async def _branch_scanner(dept_id: str) -> list[dict[str, Any]]:
        """Real Bitbucket branch scanner for the orphan-branches list."""

        target = await _resolve_bitbucket_target(dept_id)
        if target is None:
            return []
        cred, workspace, repo = target
        return await probe_client.bitbucket_scan_branches(  # type: ignore[attr-defined]
            cred, workspace, repo
        )

    async def _pr_scanner(dept_id: str) -> list[dict[str, Any]]:
        """Real Bitbucket pull-request scanner for the PO Review inbox."""

        target = await _resolve_bitbucket_target(dept_id)
        if target is None:
            return []
        cred, workspace, repo = target
        return await probe_client.bitbucket_scan_pull_requests(  # type: ignore[attr-defined]
            cred, workspace, repo
        )

    async def _bot_account_ids(dept_id: str) -> frozenset[str]:
        """Per-dept Bitbucket bot account ids from ``department_bots``."""

        async with pool.acquire() as conn:  # type: ignore[attr-defined]
            rows = await conn.fetch(
                """
                SELECT account_id
                FROM automation.department_bots
                WHERE department_id = $1
                  AND service = 'bitbucket'
                  AND account_id IS NOT NULL
                  AND account_id != ''
                """,
                dept_id,
            )
        return frozenset(str(r["account_id"]) for r in rows)

    async def _llm_diff_callback(diff_hash: str) -> str:
        """LLM diff-summary renderer.

        The diff-summary cache is the primary surface; when a hash has
        no cached summary this callback returns an empty string so the
        orphan-branch row simply omits the summary rather than failing
        the whole scan. A richer MCP-routed summariser can replace this
        without touching the router.
        """

        _ = diff_hash
        return ""

    class _PoReviewActions:
        """Bitbucket PO Review actions driven through the probe client.

        ``open_draft`` re-opens a declined draft PR; ``request_changes``
        and ``approve_note`` post PO-side comments. Each resolves the
        dept's Bitbucket credential + repo before issuing the call and
        raises a clear error when the dept is not Bitbucket-enabled.
        """

        async def _target(self, dept_id: str) -> tuple[Any, str, str]:
            target = await _resolve_bitbucket_target(dept_id)
            if target is None:
                raise RuntimeError(
                    f"dept {dept_id!r} has no Bitbucket credential/repo mapping"
                )
            return target

        async def open_draft(self, dept_id: str, pr_id: int) -> None:
            cred, workspace, repo = await self._target(dept_id)
            await probe_client._bitbucket_post(  # type: ignore[attr-defined]
                cred,
                f"/repositories/{workspace}/{repo}/pullrequests/{pr_id}/request-changes",
                {},
            )

        async def request_changes(
            self, dept_id: str, pr_id: int, *, comment: str
        ) -> None:
            cred, workspace, repo = await self._target(dept_id)
            await probe_client._bitbucket_post(  # type: ignore[attr-defined]
                cred,
                f"/repositories/{workspace}/{repo}/pullrequests/{pr_id}/comments",
                {"content": {"raw": comment}},
            )

        async def approve_note(
            self, dept_id: str, pr_id: int, *, comment: str
        ) -> None:
            cred, workspace, repo = await self._target(dept_id)
            await probe_client._bitbucket_post(  # type: ignore[attr-defined]
                cred,
                f"/repositories/{workspace}/{repo}/pullrequests/{pr_id}/comments",
                {"content": {"raw": comment}},
            )

    diff_summary_cache = DiffSummaryCacheRepo(pool=pool)
    app.state.po_review = PoReviewEndpointDeps(
        oidc_validator=oidc_validator,
        branch_scanner=_branch_scanner,
        pr_scanner=_pr_scanner,
        bot_account_ids=_bot_account_ids,
        diff_summary_cache=diff_summary_cache,
        llm_diff_callback=_llm_diff_callback,
        actions=_PoReviewActions(),
        audit_logger=audit_logger,
        clock=utc_now,
    )


def _wire_inbound(
    app: FastAPI,
    *,
    pool: object,
    vault: object,
    audit_logger: object,
    temporal: object,
) -> None:
    """Wire ``app.state.inbound``.

    Build the per-channel collaborators (asyncpg-backed
    :class:`InboundDeptResolver`, Vault-backed
    :class:`SlackSignatureVerifier`) and assemble the
    :class:`InboundContext` the Slack route reads from
    ``app.state.inbound`` at request time.

    The resolver is bound to the shared asyncpg ``pool`` so future
    schema additions (Slack workspace mapping table, inbound email
    address mapping) can be picked up without re-wiring the
    application. Until those mapping tables exist the resolver returns
    ``None`` for every lookup, which the route translates into a
    400 ``inbound_dept_unresolved`` response - the documented
    behaviour for an unresolved channel signal.

    The Slack verifier reads the per-dept signing secret from Vault
    at request time (``vault:notifications/slack_inbound/<dept_id>``,
    or ``vault:notifications/slack_inbound/_default`` for the
    URL-verification handshake) and runs the deterministic
    :func:`verify_slack_signature` HMAC chain. The secret is fetched
    lazily so a credential rotation in Vault is picked up without
    restarting the process.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.inbound`` (eg. via :class:`TestClient`
    fixtures injecting hand-rolled fakes) keeps observing its own
    container after lifespan startup completes.
    """

    if getattr(app.state, "inbound", None) is not None:
        return  # test override wins
    dept_resolver = _AsyncpgInboundDeptResolver(pool=pool)
    slack_verifier = _VaultBackedSlackSignatureVerifier(vault=vault)
    app.state.inbound = InboundContext(
        dept_resolver=dept_resolver,
        workflow_client=temporal,
        slack_verifier=slack_verifier,
        audit_logger=audit_logger,
        env=os.environ,
        now_fn=utc_now,
    )


def _wire_webhook_v2(
    app: FastAPI,
    *,
    pool: object,
    vault: object,
    audit_logger: object,
    temporal: object,
) -> None:
    """Wire ``app.state.webhook_v2`` with the production ``WebhookContext``.

    Builds the asyncpg-backed
    :class:`automation_service.webhooks_handlers.DeptResolver` and
    folds it with the shared Vault client + Temporal client +
    audit logger + ``os.environ`` + :func:`utc_now` clock into the
    :class:`WebhookContext` the Jira issue webhook
    ``POST /webhooks/jira/issue_*`` handlers pull from
    ``app.state.webhook_v2`` at request time.

    The optional Jira commenter (used by the capability-denied
    response path) is left ``None`` here. The handler tolerates the
    missing commenter by skipping the
    best-effort comment and emitting a single audit row instead.

    The dept resolver returns ``None`` for every lookup until the
    ``automation.departments`` snapshot is available; the handler
    surfaces this as ``webhook_dept_unresolved`` - the documented
    behaviour for an unresolved event.  Tests bypass this entirely
    by pre-populating ``app.state.webhook_v2`` with a hand-rolled
    :class:`WebhookContext`.

    Honours the "skip if slot already set" guard so test code that
    pre-populates ``app.state.webhook_v2`` keeps observing its own
    container after lifespan startup completes.
    """

    if getattr(app.state, "webhook_v2", None) is not None:
        return  # test override wins

    class _AsyncpgWebhookV2DeptResolver:
        """Production :class:`DeptResolver` over an asyncpg pool.

        Holds the shared :class:`asyncpg.Pool` so the
        ``automation.departments`` / ``automation.department_project_keys``
        snapshot can be wired without re-plumbing the lifespan handler.
        Until that snapshot ships both methods return conservative
        defaults (``None`` for the project_key lookup, empty list for
        the bot account id registry) so the handler emits
        ``webhook_dept_unresolved`` / "no bots known" - the documented
        behaviour for an unresolved event.
        """

        class _Cred:
            __slots__ = ("credential_ref", "account_id", "username")

            def __init__(
                self,
                *,
                credential_ref: str = "",
                account_id: str = "",
                username: str = "",
            ) -> None:
                self.credential_ref = credential_ref
                self.account_id = account_id
                self.username = username

            def has_credential(self) -> bool:
                return bool(self.credential_ref)

        class _Bot:
            __slots__ = ("jira", "bitbucket", "confluence")

            def __init__(self) -> None:
                self.jira = None
                self.bitbucket = None
                self.confluence = None

        class _Dept:
            __slots__ = (
                "id",
                "default_language",
                "web_search_enabled",
                "bot",
                "available_repos",
                "available_spaces",
                "available_capabilities",
            )

            def __init__(
                self,
                *,
                dept_id: str,
                default_language: str,
                web_search_enabled: bool,
            ) -> None:
                self.id = dept_id
                self.default_language = default_language
                self.web_search_enabled = web_search_enabled
                self.bot = _AsyncpgWebhookV2DeptResolver._Bot()
                self.available_repos: tuple[str, ...] = ()
                self.available_spaces: tuple[str, ...] = ()
                self.available_capabilities: tuple[str, ...] = ()

        class _BotAccount:
            __slots__ = ("dept_id", "service", "account_id")

            def __init__(
                self, *, dept_id: str, service: str, account_id: str
            ) -> None:
                self.dept_id = dept_id
                self.service = service
                self.account_id = account_id

        __slots__ = ("_pool",)

        def __init__(self, *, pool: object) -> None:
            self._pool = pool

        async def resolve_by_project_key(
            self, project_key: str
        ) -> Any:
            """Return the :class:`SupportsDepartment` for *project_key*."""

            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT d.id, d.default_language, d.web_search_enabled
                    FROM automation.department_project_keys pk
                    JOIN automation.departments d ON d.id = pk.department_id
                    WHERE upper(pk.project_key) = upper($1)
                      AND d.mode <> 'disabled'
                    """,
                    project_key,
                )
            if row is None:
                return self._file_dept_for_project(project_key)
            return await self._load_dept(str(row["id"]), row=row)

        async def resolve_by_dept_id(self, dept_id: str) -> Any:
            """Return a department by id for dept-handover starts."""

            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, default_language, web_search_enabled
                    FROM automation.departments
                    WHERE id = $1 AND mode <> 'disabled'
                    """,
                    dept_id,
                )
            if row is None:
                return self._file_dept_by_id(dept_id)
            return await self._load_dept(dept_id, row=row)

        async def list_bot_account_ids(self) -> list[Any]:
            """Return every ``(dept_id, service, account_id)`` triple."""

            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT department_id, service, account_id
                    FROM automation.department_bots
                    WHERE account_id IS NOT NULL AND account_id <> ''
                    """
                )
            accounts = [
                self._BotAccount(
                    dept_id=str(row["department_id"]),
                    service=str(row["service"]),
                    account_id=str(row["account_id"]),
                )
                for row in rows
            ]
            accounts.extend(self._file_bot_accounts())
            return accounts

        async def _load_dept(self, dept_id: str, *, row: Any) -> Any:
            dept = self._Dept(
                dept_id=dept_id,
                default_language=str(row["default_language"] or "tr"),
                web_search_enabled=bool(row["web_search_enabled"]),
            )
            async with self._pool.acquire() as conn:
                bot_rows = await conn.fetch(
                    """
                    SELECT service, credential_ref, account_id, username
                    FROM automation.department_bots
                    WHERE department_id = $1
                    """,
                    dept_id,
                )
                repo_rows = await conn.fetch(
                    """
                    SELECT bitbucket_repo
                    FROM automation.repo_mappings
                    WHERE department_id = $1
                    ORDER BY bitbucket_repo
                    """,
                    dept_id,
                )
                space_rows = await conn.fetch(
                    """
                    SELECT space_key
                    FROM automation.department_space_keys
                    WHERE department_id = $1
                    ORDER BY space_key
                    """,
                    dept_id,
                )
                runner_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM infrastructure.dept_ssh_assignments a
                    JOIN infrastructure.ssh_runners r ON r.runner_id = a.runner_id
                    WHERE a.dept_id = $1 AND r.status = 'active'
                    """,
                    dept_id,
                )
            capabilities: set[str] = set()
            for bot_row in bot_rows:
                service = str(bot_row["service"])
                capabilities.add(service)
                setattr(
                    dept.bot,
                    service,
                    self._Cred(
                        credential_ref=str(bot_row["credential_ref"] or ""),
                        account_id=str(bot_row["account_id"] or ""),
                        username=str(bot_row["username"] or ""),
                    ),
                )
            dept.available_repos = tuple(str(r["bitbucket_repo"]) for r in repo_rows)
            dept.available_spaces = tuple(str(r["space_key"]) for r in space_rows)
            if dept.web_search_enabled:
                capabilities.add("web_search")
            if runner_count and int(runner_count) > 0:
                capabilities.add("execution")
            dept.available_capabilities = tuple(sorted(capabilities))
            return dept

        def _file_departments(self) -> list[dict[str, Any]]:
            import json
            from pathlib import Path

            for parent in Path(__file__).resolve().parents:
                path = parent / "config" / "departments.json"
                if path.is_file():
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001
                        return []
                    return [
                        d for d in data.get("departments", []) if isinstance(d, dict)
                    ]
            return []

        def _file_dept_by_id(self, dept_id: str) -> Any:
            for item in self._file_departments():
                if item.get("id") == dept_id:
                    return self._dept_from_config(item)
            return None

        def _file_dept_for_project(self, project_key: str) -> Any:
            wanted = project_key.upper()
            for item in self._file_departments():
                keys = [str(k).upper() for k in item.get("jira_project_keys", [])]
                if wanted in keys:
                    return self._dept_from_config(item)
            return None

        def _dept_from_config(self, item: dict[str, Any]) -> Any:
            dept = self._Dept(
                dept_id=str(item.get("id")),
                default_language=str(item.get("default_language") or "tr"),
                web_search_enabled=bool(item.get("web_search_enabled", False)),
            )
            bot = item.get("bot") if isinstance(item.get("bot"), dict) else {}
            for service in ("jira", "bitbucket", "confluence"):
                entry = bot.get(service) if isinstance(bot, dict) else None
                if isinstance(entry, dict):
                    setattr(
                        dept.bot,
                        service,
                        self._Cred(
                            credential_ref=str(entry.get("credential_ref") or ""),
                            account_id=str(entry.get("account_id") or ""),
                            username=str(entry.get("username") or ""),
                        ),
                    )
            dept.available_repos = tuple(
                str(m.get("bitbucket_repo"))
                for m in item.get("repo_mappings", [])
                if isinstance(m, dict) and m.get("bitbucket_repo")
            )
            dept.available_spaces = tuple(
                str(s) for s in item.get("confluence_space_keys", [])
            )
            return dept

        def _file_bot_accounts(self) -> list[Any]:
            accounts: list[Any] = []
            for item in self._file_departments():
                dept_id = str(item.get("id") or "")
                bot = item.get("bot") if isinstance(item.get("bot"), dict) else {}
                for service in ("jira", "bitbucket", "confluence"):
                    entry = bot.get(service) if isinstance(bot, dict) else None
                    account_id = (
                        str(entry.get("account_id") or "") if isinstance(entry, dict) else ""
                    )
                    if dept_id and account_id:
                        accounts.append(
                            self._BotAccount(
                                dept_id=dept_id,
                                service=service,
                                account_id=account_id,
                            )
                        )
            return accounts

    dept_resolver = _AsyncpgWebhookV2DeptResolver(pool=pool)
    app.state.webhook_v2 = WebhookContext(
        vault=vault,
        dept_resolver=dept_resolver,
        workflow_client=temporal,
        jira_commenter=None,
        audit_logger=audit_logger,
        env=os.environ,
        now_fn=utc_now,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and return a fully wired FastAPI app for tests / runtime.

    Parameters
    ----------
    settings:
        Optional pre-built :class:`Settings`. When omitted, the default
        is constructed from the process environment (and an optional
        ``.env`` file, see ``Settings.model_config``).

    Returns
    -------
    FastAPI
        The configured application instance with the ``/healthz`` and
        ``/readyz`` routes registered.
    """

    # Install the redaction filter exactly once per process. The helper
    # short-circuits if the filter is already attached so calling it
    # from multiple ``create_app()`` invocations (eg. in test fixtures)
    # is safe.
    install_redaction_filter(loggers=[logging.getLogger()], attach_to_root=True)

    resolved = settings if settings is not None else Settings()

    app = FastAPI(
        title="automation-service",
        version="0.1.0",
        description=(
            "Webhook gateway, Temporal client and admin endpoint owner "
            "for the platform."
        ),
        lifespan=lifespan,
    )

    # Stash the resolved :class:`Settings` on ``app.state`` immediately
    # after constructing the FastAPI object. This is the contract the
    # production lifespan handler relies on - it reads
    # ``app.state.settings`` to resolve ``postgres_dsn``,
    # ``temporal_host``, ``mcp_base_url`` and friends without re-reading
    # the environment. Test code that calls ``create_app(settings)`` to
    # inject overrides also relies on this assignment to surface the
    # custom Settings instance through the same attribute
    # for dependency injection.
    app.state.settings = resolved

    # Mount the trace_id propagation middleware FIRST so every other
    # middleware / route handler observes the resolved trace_id via
    # :func:`observability.get_trace_id`.  Starlette's
    # ``add_middleware`` prepends to the middleware stack - the
    # *latest* registered middleware sits on the outermost layer of
    # the chain.  We add ``TraceMiddleware`` immediately after
    # :class:`FastAPI` construction (and before any router includes
    # that may register their own middlewares in the future) so that
    # the trace_id is available throughout the entire request lifecycle
    # across the entire request lifecycle.
    app.add_middleware(TraceMiddleware)

    # Mount :class:`SecurityHeadersMiddleware`
    # so every HTTP response carries X-Frame-Options, X-Content-Type-Options
    # and X-XSS-Protection headers regardless of status code or content type
    # for baseline response hardening.
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness probe - 200 while the process is alive."""

        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, str | list[str]]:
        """Readiness probe with real dependency checks.

        Probes PostgreSQL (``SELECT 1``) and Temporal (gRPC health
        check) in parallel with a 3-second per-probe timeout
        for dependency readiness.

        Returns 200 ``{"status": "ready"}`` when all dependencies are
        reachable. Returns 503 ``{"status": "not_ready",
        "failed_dependencies": [...]}`` when any probe fails.
        """
        from . import readiness as _readiness

        all_ready, details = await _readiness.check_readiness([
            lambda: _readiness.probe_postgres(resolved.postgres_dsn),
            lambda: _readiness.probe_temporal(resolved.temporal_host),
        ])

        if not all_ready:
            response.status_code = 503
            return details
        return details

    # Mount the Jira issue webhook handlers
    # (``POST /webhooks/jira/issue_created`` and
    # ``POST /webhooks/jira/issue_commented``). The router
    # reads its collaborators from ``app.state.webhook_v2`` (a
    # :class:`WebhookContext`) at request time so tests can wire the
    # full chain without touching ``create_app`` itself.
    from .webhooks_handlers import router as webhooks_v2_router

    app.include_router(webhooks_v2_router, prefix="/webhooks")

    # Mount the admin endpoints. The router pulls
    # its collaborators (orchestrator, vault client, audit logger,
    # connection factory, optional probe + temporal clients) off
    # ``app.state.admin``; production wiring populates that on
    # service startup. Tests inject a stub ``AdminEndpointDeps``
    # so the router can be exercised without a live backend.
    from .admin.router import router as admin_router

    app.include_router(admin_router)

    # Mount the inbound channel adapter.
    # The Slack→task router exposes ``POST /webhooks/inbound/slack`` and
    # reads its collaborators (dept resolver, workflow client, Slack
    # signature verifier, audit logger, env, clock) from
    # ``app.state.inbound`` at request time. The IMAP-based email
    # adapter (:class:`EmailToTaskPoller`) shares the same context but
    # is started as a background task by the service startup hook -
    # FastAPI does not own its lifecycle.
    from .inbound import slack_router as inbound_slack_router

    app.include_router(inbound_slack_router, prefix="/webhooks")

    # Mount the cancel API endpoint.
    # ``POST /api/workflows/{workflow_id}/cancel`` runs the
    # ``is_cancel_authorized`` predicate against the OIDC actor and
    # invokes ``WorkflowHandle.cancel()`` on success. Production wiring
    # populates ``app.state.cancel`` (a :class:`CancelEndpointDeps`)
    # during startup; tests inject a stub container directly.
    from .api import cancel_router

    app.include_router(cancel_router)

    # Mount the repo-mapping auto-sync admin endpoint. ``POST
    # /admin/departments/{id}/repo-mappings/sync`` scans the dept's
    # Bitbucket workspace, diffs the result against the dept's current
    # ``repo_mappings`` array via the pure
    # :func:`temporal_shared.repo_sync.compute_repo_mapping_diff`
    # helper, and either returns the diff (dry-run, default) or
    # atomically replaces the mapping list (``?apply=true``).
    # Production wiring populates ``app.state.repo_sync`` (a
    # :class:`RepoSyncEndpointDeps`) during startup; tests inject a
    # stub container directly.
    from .api import repo_sync_router

    app.include_router(repo_sync_router)

    # Mount the PO Review API endpoints. ``GET /api/orphan-branches`` lists ``ai/*``
    # branches with no associated PR (each row carrying a cached LLM
    # diff summary via :mod:`automation_service.diff_summary_cache`);
    # ``GET /api/po-review-inbox`` lists draft bot-authored PRs; the
    # three per-PR POST endpoints (``open-draft``, ``request-changes``,
    # ``approve-note``) drive Bitbucket from the PO Review Inbox
    # Streamlit page. Production wiring populates
    # ``app.state.po_review`` (a :class:`PoReviewEndpointDeps`) during
    # startup; tests inject a stub container directly.
    from .api import po_review_router

    app.include_router(po_review_router)

    # Mount the webhooks-v3 router. ``POST /webhooks/jira`` and
    # ``POST /webhooks/bitbucket`` run the
    # :class:`automation_service.webhook_filters.WebhookFilterChain`
    # end-to-end, claim the delivery against
    # ``automation.processed_events`` on a pass, and dispatch the
    # workflow via :func:`temporal_shared.start_helper.start_workflow_idempotent`.
    # Production wiring populates ``app.state.webhooks`` (a
    # :class:`WebhooksEndpointDeps`) during startup; tests inject a
    # stub container directly.  The router is mounted under
    # ``/webhooks`` so the final URLs are ``/webhooks/jira`` and
    # ``/webhooks/bitbucket`` - distinct from the legacy
    # ``/webhooks/jira/issue_created`` paths exposed by
    # :mod:`automation_service.webhooks_handlers` so both surfaces
    # can co-exist during the migration.
    from .api import webhooks_router

    app.include_router(webhooks_router, prefix="/webhooks")

    # Mount the per-service department credential CRUD + probe router
    # Surfaces ``GET /admin/departments``,
    # ``GET /admin/departments/{id}``,
    # ``POST|DELETE /admin/departments/{id}/credentials/{service}`` and
    # ``POST /admin/departments/{id}/probe``. Production wiring
    # populates ``app.state.dept_credentials`` (a
    # :class:`routers.dept_credentials.DeptCredentialEndpointDeps`)
    # during startup with the
    # :class:`services.dept_credential_service.DeptCredentialService`
    # orchestrator, an asyncpg connection factory and the
    # service-wide :class:`audit_logger.AuditLogger`. Tests inject a
    # stub container directly.  When the wiring is missing the router
    # surfaces a 500 ``dept_credentials router is not wired`` detail
    # rather than a less helpful attribute-error.
    from routers.dept_credentials import router as dept_credentials_router

    app.include_router(dept_credentials_router)

    # Mount the webhook pipeline router. Exposes ``POST /webhooks/jira/pipeline`` which
    # runs every payload through the canonical
    # Event_Dedup → Loop_Guard → Webhook_Dispatcher chain. The
    # :class:`middleware.webhook_auth.WebhookAuthMiddleware` (already
    # mounted at the ASGI layer when production wiring runs
    # :func:`wire_webhook_pipeline` below) sits in front of this
    # endpoint so the pipeline only sees HMAC-authenticated payloads
    # - keeping the HMAC verify → dedup → loop_guard → dispatcher
    # ordering intact.
    #
    # Production wiring populates ``app.state.webhook_pipeline`` (a
    # :class:`webhooks.WebhookPipeline`) during the lifespan startup
    # via :func:`wire_webhook_pipeline`. Tests can build a stub
    # pipeline directly and stash it on the same attribute. When the
    # attribute is missing the router replies 503 ``pipeline_not_configured``
    # so the webhook provider's retry can pick the request back up
    # once startup finishes.
    from webhooks import pipeline_router as webhook_pipeline_router

    app.include_router(webhook_pipeline_router, prefix="/webhooks")

    return app


def wire_webhook_pipeline(
    app: FastAPI,
    *,
    db: object,
    temporal: object,
    audit_logger: object | None = None,
    vault: object | None = None,
    jira_commenter: object | None = None,
    admin_notifier: object | None = None,
) -> None:
    """Build the webhook pipeline and stash it on ``app.state``.

    Called from the FastAPI lifespan handler after the database pool,
    Temporal client, audit logger and Vault client have all been
    constructed. Mirrors the ``app.state.webhooks`` /
    ``app.state.cancel`` wiring patterns used elsewhere in the
    automation-service: the router itself is stateless, but it pulls
    its collaborators off ``app.state.webhook_pipeline`` at request
    time.

    The HMAC verification middleware
    (:class:`middleware.webhook_auth.WebhookAuthMiddleware`) MUST be
    added separately (typically before this call) so that it sits in
    front of the pipeline - only authenticated payloads should ever
    reach the dedup → loop_guard → dispatcher chain
    for the dedup, loop-guard, and dispatcher pipeline.

    Parameters
    ----------
    app:
        The FastAPI application returned by :func:`create_app`.
    db:
        Async database pool (asyncpg-compatible).
    temporal:
        Temporal client.
    audit_logger:
        Optional audit logger.
    vault:
        Optional vault client.
    jira_commenter:
        Optional Jira commenter for concurrency-rejection notes
        for concurrency-rejection notes.
    admin_notifier:
        Optional admin notifier for loop storm alerts.
    """

    from webhooks import build_webhook_pipeline

    pipeline = build_webhook_pipeline(
        db=db,
        temporal=temporal,
        audit_logger=audit_logger,
        vault=vault,
        jira_commenter=jira_commenter,
        admin_notifier=admin_notifier,
    )
    app.state.webhook_pipeline = pipeline


# Module-level app for ``uvicorn automation_service.app:app`` and the
# legacy ``uvicorn src.main:app`` entry points (the latter re-exports
# this object from ``src/main.py``).
app = create_app()
