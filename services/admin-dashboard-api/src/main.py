"""FastAPI entry point for the admin-dashboard-api service.

Exposes the standard liveness/readiness contract shared by every HTTP service
in the service stack and the Control_Plane wiring for the audit sink:

* ``GET /healthz`` → 200 with body ``{"status": "ok"}``. Always returns
  200 as long as the process is alive — the manifest-load failure does
  **not** affect liveness.
* ``GET /readyz``  → 200 ``{"status": "ready"}`` when dependencies are
  reachable **and** the manifest loaded cleanly. Returns 503 with
  ``{"status": "not_ready"}`` (≤64 bytes) when the dependency probe
  fails, or 503 with ``{"status": "not_ready", "reason":
  "manifest_invalid"}`` when ``load_manifest`` raised
  :class:`ManifestLoadError` during startup.

Lifespan wiring:

* ``manifest = load_manifest(workspace_root)`` is called inside the
  FastAPI lifespan context. On :class:`ManifestLoadError` we capture
  the exception in ``app.state.manifest_error`` and continue to bring
  the rest of the lifespan up — ``/healthz`` remains 200 so the
  container does not flap, while ``/readyz`` flips to 503.
* :class:`OIDCValidator` is constructed once via the shared
  ``get_validator()`` cache from ``src.auth.dependencies`` and stored
  on ``app.state.oidc_validator`` for introspection by tests.
* :class:`httpx.AsyncClient` and :class:`asyncpg.Pool` are opened on
  startup and closed on shutdown.
* When the manifest loaded successfully, a :class:`LifecycleService`
  singleton is constructed and stored on ``app.state.lifecycle``. The
  ``get_lifecycle_service()`` dependency returns this instance.
* The ``/admin/services`` router is mounted via
  ``app.include_router(services_lifecycle_router)`` when service lifecycle wiring has
  been merged. The import is guarded with a soft-failure path so this
  module remains importable while service lifecycle wiring is in flight.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from http_shared import SecurityHeadersMiddleware, install_redaction_filter

# ``observability.TraceMiddleware`` extracts / generates the
# ``X-Trace-Id`` header per request and binds the trace_id into the
# per-request :mod:`contextvars` context so downstream code (admin
# router handlers, audit log writers, the AdminProxy that fans out to
# automation-service) can correlate logs end to end without threading
# the value through every helper.
from observability import TraceMiddleware

from .auth.dependencies import get_validator
from .audit_sink import LoggingAuditSink
from .config import Settings
from .lifecycle.audit_writer import AuditEntry, AuditWriter
from .lifecycle.compose_runner import ComposeRunner
from .lifecycle.credential_guard import check_credentials
from .lifecycle.health_probe import HealthProbe
from .lifecycle.service import LifecycleService
from .lifecycle.vault_client import VaultClient
from .manifest import ManifestLoadError, load_manifest
from .proxy import AdminProxy

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .lifecycle.service import LifecycleService as _LifecycleService

settings = Settings()

logger = logging.getLogger(__name__)

# Install the credential / secret redaction filter on the root logger
# before any handler is constructed by uvicorn / FastAPI / our own
# lifespan. The helper is idempotent so
# repeat imports under ``uvicorn --reload`` are harmless.
install_redaction_filter(loggers=[logging.getLogger()], attach_to_root=True)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown hooks for the admin-dashboard-api process.

    On startup:

    1. Load the manifest. ``ManifestLoadError`` is captured in
       ``app.state.manifest_error`` so :func:`readyz` can return 503
       with the documented body shape.
    2. Construct the OIDC validator (cached singleton) and place it on
       ``app.state.oidc_validator``.
    3. Open an :class:`httpx.AsyncClient` for outbound HTTP traffic
       (HealthProbe, VaultClient).
    4. Open an :class:`asyncpg.Pool` (deferred — created lazily on the
       first :meth:`AuditWriter.start` call so the Boot_Bundle can come
       up before Postgres is fully ready in dev environments).
    5. If the manifest loaded successfully, build a
       :class:`LifecycleService` singleton and stash it on
       ``app.state.lifecycle`` so the ``/admin/services`` router can
       resolve it via :func:`get_lifecycle_service`.

    On shutdown the inverse: stop the audit-writer drainer, close the
    asyncpg pool and httpx client. Each ``aclose`` is wrapped in a
    best-effort try/except so a partially-initialised state cannot
    block container shutdown.
    """

    # Default state slots — populated below so the readiness probe can
    # always check them without ``hasattr``.
    app.state.credential_blocked = False
    app.state.manifest_error = None
    app.state.manifest = None
    # Both attribute names point at the same singleton: ``lifecycle``
    # is the historical/ergonomic name kept for any future call sites,
    # while ``lifecycle_service`` is the canonical name expected by
    # the service lifecycle wiring router (``src.routers.services_lifecycle``). Setting
    # both lets either consumer resolve the same instance.
    app.state.lifecycle = None
    app.state.lifecycle_service = None
    app.state.oidc_validator = None
    app.state.http_client = None
    app.state.audit_writer = None
    app.state.audit_deferred_queue = None
    app.state.vault_client = None
    app.state.compose_runner = None
    app.state.health_probe = None
    app.state.admin_proxy = None
    # PromptsGitRouter (prompt git wiring) state slots — populated below.
    app.state.prompts_repo = None
    app.state.prompts_pr_opener = None
    app.state.prompts_audit_sink = None
    app.state.prompts_audit_pool = None
    app.state.prompts_dir_prefix = None
    app.state.workspace_root = None
    app.state.prompt_sandbox_history = None
    # PromptSandbox (service lifecycle wiring) — populated below alongside prompts_repo.
    app.state.prompt_sandbox = None
    # RunnerWorkspacesRouter (workspace and prompt router wiring) — populated by future lifespan
    # wiring that builds a paramiko-backed SSH client. Until then the
    # slot stays ``None`` so the router degrades to "empty list / 503"
    # per its module docstring.
    app.state.runner_workspaces_client = None

    # WorkflowControlRouter — populated
    # below with a Temporal-backed :class:`SupportsTemporalControl`
    # adapter. If Temporal is unreachable the slot stays ``None`` and
    # the router surfaces 503 with ``reason="temporal_unavailable"``.
    app.state.temporal_workflow_client = None
    # Optional explicit audit sink for workflow-control events. When
    # ``None`` the router falls back to the AdminProxy's audit sink
    # so events land in the same ``automation.audit_events`` stream
    # as other admin actions.
    app.state.workflow_control_audit_sink = None

    # PromptsRouter — populated by
    # future lifespan wiring once the production LLM client +
    # GitRepo-backed committer + Bitbucket adapter ship. Until then
    # the slots stay ``None`` and the mutating endpoints surface
    # ``HTTP 503`` with a stable ``reason`` per the router's module
    # docstring. The read-only endpoints (``GET /api/v1/prompts`` and
    # ``GET /api/v1/prompts/{name}``) work without any of these slots
    # because they read directly off the local filesystem.
    app.state.prompts_llm = None
    app.state.prompts_committer = None
    app.state.prompts_bitbucket = None

    # CapabilitiesRouter — populated by
    # future lifespan wiring once the production prober + asyncpg
    # ``shared.capability_probes`` store adapter ship. Until then the
    # ``capability_prober`` slot stays ``None`` and the single-probe
    # endpoint surfaces ``HTTP 503`` with
    # ``reason="prober_unavailable"``. The matrix endpoint stays
    # answerable regardless — when ``capability_probe_store`` is
    # ``None`` the router auto-installs an in-memory store so the
    # cache-read path always has something to hit. We pre-init both
    # slots here so tests that drive lifespan can start from a clean
    # state on every cycle (the module-level defaults below survive
    # the import but not lifespan re-entry).
    app.state.capability_prober = None
    app.state.capability_probe_store = None

    # DepartmentsRouter runtime CRUD —
    # ``departments_reload_publisher`` is an optional async callable
    # ``(dept_id, action) -> None`` that fans the change out to the
    # consumer services (assistant-service, automation-service). When
    # ``None`` the router falls back to a best-effort HTTP POST through
    # the AdminProxy and otherwise relies on the consumers' 30s config
    # poll loop. ``dept_audit_sink`` is the explicit audit sink for
    # ``dept_created/updated/deleted`` events; when ``None`` the
    # router falls back to the AdminProxy's audit sink so events land
    # in the same ``automation.audit_events`` stream as other admin
    # actions.
    app.state.departments_reload_publisher = None
    app.state.dept_audit_sink = None

    # Secret rotation router —
    # ``secret_rotator`` is the :class:`SupportsSecretRotator` adapter
    # that performs the Vault KV-v2 write. ``secret_reload_publisher``
    # is the optional :class:`SupportsHotReloadPublisher` (Redis
    # pub/sub or HTTP fan-out); when ``None`` the rotation logs a
    # structured ``secret_reload_pending`` warning so operators can
    # page consumers manually. ``secret_rotation_audit_sink``
    # is the optional explicit audit sink; when ``None`` the router
    # falls back to the AdminProxy's audit sink so ``secret_rotated``
    # events land in the same audit stream as other admin actions
    # events land in the same audit stream as other admin actions.
    # All three stay ``None`` until production wiring lands.
    app.state.secret_rotator = None
    app.state.secret_reload_publisher = None
    app.state.secret_rotation_audit_sink = None

    # ---- 0. Credential Guard ----------------------------------------
    # Must run at the very beginning of lifespan. When blocked, the
    # service refuses to proceed with remaining startup steps and
    # /healthz returns 503 with reason "insecure_credentials".
    guard_result = check_credentials(
        platform_env=os.environ.get("PLATFORM_ENV", ""),
        env_vars=dict(os.environ),
    )
    if guard_result.blocked:
        app.state.credential_blocked = True
        logger.critical(
            "Credential guard blocked boot — insecure dev-default "
            "credentials detected in production. Skipping remaining "
            "lifespan steps."
        )
        try:
            yield
        finally:
            pass
        return

    # ---- 1. Manifest -------------------------------------------------
    try:
        manifest = load_manifest(settings.workspace_root)
        app.state.manifest = manifest
    except ManifestLoadError as exc:
        # Capture the failure but continue to
        # bring the process up so the container's HTTP probe can
        # surface the "manifest_invalid" reason via /readyz. The
        # process remains live (HEALTHCHECK passes) so the operator
        # can replace the manifest without a forced restart loop.
        app.state.manifest_error = exc
        manifest = None
        logger.error(
            "manifest load failed; readiness probe will return 503 with "
            "reason=manifest_invalid: %s",
            exc,
        )

    # ---- 1b. Bot account_id uniqueness — fail-fast -------------------
    # The admin-dashboard-api owns ``config/departments.json`` (CRUD
    # endpoints + hot-reload signal); enforcing the uniqueness
    # invariant at boot here means an operator who hand-edits the
    # file outside the CRUD flow still gets a clean fail-fast error
    # instead of silent webhook routing ambiguity. The CRUD layer's
    # ``_find_account_id_conflicts`` covers the "edited via API"
    # case; this block covers the "edited on disk" case.
    #
    # The validator is intentionally tolerant of a missing /
    # malformed file — the readiness probe already surfaces config
    # corruption via the existing 503 paths, and we don't want a
    # transient ``OSError`` to take the whole admin surface down.
    # A real *uniqueness violation*, however, MUST stop the process:
    # we log the error and re-raise so uvicorn exits non-zero, per
    # the startup brief.
    departments_config_path: Path = (
        settings.workspace_root / "config" / "departments.json"
    )
    if not departments_config_path.exists():
        # Some test / staging environments resolve ``workspace_root``
        # to the repo root rather than ``platform/``. Try the
        # alternate layout before logging — keeps the validator
        # ergonomic across the two conventions used in the repo.
        alt_path = (
            settings.workspace_root / "platform" / "config" / "departments.json"
        )
        if alt_path.exists():
            departments_config_path = alt_path

    try:
        from db_shared import (
            BotAccountIdConflictError,
            validate_bot_account_id_uniqueness_from_file,
        )

        validate_bot_account_id_uniqueness_from_file(departments_config_path)
        logger.info(
            "bot_identity uniqueness validated at boot — "
            "config=%s",
            departments_config_path,
        )
    except ImportError:
        # db_shared not available — skip bot_identity validation
        logger.info(
            "db_shared not available — skipping bot_identity "
            "uniqueness validation"
        )
    except Exception as exc:
        # Catch BotAccountIdConflictError or any other validation error
        logger.error(
            "bot_identity uniqueness violation at boot — "
            "refusing to start: %s",
            exc,
        )
        raise
    except FileNotFoundError as exc:
        # Brand-new install / test harness without a config file —
        # nothing to validate. The CRUD layer will create one on
        # first ``POST /api/v1/departments``.
        logger.info(
            "departments.json not found at %s — skipping bot_identity "
            "uniqueness check (no depts configured yet): %s",
            departments_config_path,
            exc,
        )
    except (OSError, ValueError) as exc:
        # ``ValueError`` here means a non-conflict shape problem
        # (unparseable JSON, etc) since the conflict path is caught
        # above. Schema validation is the long-term signal for those
        # — we surface a warning so the readiness probe / schema
        # check can take over.
        logger.warning(
            "bot_identity uniqueness check skipped due to config read "
            "error (schema validation will surface this separately): %s",
            exc,
        )


    # ---- 2. OIDC validator (cached singleton) ------------------------
    # ``get_validator`` reads :class:`Settings` and memoises the
    # validator instance — the JWKS cache inside :class:`OIDCValidator`
    # is therefore shared across requests.
    app.state.oidc_validator = get_validator()

    # ---- 3. httpx.AsyncClient ---------------------------------------
    http_client = httpx.AsyncClient(timeout=10.0, trust_env=False)
    app.state.http_client = http_client

    # ---- 4. asyncpg pool (lazy) + AuditWriter ------------------------
    # We construct the AuditWriter eagerly but defer the actual pool
    # creation until ``start()`` is awaited — that way a temporary
    # Postgres outage during boot does not crash the process. When the
    # manifest also failed to load we skip ``start`` entirely because
    # there is no LifecycleService to feed the writer.
    deferred_queue: asyncio.Queue[AuditEntry] = asyncio.Queue()
    audit_writer = AuditWriter(
        dsn=settings.postgres_dsn,
        deferred_queue=deferred_queue,
    )
    app.state.audit_writer = audit_writer
    app.state.audit_deferred_queue = deferred_queue

    if manifest is not None:
        try:
            await audit_writer.start()
        except Exception as exc:  # noqa: BLE001 - log and continue
            # Pool creation failed (e.g. Postgres still booting). The
            # readiness probe in ``Settings.dependencies_reachable``
            # is the long-term signal here; we surface the failure in
            # logs but keep going so a transient outage doesn't kill
            # the process.
            logger.warning(
                "AuditWriter.start() failed during lifespan; will retry "
                "lazily: %s",
                exc,
            )

    # ---- 5. Lifecycle wiring ----------------------------------------
    if manifest is not None:
        vault_client = VaultClient(
            addr=settings.vault_addr,
            token=settings.vault_token,
        )
        compose_runner = ComposeRunner(
            compose_file=settings.workspace_root / settings.compose_file,
            workspace_root=settings.workspace_root,
        )
        health_probe = HealthProbe(
            http_client=http_client,
            temporal_host=settings.temporal_host,
            compose_internal_ports={
                "automation-service": 8080,
                "assistant-service": 8081,
                "admin-dashboard-api": 8082,
                "admin-dashboard-ui": 3000,
                "task-intake-service": 8083,
                "atlassian-mcp": 8090,
                "firecrawl": 3002,
                "opencode-sidecar": 4096,
                "streamlit-ui": 8501,
            },
            prefer_docker_health=True,
        )
        lifecycle = LifecycleService(
            manifest=manifest,
            audit=audit_writer,
            vault=vault_client,
            compose=compose_runner,
            health=health_probe,
            workspace_root=settings.workspace_root,
            health_poll_interval_seconds=settings.health_poll_interval_seconds,
            health_ready_timeout_seconds=settings.health_ready_timeout_seconds,
            health_fail_streak_threshold=settings.health_fail_streak_threshold,
        )
        app.state.vault_client = vault_client
        app.state.compose_runner = compose_runner
        app.state.health_probe = health_probe
        app.state.lifecycle = lifecycle
        app.state.lifecycle_service = lifecycle

    # ---- 6. AdminProxy (admin proxy wiring — platform foundation) ------
    # BFF forwarder for ``/admin/*`` endpoints owned by
    # automation-service. Wired regardless of
    # manifest state because forwarding does not depend on the local
    # Compose manifest — even when the local lifecycle surface is
    # unavailable the operator should still be able to reach
    # automation-service.
    admin_proxy = AdminProxy(
        automation_service_url=settings.automation_service_url,
        http_client=http_client,
        audit_sink=LoggingAuditSink(),
        # Credential save runs a live Atlassian read+write probe through
        # the MCP (myself → find issue → add comment → delete comment),
        # which legitimately takes longer than the default 30s on cloud
        # round trips. Allow 90s so a valid credential is not rejected
        # with a spurious 504 upstream timeout.
        request_timeout_seconds=90.0,
    )
    app.state.admin_proxy = admin_proxy

    # ---- 7. PromptsGitRouter (prompt git wiring — operations surface) -------
    # Build the :class:`GitRepo` that the ``/admin/prompts`` endpoints
    # operate on. Failures here are non-fatal — we leave
    # ``app.state.prompts_repo`` as ``None`` so the dependency
    # surfaces 503 with a clear reason while the rest of the admin
    # surface keeps serving traffic.
    app.state.prompts_dir_prefix = settings.prompts_dir_prefix
    # Stash the workspace root so :func:`_build_pr_description` can
    # locate ``architecture notes`` for the V15 cross-reference section
    # (audit sink wiring). Falls back to ``None`` when the manifest failed to
    # load — the renderer prints a soft warning in that case.
    app.state.workspace_root = settings.workspace_root
    try:
        from git_shared import GitRepo as _GitRepo

        app.state.prompts_repo = _GitRepo(
            repo_path=settings.prompts_repo_path,
            main_branch=settings.prompts_main_branch,
        )
        logger.info(
            "prompts_repo wired at %s (main=%s)",
            settings.prompts_repo_path,
            settings.prompts_main_branch,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        app.state.prompts_repo = None
        logger.warning(
            "prompts_repo unavailable (prompt git wiring): %s — "
            "/admin/prompts will return 503 until resolved",
            exc,
        )

    # The PR opener wires Bitbucket via the foundation MCP client.
    # The router reads it off ``app.state.prompts_pr_opener``; until
    # the MCP wiring lands here we leave it ``None`` so the only
    # endpoint that needs it (``POST .../pr``) returns 503 while
    # the read / draft endpoints stay live.
    app.state.prompts_pr_opener = None

    # The audit sink that the prompts router writes to. We try to
    # build the asyncpg-backed :class:`AsyncpgAuditSink` (audit sink wiring)
    # so prompt mutation events (``prompt_draft_created``,
    # ``prompt_pr_opened``, ``prompt_pr_conflict``,
    # ``prompt_render_failed``) land in ``automation.audit_events``.
    # When asyncpg or the pool factory is unavailable (eg. in unit
    # tests, or while Postgres is still booting) we fall back to the
    # in-process :class:`LoggingAuditSink` so the ``/admin/prompts``
    # router stays answerable. The router's ``_safe_audit`` helper
    # never propagates audit failures, so a degraded sink degrades
    # observability but never fails the underlying request.
    app.state.prompts_audit_sink = await _build_prompts_audit_sink(
        dsn=settings.postgres_dsn,
    )

    # The asyncpg pool created by ``_build_prompts_audit_sink`` is
    # stashed on ``app.state.prompts_audit_pool`` so the shutdown
    # hook can close it without re-importing asyncpg.
    # When the helper falls back to the logging sink the slot stays
    # ``None``.
    app.state.prompts_audit_pool = getattr(
        app.state.prompts_audit_sink, "_pool", None
    )

    # ---- 8. Ops routers wiring ------------------------------------
    # The operations routers shipped under ``src/routers/`` expect
    # the following ``app.state`` slots:
    #
    # * ``pg_pool`` — asyncpg pool used by costs / feature_flags
    #   routers. We try to reuse the prompts audit pool so we don't
    #   open a second connection pool against the same Postgres;
    #   when the prompts wiring also failed we fall back to a fresh
    #   pool here. Both branches surface a clear 503 with
    #   ``reason="pg_pool_unavailable"`` from the routers when the
    #   pool is missing entirely.
    # * ``loki_client`` — Loki search proxy. Wired with a soft-fail
    #   stub when the deployment doesn't ship Loki (the audit search
    #   panel still renders Postgres-backed Audit results from the
    #   foundation surface).
    # * ``archive_index`` — MinIO/Postgres-backed archive index for
    #   the ``audit/search`` archived-row branch.
    # * ``healthcheck_aggregator`` — :class:`HealthcheckAggregator`
    #   built once per process; reads the manifest's
    #   ``depends_on_services`` for cascade computation.
    # * ``credential_rotation_state`` — callable returning the dept
    #   rotation TTL list for the security panel banner.
    # * ``feature_flag_audit_sink`` — bound to the AdminProxy's audit
    #   sink so toggles are observable in ``automation.audit_events``.
    #
    # Every wire is best-effort; when the underlying dependency is
    # missing the slot stays ``None`` and the router surfaces a 503
    # with the documented ``reason`` field. This mirrors the
    # PromptsGitRouter pattern so a partial rollout never blocks the
    # entire admin surface.

    # 8a. pg_pool — share the prompts pool when available, else
    #     open a dedicated one.
    pg_pool = app.state.prompts_audit_pool
    if pg_pool is None:
        try:
            import asyncpg  # type: ignore[import-not-found]

            pg_pool = await asyncpg.create_pool(
                dsn=settings.postgres_dsn,
                min_size=1,
                max_size=4,
                timeout=10.0,
            )
            logger.info(
                "ops_pg_pool created (separate from prompts audit pool)"
            )
        except Exception as exc:  # noqa: BLE001
            pg_pool = None
            logger.warning(
                "ops_pg_pool unavailable: %s — costs / feature_flags "
                "routers will return 503 until resolved",
                exc,
            )
    app.state.pg_pool = pg_pool
    # Track pool ownership so shutdown can close only the pool we
    # opened (not the prompts pool, which has its own close path).
    app.state.ops_pg_pool_owned = (
        pg_pool is not None and pg_pool is not app.state.prompts_audit_pool
    )

    # ----------------------------------------------------------------
    # SQL migration runner
    # ----------------------------------------------------------------
    # Postgres ``docker-entrypoint-initdb.d`` only runs top-level
    # ``infra/postgres/*.sql`` files on first boot. The ``migrations/``
    # subdirectory (and previously ``config/migrations/``) was never
    # applied — so tables like ``infrastructure.ssh_runners``,
    # ``llm_providers``, ``test_runs``, ``bootstrap_tokens`` are absent
    # on a fresh deployment and the admin features that depend on them
    # return 500. ``apply_migrations`` is idempotent (checksum-tracked
    # via ``shared.schema_migrations``) so we run it on every boot.
    #
    # The directory is resolved from ``MIGRATIONS_DIR`` env (mounted
    # at ``/app/migrations`` inside the container via the compose
    # volume) with a sensible repo-relative fallback so local dev
    # invocations (``python -m admin_dashboard_api``) work without
    # extra setup.
    if pg_pool is not None:
        try:
            from db_shared.migrations import (
                apply_migrations as _apply_migrations,
            )
            import os as _os

            _migrations_env = _os.environ.get("MIGRATIONS_DIR", "").strip()
            if _migrations_env:
                _migrations_path = Path(_migrations_env)
            else:
                # Fall back to ``/app/migrations`` (container) or a
                # repo-relative path (local dev / tests).
                _container_path = Path("/app/migrations")
                if _container_path.is_dir():
                    _migrations_path = _container_path
                else:
                    # services/admin-dashboard-api/src/main.py →
                    # parents[3] = platform/
                    _migrations_path = (
                        Path(__file__).resolve().parents[3]
                        / "infra"
                        / "postgres"
                        / "migrations"
                    )

            logger.info(
                "K1 migration runner starting (dir=%s)", _migrations_path
            )
            _migration_result = await _apply_migrations(
                pg_pool, _migrations_path
            )
            app.state.migration_result = _migration_result
            logger.info(
                "K1 migrations applied: newly=%d already=%d "
                "checksum_mismatches=%d",
                len(_migration_result.newly_applied),
                len(_migration_result.already_applied),
                len(_migration_result.checksum_mismatches),
            )
            if _migration_result.checksum_mismatches:
                # Loud warning — somebody modified an already-applied
                # migration on disk. Operator must investigate.
                for _mm in _migration_result.checksum_mismatches:
                    logger.error(
                        "MIGRATION DRIFT: %s stored_checksum=%s "
                        "current_checksum=%s — file modified after "
                        "deployment; runner did NOT re-apply",
                        _mm.version,
                        _mm.stored_checksum[:12],
                        _mm.current_checksum[:12],
                    )
        except Exception as exc:  # noqa: BLE001
            # A migration failure is a hard error for production
            # correctness — every downstream router assumes the schema
            # is current. We log loudly and continue lifespan so the
            # ``/healthz`` endpoint stays reachable (operators can see
            # the failure in the log), but flag the state on
            # ``app.state.migration_error`` so future readiness checks
            # can refuse traffic.
            logger.exception(
                "K1 migration runner FAILED: %s — admin features that "
                "depend on un-applied migrations will return 500 until "
                "resolved",
                exc,
            )
            app.state.migration_error = str(exc)
            app.state.migration_result = None
    else:
        logger.warning(
            "K1 migration runner SKIPPED: pg_pool unavailable; admin "
            "features depending on migration-created tables will fail"
        )
        app.state.migration_result = None

    if pg_pool is not None and app.state.http_client is not None:
        try:
            from .capability_prober import (
                AsyncpgCapabilityProbeStore,
                LiveCapabilityProber,
            )

            app.state.capability_probe_store = AsyncpgCapabilityProbeStore(pg_pool)
            app.state.capability_prober = LiveCapabilityProber(
                pool=pg_pool,
                http_client=app.state.http_client,
                vault_addr=settings.vault_addr,
                vault_token=settings.vault_token,
            )
            logger.info("capability_prober wired (live SSH/Docker probes enabled)")
        except Exception as exc:  # noqa: BLE001
            app.state.capability_prober = None
            logger.warning("capability_prober wiring failed: %s", exc)

    try:
        from .temporal_control import TemporalWorkflowControl

        app.state.temporal_workflow_client = (
            await TemporalWorkflowControl.connect(settings.temporal_host)
        )
        logger.info(
            "temporal_workflow_client wired (host=%s)",
            settings.temporal_host,
        )
    except Exception as exc:  # noqa: BLE001
        app.state.temporal_workflow_client = None
        logger.warning(
            "temporal_workflow_client unavailable: %s -- workflow control "
            "endpoints will return temporal_unavailable",
            exc,
        )

    # llm-provider-management provider audit wiring — wire the asyncpg-backed audit
    # sink onto ``app.state.audit_logger`` so the LLM provider service
    # (routers/llm_providers.py) lands its mutation/test events in the
    # ``automation.audit_events`` table.  When the pool is missing we
    # fall back to the logging sink so the audit pipeline still
    # produces structured log lines.
    if pg_pool is not None:
        try:
            from .audit_sink import AsyncpgAuditSink

            app.state.audit_logger = AsyncpgAuditSink(pool=pg_pool)
            logger.info(
                "llm_provider_audit_sink wired (AsyncpgAuditSink on pg_pool)"
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail to logging sink
            logger.warning(
                "AsyncpgAuditSink construction failed; falling back to "
                "LoggingAuditSink: %s",
                exc,
            )
            app.state.audit_logger = LoggingAuditSink()
    else:
        app.state.audit_logger = LoggingAuditSink()
        logger.info(
            "llm_provider_audit_sink falling back to LoggingAuditSink "
            "(pg_pool unavailable)"
        )

    # Feature-flag start gate.
    # The lifecycle service was constructed in block 5 with
    # ``feature_flag_reader=None``; now that the asyncpg pool is
    # available we wire :class:`AsyncpgFeatureFlagReader` so Step 1.5
    # can issue a single SELECT against ``shared.feature_flags`` per
    # ``start`` invocation. When the pool is missing (eg. Postgres
    # still booting) the gate stays inert and starts proceed without
    # the flag check — the readiness probe surfaces the missing pool
    # via the existing ``pg_pool_unavailable`` reason from the
    # ``feature_flags`` / ``costs`` routers.
    if app.state.lifecycle is not None and pg_pool is not None:
        try:
            from .lifecycle.service import AsyncpgFeatureFlagReader

            app.state.lifecycle._feature_flag_reader = (  # type: ignore[attr-defined]
                AsyncpgFeatureFlagReader(pool=pg_pool)
            )
            logger.info(
                "feature_flag_reader wired (start gate "
                "active)"
            )
        except Exception as exc:  # noqa: BLE001 — soft-fail
            logger.warning(
                "feature_flag_reader wiring failed (Step 1.5 gate inert): %s",
                exc,
            )

    # 8b. Loki client — best-effort; when missing the audit search
    #     router degrades to "0 hits from Loki, plus archive hits".
    try:
        from .clients.loki_client import LokiClient  # type: ignore[import-not-found]

        app.state.loki_client = LokiClient(
            base_url=getattr(settings, "loki_url", "http://loki:3100"),
            http_client=http_client,
        )
        logger.info("loki_client wired")
    except Exception as exc:  # noqa: BLE001
        app.state.loki_client = None
        logger.info("loki_client unavailable (soft): %s", exc)

    # 8b'. MCP metrics client — best-effort.
    #     Powers ``GET /api/v1/mcp/traffic``; when missing the endpoint
    #     surfaces 503 with ``reason="mcp_metrics_unavailable"``.
    try:
        from .clients.mcp_metrics_client import McpMetricsClient

        app.state.mcp_metrics_client = McpMetricsClient(
            base_url=settings.mcp_metrics_url,
            http_client=http_client,
        )
        logger.info(
            "mcp_metrics_client wired (base_url=%s)",
            settings.mcp_metrics_url,
        )
    except Exception as exc:  # noqa: BLE001
        app.state.mcp_metrics_client = None
        logger.info("mcp_metrics_client unavailable (soft): %s", exc)

    # 8c. Archive index — best-effort; the ``audit/search`` router
    #     skips archived-row aggregation when this slot is ``None``.
    try:
        from .audit.archive_index import ArchiveIndex  # type: ignore[import-not-found]

        if pg_pool is not None:
            app.state.archive_index = ArchiveIndex(pool=pg_pool)
            logger.info("archive_index wired")
        else:
            app.state.archive_index = None
    except Exception as exc:  # noqa: BLE001
        app.state.archive_index = None
        logger.info("archive_index unavailable (soft): %s", exc)

    # 8d. Healthcheck aggregator — built only when the manifest
    #     loaded; otherwise the cascade router has no dependency
    #     graph to reason against.
    if manifest is not None:
        try:
            from .routers.healthcheck import HealthcheckAggregator

            def _probe_state() -> dict[str, str]:
                """Return the latest per-service health snapshot.

                The :class:`HealthProbe` bound during the lifecycle
                wiring tracks every service's status in its internal
                state; we project that to a flat name → status map
                here so the aggregator can compose the cascade
                without poking at probe internals.
                """

                hp = app.state.health_probe
                if hp is None or not hasattr(hp, "snapshot"):
                    return {}
                try:
                    raw = hp.snapshot()  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    return {}
                if isinstance(raw, dict):
                    return {str(k): str(v) for k, v in raw.items()}
                return {}

            def _manifest_loader() -> dict[str, Any]:
                m = app.state.manifest
                if m is None:
                    return {}
                # ServiceManifest exposes ``entries`` as a tuple of
                # ManifestEntry dataclasses; project to plain dicts
                # so the aggregator's downstream consumers see the
                # same shape regardless of which manifest type
                # version shipped.
                return {
                    "entries": [
                        {
                            "name": getattr(e, "name", None),
                            "depends_on_services": list(
                                getattr(e, "depends_on_services", []) or []
                            ),
                        }
                        for e in getattr(m, "entries", [])
                    ]
                }

            app.state.healthcheck_aggregator = HealthcheckAggregator(
                probe_state=_probe_state,
                manifest_loader=_manifest_loader,
            )
            logger.info("healthcheck_aggregator wired (cascade enabled)")
        except Exception as exc:  # noqa: BLE001
            app.state.healthcheck_aggregator = None
            logger.warning(
                "healthcheck_aggregator wiring failed: %s", exc
            )

    # 8e. Credential rotation state — callable returning the dept
    #     rotation TTL list. The admin proxy already forwards the
    #     foundation Vault metadata to automation-service, so for
    #     this lifespan we ship a no-op default that returns an
    #     empty list. The router treats that as "no banners
    #     today" and renders nothing.
    app.state.credential_rotation_state = lambda: []

    # 8f. Feature-flag audit sink — bound to the AdminProxy's audit
    #     sink so toggles land in the same ``automation.audit_events``
    #     stream as the proxy-forwarded admin commands.
    app.state.feature_flag_audit_sink = getattr(
        admin_proxy, "_audit", LoggingAuditSink()
    )

    # ---- 8. PromptSandbox (service lifecycle wiring — operations surface) ----------
    # The sandbox runs an isolated LLM call with ``cost_tag="sandbox"``
    # so production budgets are not affected. The
    # production wiring uses the configured LLM provider directly; if
    # provider credentials are missing, sandbox invocations fail closed
    # instead of returning synthetic model output.
    try:
        from .sandbox import (
            NullCostTracker,
            ProviderLlmInvoker,
            PromptSandbox,
        )

        app.state.prompt_sandbox = PromptSandbox(
            llm=ProviderLlmInvoker(),
            cost_tracker=NullCostTracker(),
        )
        logger.info(
            "prompt_sandbox wired (configured LLM + null cost tracker; "
            "cost_tag='sandbox')"
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail
        app.state.prompt_sandbox = None
        logger.warning(
            "prompt_sandbox unavailable (service lifecycle wiring): %s — "
            "/admin/prompts/.../sandbox-test will return 503 until resolved",
            exc,
        )

    try:
        yield
    finally:
        # Shutdown — best-effort, never raises.
        try:
            await audit_writer.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AuditWriter.close() failed: %s", exc)
        # Close the asyncpg pool that backs the prompts audit sink
        # (audit sink wiring). The pool is only present when the eager
        # ``_build_prompts_audit_sink`` succeeded; the logging-sink
        # fallback leaves the slot ``None``.
        prompts_pool = getattr(app.state, "prompts_audit_pool", None)
        if prompts_pool is not None:
            try:
                await prompts_pool.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "prompts_audit_pool.close() failed: %s", exc
                )
        # Close the ops pool only when *we* opened it (the prompts
        # pool above already closed it in the shared-pool branch).
        if (
            getattr(app.state, "ops_pg_pool_owned", False)
            and app.state.pg_pool is not None
        ):
            try:
                await app.state.pg_pool.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "ops_pg_pool.close() failed: %s", exc
                )
        try:
            vault_client = getattr(app.state, "vault_client", None)
            if vault_client is not None:
                await vault_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("VaultClient.aclose() failed: %s", exc)
        try:
            await http_client.aclose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("httpx.AsyncClient.aclose() failed: %s", exc)


# ---------------------------------------------------------------------------
# Prompt audit sink helper (audit sink wiring — operations surface)
# ---------------------------------------------------------------------------


async def _build_prompts_audit_sink(*, dsn: str) -> Any:
    """Build the audit sink the prompts router writes through.

    Tries the asyncpg-backed :class:`AsyncpgAuditSink` first so prompt
    mutation events land in ``automation.audit_events`` (audit sink wiring,
    workflow events. Falls back to the in-process
    :class:`LoggingAuditSink` when:

    * :mod:`asyncpg` is not importable (rare in production, common
      in unit-test environments that pin the dependency out).
    * :func:`asyncpg.create_pool` raises (Postgres still booting,
      DSN points at the wrong host, etc).

    The fallback is intentional — the router's ``_safe_audit`` helper
    swallows write failures, but we would rather degrade to a logged
    audit envelope than block the entire ``/admin/prompts`` surface
    on a transient Postgres outage. When the asyncpg path lights up
    later (eg. after Postgres recovers) the operator can restart the
    process to upgrade.

    The returned object always satisfies the ``write(event)``
    protocol consumed by ``src.routers.prompts_git``.
    """

    try:
        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        logger.info(
            "asyncpg not available; falling back to LoggingAuditSink "
            "for prompt mutation events (audit sink wiring)."
        )
        return LoggingAuditSink(
            logger_name="admin_dashboard_api.prompts.audit"
        )

    try:
        pool = await asyncpg.create_pool(
            dsn=dsn,
            min_size=1,
            max_size=4,
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 — soft-fail to logging sink
        logger.warning(
            "asyncpg.create_pool failed for prompts audit sink "
            "(falling back to LoggingAuditSink): %s",
            exc,
        )
        return LoggingAuditSink(
            logger_name="admin_dashboard_api.prompts.audit"
        )

    from .prompts import AsyncpgAuditSink

    sink = AsyncpgAuditSink(pool=pool)
    # Cache the pool on the sink instance so the lifespan shutdown
    # hook can close it without re-reaching into the wrapper. Using
    # a dunder attribute matches the convention the AdminProxy uses
    # for its private audit handle.
    sink._pool = pool  # type: ignore[attr-defined]
    logger.info(
        "prompts audit sink wired against asyncpg pool (audit sink wiring)."
    )
    return sink


app = FastAPI(
    title="admin-dashboard-api",
    version="0.0.0",
    description="Admin Dashboard API — backs the Next.js admin UI .",
    lifespan=lifespan,
)

# Trace middleware wiring — mount :class:`TraceMiddleware` ahead of
# every other middleware / router so the resolved ``X-Trace-Id`` is
# available to every request handler (and to the AdminProxy that fans
# out to automation-service via :class:`http_shared.make_mcp_client`,
# whose request hook reads :func:`observability.get_trace_id`).
# ``app.add_middleware`` prepends to Starlette's middleware stack so
# the middleware added latest sits on the *outside* of the chain — we
# add this one immediately after :class:`FastAPI` construction so it
# remains outermost regardless of any future middleware additions.
app.add_middleware(TraceMiddleware)

# CORS middleware — allows the admin-dashboard-ui to call the API during
# local development. Origins are env-driven (single source of truth):
# set ``CORS_ALLOW_ORIGINS`` to a comma-separated list to override. The
# dev default covers the dashboard's published host port (see
# ``ADMIN_DASHBOARD_UI_HOST_PORT`` in infra/.env). Ports are NOT
# hard-coded — change the deployment env, not this source.
import os as _os

from fastapi.middleware.cors import CORSMiddleware

_cors_origins = [
    origin.strip()
    for origin in _os.environ.get(
        "CORS_ALLOW_ORIGINS",
        "http://localhost:33000,http://127.0.0.1:33000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers middleware wiring — mount :class:`SecurityHeadersMiddleware`
# so every HTTP response carries X-Frame-Options, X-Content-Type-Options
# and X-XSS-Protection headers regardless of status code or content type
# for every response.
app.add_middleware(SecurityHeadersMiddleware)

# External probe wiring — install rate limiter middleware.
# Applies per-endpoint rate limits: 100/min for
# webhook paths (IP-keyed), 60/min for admin API paths (user-keyed),
# and exempts health-check paths (/healthz, /readyz) from limiting.
# Storage backend is configured via RATE_LIMIT_STORAGE_URI env var
# (Redis URI for production, in-memory for development).
from .middleware.rate_limit import install_rate_limiter  # noqa: E402

install_rate_limiter(app)


# ---------------------------------------------------------------------------
# Optional router from service lifecycle wiring (services_lifecycle)
# ---------------------------------------------------------------------------
#
# ``services/admin-dashboard-api/src/routers/services_lifecycle.py`` exposes
# a ``router`` symbol bound to an :class:`fastapi.APIRouter`. The import below
# is allowed to fail softly so the service remains operable during partial
# deployments. When the router is present we mount it under its own prefix
# (the router itself declares ``/admin/services``).
try:  # pragma: no cover - exercised by integration tests
    from .routers.services_lifecycle import router as services_lifecycle_router
except Exception as _import_exc:  # noqa: BLE001 - any import failure is tolerated
    services_lifecycle_router = None
    # The file ``services_lifecycle.py``
    # exists — this catch is for genuine import errors (missing dep,
    # syntax error during reload, etc.), NOT "service lifecycle wiring not yet wired"
    # any more. Bumped from ``info`` to ``warning`` so an unintended
    # import failure surfaces clearly in admin logs.
    logger.warning(
        "services_lifecycle router import FAILED — admin services "
        "lifecycle endpoints will be unavailable: %s",
        _import_exc,
    )

if services_lifecycle_router is not None:  # pragma: no cover
    app.include_router(services_lifecycle_router)


# ---------------------------------------------------------------------------
app.state.departments_reload_publisher = None
app.state.dept_audit_sink = None
try:  # pragma: no cover - exercised by browser/live E2E tests
    from .routers.departments import (
        router as _departments_router_early,
        crud_router as _departments_crud_router_early,
    )

    app.include_router(_departments_router_early)
    app.include_router(_departments_crud_router_early)
    _departments_routers_mounted_early = True
except Exception as _import_exc:  # noqa: BLE001
    _departments_routers_mounted_early = False
    logger.info("departments router unavailable (early mount): %s", _import_exc)


# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised by browser/live E2E tests
    from .routers.ssh_runners import (
        router as _ssh_runners_router_early,
        dept_ssh_router as _dept_ssh_router_early,
    )

    app.include_router(_ssh_runners_router_early)
    app.include_router(_dept_ssh_router_early)
    _ssh_runners_routers_mounted_early = True
except Exception as _import_exc:  # noqa: BLE001
    _ssh_runners_routers_mounted_early = False
    logger.info("ssh_runners router unavailable (early mount): %s", _import_exc)


# AdminProxy router (admin proxy wiring — platform foundation)
# ---------------------------------------------------------------------------
#
# Mounts the BFF proxy that forwards every ``/admin/*`` route owned by
# automation-service. Imported with the same soft-
# fail pattern as the lifecycle router so a stale checkout cannot
# block the rest of the admin surface.
try:  # pragma: no cover - exercised by unit tests
    from .routers.admin_proxy import router as admin_proxy_router
except Exception as _import_exc:  # noqa: BLE001
    admin_proxy_router = None
    # Keep this at warning because admin_proxy forwards /admin/* to
    # automation-service. If the import
    # fails, the dashboard cannot reach automation-service endpoints
    # through the BFF.
    logger.warning(
        "admin_proxy router import FAILED — /admin/* forwarding to "
        "automation-service will be unavailable: %s",
        _import_exc,
    )

if admin_proxy_router is not None:  # pragma: no cover
    # Mount the LLM provider routers BEFORE the admin proxy so the more-specific
    # ``/admin/llm-providers/...`` and
    # ``/admin/departments/{dept_id}/llm-provider`` routes claim
    # their requests locally instead of being forwarded to
    # automation-service (which does not own those URLs).
    try:  # pragma: no cover - exercised by unit/invariant tests
        from .routers.llm_providers import (
            router as _llm_providers_router_early,
            department_router as _llm_providers_dept_router_early,
        )
        from .llm_providers.error_handlers import (
            register_validation_error_handler as _register_llm_validation_handler,
        )

        app.include_router(_llm_providers_router_early)
        app.include_router(_llm_providers_dept_router_early)
        _register_llm_validation_handler(app)
        _llm_providers_routers_mounted_early = True
    except Exception as _import_exc:  # noqa: BLE001
        _llm_providers_routers_mounted_early = False
        logger.info(
            "llm_providers routers (early mount) unavailable: %s",
            _import_exc,
        )

    app.include_router(admin_proxy_router)
else:
    _llm_providers_routers_mounted_early = False


# ---------------------------------------------------------------------------
# PromptsGitRouter (operations surface prompt git wiring)
# ---------------------------------------------------------------------------
#
# Mounts the ``/admin/prompts`` CRUD/draft/PR endpoints required by
# Imported with the same soft-fail pattern as the
# other admin routers so a missing GitPython install or an invalid
# ``PROMPTS_REPO_PATH`` does not block the rest of the admin
# surface — the readiness probe surfaces the failure mode via
# ``app.state.prompts_repo`` (left ``None`` in that case, which
# triggers a 503 from the dependency).
try:  # pragma: no cover - exercised by unit tests
    from .routers.prompts_git import router as prompts_git_router
except Exception as _import_exc:  # noqa: BLE001
    prompts_git_router = None
    logger.info(
        "prompts_git router unavailable (prompt git wiring wiring incomplete): %s",
        _import_exc,
    )

if prompts_git_router is not None:  # pragma: no cover
    app.include_router(prompts_git_router)


# ---------------------------------------------------------------------------
# Ops routers
# ---------------------------------------------------------------------------
#
# Mounts the BFF / aggregator routers shipped under ``src/routers/``.
# Each router is imported with the same soft-fail pattern as the other
# admin routers so a stale checkout cannot block the rest of the admin
# surface.

try:  # pragma: no cover - exercised by unit tests
    from .routers.workflows_drilldown import router as _workflows_drilldown_router
    app.include_router(_workflows_drilldown_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("workflows_drilldown router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.loki_search import (
        router as _loki_search_router,
        workflow_logs_router as _workflow_logs_router,
    )
    app.include_router(_loki_search_router)
    # Workflow-scoped log filter. Mounted alongside the audit-search surface
    # so the workflow detail page can fold a ``trace_id`` filter
    # over a single Loki round-trip.
    app.include_router(_workflow_logs_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("loki_search router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.costs import router as _costs_router
    app.include_router(_costs_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("costs router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.healthcheck import router as _healthcheck_router
    app.include_router(_healthcheck_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("healthcheck router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.feature_flags import (
        router as _feature_flags_router,
        v1_router as _feature_flags_v1_router,
    )
    app.include_router(_feature_flags_router)
    # Vault init wiring — adds /api/v1/feature-flags surface
    # alongside the legacy /admin/feature-flags listing so the dept
    # override columns and 5s confirm-dialog UX have a backing API
    # without breaking existing callers.
    app.include_router(_feature_flags_v1_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("feature_flags router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.security import router as _security_router
    app.include_router(_security_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("security router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# LLM Provider Management routers
# ---------------------------------------------------------------------------
#
# Mount the ``/admin/llm-providers`` CRUD/test surface and the sibling
# per-department override surface, then register the custom validation
# error handler so the expected 422 body shapes
# (``validation_failed``, ``unsupported_provider_type``,
# ``extra_fields_not_allowed``) reach the UI verbatim. The routers pull
# their collaborators (``pg_pool``, ``vault_client``, ``http_client``,
# audit sink) off ``app.state``; see :mod:`src.routers.llm_providers`
# for the resolution path.
if not _llm_providers_routers_mounted_early:
    # Fallback path — early mount above failed (or admin_proxy_router
    # was unavailable). Mount the routers here at the same priority as
    # the rest of the admin surface; the proxy will not claim them
    # because either it wasn't included or this is the regular order.
    try:  # pragma: no cover - exercised by unit/invariant tests
        from .routers.llm_providers import (
            router as _llm_providers_router,
            department_router as _llm_providers_dept_router,
        )
        from .llm_providers.error_handlers import (
            register_validation_error_handler,
        )

        app.include_router(_llm_providers_router)
        app.include_router(_llm_providers_dept_router)
        register_validation_error_handler(app)
    except Exception as _import_exc:  # noqa: BLE001
        logger.info(
            "llm_providers routers unavailable (wiring incomplete): %s",
            _import_exc,
        )

# ---------------------------------------------------------------------------
# Secret rotation router
# ---------------------------------------------------------------------------
#
# Mounts ``POST /api/v1/security/rotate/{kind}`` for
# ``kind ∈ {webhook_secret, bot_credential, llm_api_key}``
# 15.1–15.5). Resolves its dependencies through three ``app.state`` slots:
#
# * ``secret_rotator`` — :class:`SupportsSecretRotator` adapter that
#   performs the Vault KV-v2 write. Production wires this against
#   :mod:`vault_client`; until that lands the slot stays ``None`` and
#   the endpoint returns ``HTTP 503`` with
#   ``reason="secret_rotator_unavailable"``.
# * ``secret_reload_publisher`` — optional
#   :class:`SupportsHotReloadPublisher` (Redis pub/sub or HTTP fan-out).
#   When ``None`` the rotation logs a structured ``secret_reload_pending``
#   warning so operators can page consumers manually.
# * ``secret_rotation_audit_sink`` — optional explicit audit sink. When
#   absent the router falls back to the AdminProxy's audit sink so
#   ``secret_rotated`` events land in the same audit stream as other
#   admin actions.
app.state.secret_rotator = None
app.state.secret_reload_publisher = None
app.state.secret_rotation_audit_sink = None
try:  # pragma: no cover - exercised by unit tests
    from .routers.security import rotation_router as _security_rotation_router
    app.include_router(_security_rotation_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info(
        "security rotation router unavailable (secret rotation wiring wiring incomplete): %s",
        _import_exc,
    )


# ---------------------------------------------------------------------------
# RunnerWorkspacesRouter (platform operations workspace and prompt router wiring)
# ---------------------------------------------------------------------------
#
# Mounts the ``/admin/runner/workspaces[...]`` endpoints used by the
# *Services → Workspaces* sub-tab. Imported with the same soft-fail
# pattern as the other admin routers so a missing dependency cannot
# block the rest of the admin surface. The router resolves its SSH
# side-effect surface through ``app.state.runner_workspaces_client``;
# until production wiring lands the slot stays ``None`` and the
# router degrades to "empty list / 503" per its module docstring.
try:  # pragma: no cover - exercised by unit tests
    from .routers.runner_workspaces import (
        router as _runner_workspaces_router,
    )
    app.include_router(_runner_workspaces_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("runner_workspaces router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# OperationsRouter (platform operations operations router wiring)
# ---------------------------------------------------------------------------
#
# Mounts the ``/admin/operations/license`` endpoint that returns license
# cap usage for the operations dashboard.
# Reads directly from the asyncpg pool (``app.state.pg_pool``) — no
# proxy hop to automation-service needed. Imported with the same
# soft-fail pattern as the other admin routers.
try:  # pragma: no cover - exercised by unit tests
    from .routers.operations import router as _operations_router
    app.include_router(_operations_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("operations router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# Additional admin dashboard routers
# ---------------------------------------------------------------------------
#
# Mounts the three routers shipped under ``src/routers/``:
#
# * ``firecrawl_allowlist`` — Firecrawl egress allowlist CRUD
#   endpoints.
# * ``setup_wizard`` — Step-by-step guided setup for platform
#   services.
# * ``test_results`` — Service test results parsing and history
#   and history.
#
# Each include uses the same soft-fail pattern as the other admin
# routers so a stale checkout cannot block the rest of the admin
# surface — when any router fails to import we log the reason and
# move on, leaving the remaining endpoints answerable.

try:  # pragma: no cover - exercised by unit tests
    from .routers.firecrawl_allowlist import router as _firecrawl_allowlist_router
    app.include_router(_firecrawl_allowlist_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("firecrawl_allowlist router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.setup_wizard import router as _setup_wizard_router
    app.include_router(_setup_wizard_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("setup_wizard router unavailable: %s", _import_exc)

# ---------------------------------------------------------------------------
# AuthBootstrapRouter
# ---------------------------------------------------------------------------
#
# Mounts ``POST /auth/bootstrap`` — one-time admin user creation via
# bootstrap token. Unauthenticated by design (creates the first admin
# before OIDC is configured). Returns 410 when OIDC is active.
try:  # pragma: no cover - exercised by unit tests
    from .routers.auth_bootstrap import router as _auth_bootstrap_router
    app.include_router(_auth_bootstrap_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("auth_bootstrap router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.test_results import router as _test_results_router
    app.include_router(_test_results_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("test_results router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# VaultInitRouter
# ---------------------------------------------------------------------------
#
# Mounts ``POST /admin/vault/init`` — Vault production initialization
# with Shamir secret sharing (5 key shares, 3 threshold). Returns
# unseal keys and root token for one-time display. Returns 409 when
# Vault is already initialized.
try:  # pragma: no cover - exercised by unit tests
    from .routers.vault_init import router as _vault_init_router
    app.include_router(_vault_init_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("vault_init router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# WorkflowControlRouter
# ---------------------------------------------------------------------------
#
# Mounts the ``/api/v1/workflows/{workflow_id}/(cancel|retry|signal)``
# and ``/api/v1/workflows`` endpoints.
# Resolves its Temporal entry point through
# ``app.state.temporal_workflow_client`` — when ``None`` the
# endpoints surface ``HTTP 503`` with
# ``reason="temporal_unavailable"`` per the router's module docstring.
try:  # pragma: no cover - exercised by unit tests
    from .routers.workflow_control import router as _workflow_control_router
    app.include_router(_workflow_control_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("workflow_control router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# DepartmentsRouter — runtime CRUD
# ---------------------------------------------------------------------------
#
# Mounts the runtime ``POST/PATCH/DELETE /api/v1/departments`` surface
# The legacy ``/admin/departments`` matrix +
# repo-mapping router is mounted from the same module.
#
# Hot-reload signalling is wired through two optional ``app.state`` slots:
#
# * ``departments_reload_publisher`` — async callable
#   ``(dept_id, action) -> None`` used when a pub/sub channel is
#   available (Redis, Postgres LISTEN/NOTIFY, etc).
# * Fallback: ``POST {automation_service_url}/admin/departments/_reload``
#   through the AdminProxy's HTTP client when the publisher is missing.
#
# Both paths are best-effort — when neither is wired the next 30s config
# poll loop in each consumer picks up the new dept anyway. The slot
# stays ``None`` here; the lifespan can wire it once a real publisher
# adapter ships.
app.state.departments_reload_publisher = None
app.state.dept_audit_sink = None
if not globals().get("_departments_routers_mounted_early", False):
    try:  # pragma: no cover - exercised by unit tests
        from .routers.departments import (
            router as _departments_router,
            crud_router as _departments_crud_router,
        )

        app.include_router(_departments_router)
        app.include_router(_departments_crud_router)
    except Exception as _import_exc:  # noqa: BLE001
        logger.info("departments router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# SshRunnersRouter
# ---------------------------------------------------------------------------
#
# Mounts the SSH runner pool CRUD + department assignment endpoints
# Endpoints:
#
# * ``GET  /admin/ssh-runners``                    — list all runners.
# * ``POST /admin/ssh-runners``                    — create a new runner.
# * ``PATCH /admin/ssh-runners/{runner_id}``       — update runner fields.
# * ``GET  /admin/departments/{dept_id}/ssh-runners``  — dept's runners.
# * ``POST /admin/departments/{dept_id}/ssh-runners``  — update assignments.
#
# The router reads from ``infrastructure.ssh_runners`` and
# ``infrastructure.dept_ssh_assignments`` tables via ``app.state.pg_pool``.
# Private keys are written to Vault via ``app.state.vault_client``.
if not globals().get("_ssh_runners_routers_mounted_early", False):
    try:  # pragma: no cover - exercised by unit tests
        from .routers.ssh_runners import (
            router as _ssh_runners_router,
            dept_ssh_router as _dept_ssh_router,
        )

        app.include_router(_ssh_runners_router)
        app.include_router(_dept_ssh_router)
    except Exception as _import_exc:  # noqa: BLE001
        logger.info("ssh_runners router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# CapabilitiesRouter
# ---------------------------------------------------------------------------
#
# Mounts the ``GET /api/v1/departments/capabilities`` matrix endpoint
# and the ``POST /api/v1/departments/{dept_id}/probe/{service}``
# single-probe endpoint.
#
# The router resolves its prober + cache through two
# ``app.state`` slots:
#
# * ``capability_prober`` — :class:`SupportsCapabilityProber` adapter
#   that runs the actual ``/myself`` / ``docker info`` / etc.
#   probes. Production wires this against the foundation MCP / SSH
#   clients; until that lands the slot stays ``None`` and the
#   single-probe endpoint surfaces ``503`` with
#   ``reason="prober_unavailable"``. The matrix endpoint stays
#   answerable regardless — it only reads from the cache.
# * ``capability_probe_store`` — :class:`SupportsCapabilityProbeStore`
#   adapter backed by ``shared.capability_probes`` (capability persistence wiring). When
#   ``None`` the router auto-installs an in-memory store so the
#   matrix endpoint always has a cache to read from.
app.state.capability_prober = None
app.state.capability_probe_store = None
try:  # pragma: no cover - exercised by unit tests
    from .routers.capabilities import router as _capabilities_router
    app.include_router(_capabilities_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("capabilities router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by live browser validation
    from .routers.live_smoke import router as _live_smoke_router
    app.include_router(_live_smoke_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("live_smoke router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by live browser validation
    from .routers.automation_e2e_smoke import router as _automation_e2e_smoke_router
    app.include_router(_automation_e2e_smoke_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("automation_e2e_smoke router unavailable: %s", _import_exc)

try:  # pragma: no cover - exercised by unit tests
    from .routers.po_review_proxy import router as _po_review_proxy_router
    app.include_router(_po_review_proxy_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("po_review_proxy router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# McpTrafficRouter
# ---------------------------------------------------------------------------
#
# Mounts ``GET /api/v1/mcp/traffic`` — the admin dashboard's view into
# the MCP server's request counter. The router
# resolves its scrape client through ``app.state.mcp_metrics_client``;
# when ``None`` the endpoint surfaces ``HTTP 503`` with
# ``reason="mcp_metrics_unavailable"`` per the router's module docstring.
try:  # pragma: no cover - exercised by unit tests
    from .routers.mcp_traffic import router as _mcp_traffic_router
    app.include_router(_mcp_traffic_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("mcp_traffic router unavailable: %s", _import_exc)

# ---------------------------------------------------------------------------
# ActiveWorkflowsRouter
# ---------------------------------------------------------------------------
#
# Mounts ``GET /api/v1/departments/{dept_id}/active-workflows`` — the
# admin dashboard's view of the per-dept concurrency saturation badge
# The router reads ``automation.work_items`` via
# ``app.state.pg_pool``; when the pool is missing the endpoint
# surfaces ``HTTP 503`` with ``reason="pg_pool_unavailable"``.
try:  # pragma: no cover - exercised by unit tests
    from .routers.active_workflows import router as _active_workflows_router
    app.include_router(_active_workflows_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("active_workflows router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# TestRunnerRouter
# ---------------------------------------------------------------------------
#
# Mounts ``POST /admin/services/{service_name}/test`` with true
# line-by-line SSE streaming via subprocess. When ``stream=True`` the
# endpoint spawns the service's test command and streams stdout/stderr
# as SSE ``data:`` events with a final ``event: done`` carrying the
# exit code. Handles client disconnect by terminating the subprocess.
#
# NOTE: The ``services_lifecycle`` router (service lifecycle wiring) also mounts a
# ``/admin/services/{name}/test`` endpoint that captures full output
# before slicing into SSE frames. When both routers are present,
# FastAPI resolves the first matching route — since
# ``services_lifecycle`` is mounted earlier, it takes precedence.
# This router serves as the standalone implementation for environments
# where the full lifecycle surface is not available, and exports the
# streaming infrastructure (:func:`stream_subprocess_sse`) for reuse.
#
try:  # pragma: no cover - exercised by unit tests
    from .routers.test_runner import router as _test_runner_router
    app.include_router(_test_runner_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("test_runner router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# PromptsRouter — CRUD + Sandbox + Commit
# ---------------------------------------------------------------------------
#
# Mounts the ``/api/v1/prompts`` surface:
#
# * ``GET    /api/v1/prompts``                  — list prompt files.
# * ``GET    /api/v1/prompts/{name}``           — read current content.
# * ``POST   /api/v1/prompts/{name}/sandbox``   — isolated LLM call.
# * ``POST   /api/v1/prompts/{name}/commit``    — branch + push + draft PR.
#
# Resolves three side-effect surfaces through ``app.state`` slots so
# production wires real backends and tests inject fakes:
#
# * ``prompts_llm`` — :class:`SupportsLLMClient` for the sandbox call.
#   When ``None`` ``POST .../sandbox`` returns ``HTTP 503`` with
#   ``reason="prompts_llm_unavailable"``.
# * ``prompts_committer`` — :class:`SupportsPromptCommitter` for the
#   git branch + push. When ``None`` ``POST .../commit`` returns 503
#   with ``reason="prompts_committer_unavailable"``.
# * ``prompts_bitbucket`` — :class:`SupportsBitbucketClient` for the
#   draft PR. When ``None`` ``POST .../commit`` returns 503 with
#   ``reason="prompts_bitbucket_unavailable"``.
#
# The version row insert into ``shared.prompt_versions`` reads through
# ``app.state.pg_pool`` (already wired earlier in the lifespan); the
# audit event uses the same fallback chain as ``workflow_control``
# (explicit ``app.state.prompts_audit_sink`` slot, falling back to the
# AdminProxy's audit sink). The slots are pre-initialised inside the
# lifespan so a unit test that constructs the router against an
# already-wired ``app.state`` does not need to reset them by hand.
try:  # pragma: no cover - exercised by unit tests
    from .routers.prompts import router as _prompts_router
    app.include_router(_prompts_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("prompts router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# ExternalProvidersRouter
# ---------------------------------------------------------------------------
#
# Mounts ``GET /api/v1/services/external`` — reads ``kind="external"``
# entries from the services manifest, probes each provider via
# :func:`~src.lifecycle.external_probe.probe_external`, and returns
# aggregated status results.
try:  # pragma: no cover - exercised by unit tests
    from .routers.external_providers import router as _external_providers_router
    app.include_router(_external_providers_router)
except Exception as _import_exc:  # noqa: BLE001
    logger.info("external_providers router unavailable: %s", _import_exc)


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def get_lifecycle_service(request: Request) -> "LifecycleService":
    """Return the process-wide :class:`LifecycleService` singleton.

    The instance is created during :func:`lifespan` and stashed on
    ``app.state.lifecycle``. When the manifest failed to load we have
    no service to hand out — the dependency raises ``503`` with the
    canonical ``manifest_invalid`` reason so individual lifecycle
    endpoints surface the same failure mode as ``/readyz``.

    Raises:
        HTTPException(503): When the manifest could not be loaded and
            no LifecycleService was constructed.
    """

    lifecycle = getattr(request.app.state, "lifecycle", None)
    if lifecycle is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "manifest_invalid"},
        )
    return lifecycle


# ---------------------------------------------------------------------------
# Health & readiness probes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Liveness probe — returns 200 as long as the process is up.

    Returns HTTP 503 with ``{"status": "not_ready", "reason":
    "insecure_credentials"}`` when the credential guard has blocked
    boot due to dev-default credentials in production.

    Otherwise, stays at 200 even when the manifest failed to load.
    ``/healthz`` is the signal Compose / Kubernetes uses to decide
    whether to restart the container; restarting on a manifest error
    would create a flap loop without fixing anything (the operator must
    edit ``config/services.manifest.json`` and the process picks up the
    new file on next start). Readiness is the correct surface for
    "service can do work" — see :func:`readyz`.
    """

    credential_blocked = getattr(request.app.state, "credential_blocked", False)
    if credential_blocked:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "insecure_credentials"},
        )

    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe with real dependency checks.

    Probes PostgreSQL (``SELECT 1``) and Vault (``/v1/sys/health``)
    in parallel with a 3-second per-probe timeout.

    Returns 200 ``{"status": "ready"}`` when:

    * All dependency probes pass (PostgreSQL, Vault), **and**
    * the manifest loaded cleanly during lifespan.

    Returns 503 ``{"status": "not_ready", "reason": "manifest_invalid"}``
    when ``load_manifest`` raised
    :class:`ManifestLoadError`.

    Returns 503 ``{"status": "not_ready", "failed_dependencies": [...]}``
    when any dependency probe fails.
    """

    manifest_error = getattr(request.app.state, "manifest_error", None)
    if manifest_error is not None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "reason": "manifest_invalid"},
        )

    from .lifecycle import readiness as _readiness

    all_ready, details = await _readiness.check_readiness([
        lambda: _readiness.probe_postgres(settings.postgres_dsn),
        lambda: _readiness.probe_vault(settings.vault_addr),
    ])

    if not all_ready:
        return JSONResponse(status_code=503, content=details)
    return JSONResponse(status_code=200, content=details)


def run() -> None:  # pragma: no cover - thin uvicorn shim
    """Convenience launcher used by Standalone Mode (``python -m src.main``).

    Compose and the ``Dockerfile`` invoke ``uvicorn src.main:app`` directly,
    so this helper is only useful for local debugging without uvicorn on the
    PATH.
    """

    import uvicorn  # imported lazily so the package import stays light

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    run()
