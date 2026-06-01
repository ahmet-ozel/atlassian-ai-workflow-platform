"""``automation-worker`` boot script.

Hosts the **single** Temporal worker that polls the ``automation-tq``
task queue (workflows-spec Requirements 1.1, 1.2 — *"a worker SHALL
listen on exactly one task queue"*). The queue name is sourced from
:func:`temporal_shared.workflow_registry.task_queue_for` so the boot
script and the workflow modules share a single source of truth — the
queue string is never duplicated as a string literal anywhere in the
worker package.

The worker registers three workflows:

* :class:`AutomationWorkflow` — webhook-triggered gateway plus the
  capability gate + workflow_type router (workflows-spec task 2.1 /
  Requirements 1.1, 6.1, 6.2, 6.4, 7.9).
* :class:`BotBranchRetention` — daily cron (02:30 UTC) that deletes
  ``ai/{issue_key}`` branches older than 30 days whose linked Jira
  issue is closed (workflows-spec task 2.4 / Requirement 10.2,
  MIMARI §16.16 N5).
* :class:`AuditPruneWorkflow` — daily cron (03:00 UTC) that archives
  ``audit_events`` to MinIO before deleting them (ops-spec task 13.1
  / Requirements 6.3, 6.4).

The boot script's only job is to construct the collaborator graph
(Postgres pool, MinIO settings, NotificationService) and hand it to
the activity-side setters declared in
:mod:`automation_worker.activities.audit_prune` before the worker
enters its run loop. Configuration is read from the process
environment (no ``.env`` file loader at this layer — Compose /
Kubernetes injects the values). The keys consumed are documented in
``platform/workers/automation-worker/.env.example``.

Validates Requirements:
    * 1.1, 1.2 — three-workflow / single-queue invariant.
    * 6.3 — daily cron archives ``audit_events`` older than
      ``RETENTION_DAYS`` to MinIO and then deletes them.
    * 6.4 — any failure invokes the mandatory admin Slack alarm
      via the ``notify_audit_prune_failed`` activity.
    * 10.2 — daily cron deletes stale ``ai/*`` branches.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Any

# Avoid importing temporalio at module scope when only documentation is
# being rendered; the runtime dependency is required for ``run()``.
try:  # pragma: no cover — exercised at boot only
    from temporalio.client import Client, ScheduleHandle
    from temporalio.worker import Worker
    from temporalio.service import RPCError
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment,misc]
    Worker = None  # type: ignore[assignment,misc]
    ScheduleHandle = None  # type: ignore[assignment,misc]
    RPCError = Exception  # type: ignore[assignment,misc]

from automation_worker.activities import audit_prune as audit_prune_activities
from automation_worker.activities import (
    iteration_manager as iteration_manager_activities,
)
from automation_worker.activities import (
    notification_dispatch as notification_dispatch_activities,
)
from automation_worker.activities import output_actions as output_actions_module
from automation_worker.activities import platform_io as platform_io_activities
from automation_worker.activities import repo_resolver as repo_resolver_module
from automation_worker.activities import task_analyzer as task_analyzer_module
from automation_worker.workflows import (
    AUDIT_PRUNE_CRON_SCHEDULE,
    AUDIT_PRUNE_WORKFLOW_ID,
    BOT_BRANCH_RETENTION_CRON_SCHEDULE,
    BOT_BRANCH_RETENTION_WORKFLOW_ID,
    WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE,
    WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID,
    ApprovalGateWorkflow,
    AuditPruneWorkflow,
    AutomationWorkflow,
    BotBranchRetention,
    EpicSubtaskWorkflow,
    IterationWorkflow,
    MultiStepWorkflow,
    WorkspaceCleanupSchedulerWorkflow,
)

# platform-completion task 26.3 — new activities registered alongside
# the audit-prune family. Imported here as plain callables; the
# ``@activity.defn`` decorator on each function carries the wire
# name so the workflow side can resolve them via
# ``workflow.execute_activity("execute_output_actions", ...)``.
from automation_worker.activities.branch_rules import evaluate_branch_rules
from automation_worker.activities.mcp_caller import build_default_mcp_caller
from automation_worker.activities.output_actions import execute_output_actions
from automation_worker.activities.platform_io import (
    audit_write,
    jira_add_comment,
    jira_transition_issue,
    load_branch_pattern_rules,
    noop_test_post_result,
    prepare_task_analysis_input,
)
from automation_worker.activities.repo_resolver import resolve_repo_field
from automation_worker.activities.status_mapping import resolve_jira_status
from automation_worker.activities.task_analyzer import analyze_task

# Pull the queue name from the shared registry so the boot script and
# the workflow modules cannot drift apart on the queue string. The
# helper raises ``KeyError`` for unknown workflow names, which means
# adding a new workflow to this worker also requires extending the
# registry — the worker boot cannot silently fall through to a
# default queue (workflows-spec R1.2).
from temporal_shared.workflow_registry import task_queue_for

#: Single source of truth for the queue this worker polls. Materialised
#: at module import time so tests can assert the value without spinning
#: up the worker; the lookup is pure and side-effect-free so it is safe
#: to run during import.
AUTOMATION_TASK_QUEUE: str = task_queue_for("AutomationWorkflow")


_LOG = logging.getLogger("automation_worker")


# ---------------------------------------------------------------------------
# Environment-backed settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MinioSettings:
    """MinIO connection block consumed by ``archive_audit_to_minio``."""

    endpoint: str
    access_key: str
    secret_key: str
    use_ssl: bool
    region: str


def _load_minio_settings() -> _MinioSettings:
    """Build :class:`_MinioSettings` from ``MINIO_*`` env vars.

    Mirrors the keys used by :mod:`execution-runner-worker.activities.minio`
    so a deployment that already configures one worker also configures
    this one without renames.
    """

    endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    access_key = os.environ.get("MINIO_ROOT_USER", "")
    secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "")
    use_ssl = os.environ.get("MINIO_USE_SSL", "false").lower() in (
        "1",
        "true",
        "yes",
    )
    region = os.environ.get("MINIO_REGION", "us-east-1")
    return _MinioSettings(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        use_ssl=use_ssl,
        region=region,
    )


def _temporal_address() -> str:
    return os.environ.get("TEMPORAL_HOST", "temporal:7233")


def _postgres_dsn() -> str:
    return os.environ.get(
        "POSTGRES_DSN", "postgresql://ai:ai_dev_only@postgres:5432/ai"
    )


# ---------------------------------------------------------------------------
# Collaborator construction
# ---------------------------------------------------------------------------


async def _build_postgres_pool() -> object:
    """Construct the asyncpg pool consumed by the audit activities.

    Imported lazily so the boot script remains importable without the
    ``asyncpg`` dependency (the test suite stubs the pool via
    :func:`audit_prune_activities.set_db_pool`).
    """

    import asyncpg  # type: ignore[import-not-found]

    return await asyncpg.create_pool(
        dsn=_postgres_dsn(),
        min_size=1,
        max_size=4,
        timeout=10.0,
    )


def _build_notification_service() -> object:
    """Construct a :class:`NotificationService` for the alarm activity.

    Wires the production concrete adapters from
    :mod:`notification.concrete_adapters`:

    * :class:`AiohttpSlackAdapter` — POSTs to the admin Slack webhook
      resolved from ``SLACK_ADMIN_WEBHOOK`` (or
      ``vault:notifications/slack/admin`` when Vault credentials are
      available).
    * :class:`AsyncpgNotificationLogStore` — backed by the same
      Postgres pool the audit activities use; we open a separate
      connection here because the audit pool is bound to the
      activity-side setter.
    * The :class:`prompts.PromptLoader` from the boot script is
      reused via the in-process module-level cache; the dispatcher
      depends only on the Protocol-typed ``render`` method.

    Failure modes are non-fatal — when any adapter cannot be
    constructed (eg. no Slack webhook configured) we fall back to a
    log-only stub and log a clear warning so the operator can fix
    the configuration without restarting the worker.
    """

    try:
        from notification import (  # type: ignore[import-not-found]
            AiohttpSlackAdapter,
            AsyncpgNotificationLogStore,
            NotificationService,
        )
    except ImportError as exc:
        _LOG.warning(
            "libs/notification not importable (%s); audit_prune_failed "
            "alarm will fall back to a stub.",
            exc,
        )
        return _audit_prune_stub_service()

    admin_webhook = os.environ.get("SLACK_ADMIN_WEBHOOK", "").strip()
    if not admin_webhook:
        _LOG.warning(
            "SLACK_ADMIN_WEBHOOK not set; audit_prune_failed alarm will "
            "fall back to a stub. Configure the webhook in the deployment "
            "env file before going to production."
        )
        return _audit_prune_stub_service()

    try:
        import aiohttp  # type: ignore[import-not-found]
    except ImportError:
        _LOG.warning(
            "aiohttp not available; audit_prune_failed alarm will use stub."
        )
        return _audit_prune_stub_service()

    # We construct the aiohttp session lazily on first dispatch so the
    # boot path doesn't pay the cost when the workflow runs only once
    # per day. The wrapper service below builds the session on demand
    # and shares it across alarm calls.
    return _LazyNotificationService(
        admin_webhook=admin_webhook,
        postgres_dsn=_postgres_dsn(),
    )


def _audit_prune_stub_service() -> object:
    """Stub used when concrete adapters cannot be wired."""

    class _StubService:
        async def notify_audit_prune_failed(self, *, error: object) -> None:
            _LOG.error(
                "audit_prune_failed alarm (stub): %s",
                error,
            )

        async def notify_workflow_completion(
            self,
            *,
            workflow_id: str,
            dept: object,
            result: object,
            prompt_vars: object | None = None,
        ) -> None:
            _LOG.info(
                "workflow completion notification skipped (stub): %s",
                workflow_id,
            )

    return _StubService()


def _build_credential_resolver() -> Any:
    """Construct the worker-local Atlassian credential resolver.

    Used by the production HTTP-backed MCP caller (platform-gap-fill
    task 8.2 / Requirement 9.3) so each ``execute_output_actions``
    dispatch can inject the correct department's Atlassian
    credentials via :func:`http_shared.with_atlassian_creds`.

    Raises
    ------
    Exception
        Re-raised when the resolver cannot be constructed (missing
        Vault address, missing decision module, etc.). The caller
        catches it, logs a warning, and continues without a wired
        MCP caller — :func:`output_actions.execute_output_actions`
        will fail loudly at the first call instead.
    """

    # The shared automation-service credential resolver expects a
    # Vault address + token in the environment; both are required at
    # boot in production deployments. Local dev / tests will catch
    # the ``ImportError`` or ``RuntimeError`` and fall through to
    # the warning path in ``_run_async``.
    from vault_client import VaultPath, make_client  # type: ignore[import-not-found]

    vault = make_client(os.environ)

    class _VaultAtlassianCredentialResolver:
        async def get(
            self,
            dept_id: str,
            service: str,
            *,
            scope: str = "org",
        ) -> dict[str, str]:
            if scope != "org":
                raise ValueError("automation-worker only supports org scope")
            if service not in {"jira", "bitbucket", "confluence"}:
                raise ValueError(
                    "service must be one of jira, bitbucket, confluence"
                )
            path = VaultPath.parse(f"vault:atlassian/{dept_id}/{service}")
            return dict(vault.read(path))

    return _VaultAtlassianCredentialResolver()


class _LazyNotificationService:
    """Defer aiohttp session + asyncpg pool creation until first dispatch.

    Mirrors the public surface of :class:`NotificationService` but only
    exposes :meth:`notify_audit_prune_failed` because that's the single
    entry point the audit prune activity calls. On the first call we
    construct the aiohttp session, the asyncpg pool, the Slack adapter
    and the notification log store, then build the real
    :class:`NotificationService` and forward the call.

    Concurrent first-callers are serialised through an asyncio lock so
    we don't open two sessions; subsequent calls reuse the cached
    service instance.
    """

    def __init__(self, *, admin_webhook: str, postgres_dsn: str) -> None:
        self._admin_webhook = admin_webhook
        self._postgres_dsn = postgres_dsn
        self._service: object | None = None
        self._session: object | None = None
        self._pool: object | None = None
        self._lock = asyncio.Lock()

    async def _ensure_built(self) -> object:
        if self._service is not None:
            return self._service
        async with self._lock:
            if self._service is not None:
                return self._service
            import aiohttp  # type: ignore[import-not-found]
            import asyncpg  # type: ignore[import-not-found]

            from notification import (  # type: ignore[import-not-found]
                AiohttpSlackAdapter,
                AsyncpgNotificationLogStore,
                NotificationService,
            )

            session = aiohttp.ClientSession()
            pool = await asyncpg.create_pool(
                dsn=self._postgres_dsn,
                min_size=1,
                max_size=2,
                timeout=10.0,
            )
            slack = AiohttpSlackAdapter(
                session=session,
                admin_webhook=self._admin_webhook,
            )
            store = AsyncpgNotificationLogStore(pool=pool)
            service = NotificationService(
                slack=slack,
                email=_unused_email_adapter(),
                prompts=_audit_prune_prompt_renderer(),
                log_store=store,
            )
            self._session = session
            self._pool = pool
            self._service = service
            return service

    async def notify_audit_prune_failed(self, *, error: object) -> None:
        try:
            service = await self._ensure_built()
            await service.notify_audit_prune_failed(error=str(error))
        except Exception as exc:  # noqa: BLE001
            _LOG.error(
                "audit_prune_failed alarm dispatch failed: %s", exc
            )
            # Re-raise so Temporal's RetryPolicy can replay.
            raise

    async def notify_workflow_completion(
        self,
        *,
        workflow_id: str,
        dept: object,
        result: object,
        prompt_vars: object | None = None,
    ) -> object:
        """Forward the workflow-completion dispatch to the real service.

        Mirrors :meth:`notify_audit_prune_failed`: the underlying
        :class:`NotificationService` is built on first use (shared
        aiohttp session + asyncpg pool + Slack adapter +
        notification_log store), then the call is forwarded
        verbatim. Callers receive the same
        :class:`NotificationOutcome` they would get if they had
        constructed the service eagerly at boot.

        Errors are **not** swallowed at this layer — the
        :func:`dispatch_notification` activity wraps the call in its
        own best-effort ``try / except`` so the workflow's terminal
        path is never blocked by a transport hiccup.
        """

        service = await self._ensure_built()
        return await service.notify_workflow_completion(  # type: ignore[union-attr]
            workflow_id=workflow_id,
            dept=dept,
            result=result,
            prompt_vars=prompt_vars,
        )


def _unused_email_adapter() -> object:
    """Stand-in email adapter used when only Slack is configured.

    The audit prune alarm only uses Slack, so the email adapter is
    never invoked through this code path. We surface a deliberately
    raise-on-call stub so a future change that accidentally wires
    email into the alarm path fails loudly.
    """

    class _RaisingEmailAdapter:
        async def send(self, body: str, *, to: str) -> None:
            raise RuntimeError(
                "email adapter not configured for the audit_prune alarm "
                "path; this code should never be invoked."
            )

    return _RaisingEmailAdapter()


def _audit_prune_prompt_renderer() -> object:
    """File-backed prompt renderer for the ``audit_prune_failed`` template.

    The full :class:`prompts.PromptLoader` is wired in
    :mod:`assistant_service` for hot-reload; the worker only needs
    the single ``audit_prune_failed`` template, so we ship a minimal
    file-backed renderer that reads the file each call. The file
    does not change frequently and a failed alarm is a once-per-day
    event, so the lack of caching is irrelevant.
    """

    from pathlib import Path

    class _FileRenderer:
        def render(self, name: str, *, vars: object) -> str:
            # The standard layout puts the template under
            # ``platform/prompts/<name>.md``; resolve relative to
            # this module file (workers/.../main.py → parents[4]).
            module_path = Path(__file__).resolve()
            try:
                workspace = module_path.parents[5]
            except IndexError:  # pragma: no cover
                workspace = module_path.parent
            template_path = workspace / "platform" / "prompts" / f"{name}.md"
            if not template_path.is_file():
                return f"[template not found: {name}]"
            body = template_path.read_text(encoding="utf-8")
            if isinstance(vars, dict):
                try:
                    return body.format(**vars)
                except KeyError:
                    return body
            return body

    return _FileRenderer()


# ---------------------------------------------------------------------------
# Cron schedule registration
# ---------------------------------------------------------------------------


async def _ensure_audit_prune_schedule(client: object) -> None:
    """Idempotently register the daily ``audit-prune-cron`` schedule.

    Uses Temporal's first-class ``Schedule`` API so the cron survives
    worker restarts. Calling this function repeatedly is safe — when
    a schedule with the canonical ID already exists we leave it
    alone; otherwise we create it with ``cron_schedule="0 3 * * *"``.
    """

    if Client is None:  # pragma: no cover — guarded import path
        raise RuntimeError(
            "temporalio is required to register the audit-prune cron"
        )

    from temporalio.client import (  # type: ignore[import-not-found]
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleSpec,
    )

    schedule_id = AUDIT_PRUNE_WORKFLOW_ID
    try:
        # When the schedule already exists this returns a handle; we
        # do not need to re-create it. The probe call below confirms
        # the schedule responds.
        handle = client.get_schedule_handle(schedule_id)  # type: ignore[attr-defined]
        await handle.describe()
        _LOG.info(
            "audit-prune cron already registered (id=%s)", schedule_id
        )
        return
    except RPCError:
        # Not found — fall through to create.
        pass
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "audit-prune schedule probe failed (%s); attempting create",
            exc,
        )

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            AuditPruneWorkflow.run,
            id=schedule_id,
            task_queue=AUTOMATION_TASK_QUEUE,
        ),
        spec=ScheduleSpec(cron_expressions=[AUDIT_PRUNE_CRON_SCHEDULE]),
    )
    try:
        await client.create_schedule(schedule_id, schedule)  # type: ignore[attr-defined]
        _LOG.info(
            "audit-prune cron registered: id=%s, cron=%s, queue=%s",
            schedule_id,
            AUDIT_PRUNE_CRON_SCHEDULE,
            AUTOMATION_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.error(
            "failed to create audit-prune cron schedule: %s", exc
        )
        raise


async def _ensure_workspace_cleanup_schedule(client: object) -> None:
    """Idempotently register the hourly ``workspace-cleanup-scheduler-cron``.

    Single-runner canonical contract — G2. Mirrors the pattern of
    :func:`_ensure_audit_prune_schedule`: try ``describe`` first,
    create only when not found. Failure to register is logged but
    does not abort the worker boot — operators can drive an emergency
    prune via admin-dashboard while a misconfigured Temporal cluster
    is fixed.
    """

    if Client is None:  # pragma: no cover — guarded import path
        raise RuntimeError(
            "temporalio is required to register the workspace-cleanup cron"
        )

    from temporalio.client import (  # type: ignore[import-not-found]
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleSpec,
    )

    schedule_id = WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID
    try:
        handle = client.get_schedule_handle(schedule_id)  # type: ignore[attr-defined]
        await handle.describe()
        _LOG.info(
            "workspace-cleanup cron already registered (id=%s)",
            schedule_id,
        )
        return
    except RPCError:
        pass
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "workspace-cleanup schedule probe failed (%s); attempting "
            "create",
            exc,
        )

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            WorkspaceCleanupSchedulerWorkflow.run,
            id=schedule_id,
            task_queue=AUTOMATION_TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            cron_expressions=[WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE]
        ),
    )
    try:
        await client.create_schedule(schedule_id, schedule)  # type: ignore[attr-defined]
        _LOG.info(
            "workspace-cleanup cron registered: id=%s, cron=%s, queue=%s",
            schedule_id,
            WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE,
            AUTOMATION_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.error(
            "failed to create workspace-cleanup cron schedule: %s", exc
        )
        raise


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def _run_async() -> None:
    if Client is None or Worker is None:  # pragma: no cover
        raise RuntimeError(
            "temporalio is required to run automation-worker"
        )

    # platform-gap-fill task 7.2 / Requirement 8.3 — install the
    # :class:`TraceLogFilter` on the root logger BEFORE ``basicConfig``
    # so every log record routed through the root handler chain has
    # the ``trace_id`` attribute populated, even when the trace_id is
    # the empty string (no active context).  Activities call
    # :func:`observability.set_trace_id(input.trace_id)` at entry
    # (see e.g. :func:`automation_worker.activities.task_analyzer.analyze_task`)
    # which sets the context variable; the filter fans the value out
    # into the log record's ``%(trace_id)s`` placeholder.  Wrapped in
    # try/except so a missing observability lib (in focused unit
    # tests) does not block worker startup — in that branch we fall
    # back to a format string without the trace_id placeholder so
    # ``logging.basicConfig`` cannot raise ``KeyError`` later on.
    log_format = (
        "%(asctime)s %(levelname)s %(name)s [trace=%(trace_id)s] %(message)s"
    )
    trace_log_filter: object | None = None
    try:
        from observability import TraceLogFilter

        trace_log_filter = TraceLogFilter()
        logging.getLogger().addFilter(trace_log_filter)
    except Exception as exc:  # pragma: no cover - defensive
        _LOG.warning(
            "TraceLogFilter unavailable (%s); log records will not "
            "carry trace_id field but the worker continues.",
            exc,
        )
        log_format = "%(asctime)s %(levelname)s %(name)s %(message)s"

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=log_format,
    )
    if trace_log_filter is not None:
        for handler in logging.getLogger().handlers:
            handler.addFilter(trace_log_filter)

    # Y4 fix (GEREKSINIM_ANALIZI.md): install the redaction filter
    # before any activity logs run. The automation-worker handles Jira
    # / Bitbucket / Confluence tokens via the MCP caller — leaks here
    # surface in the workflow audit trail.
    from http_shared import install_redaction_filter  # noqa: PLC0415

    install_redaction_filter(
        loggers=[logging.getLogger()], attach_to_root=True
    )

    # ---- Wire activity-side collaborators -----------------------------
    audit_prune_activities.set_minio_settings(_load_minio_settings())
    notification_service = _build_notification_service()
    audit_prune_activities.set_notification_service(notification_service)
    # Share the same NotificationService instance with the
    # workflow-completion dispatch activity so the worker only
    # constructs one underlying service / one Slack adapter / one
    # notification_log pool. The activity tolerates a stub /
    # missing service (logs + skips) so dev environments without a
    # real Slack webhook still boot cleanly.
    notification_dispatch_activities.set_notification_service(
        notification_service
    )

    # platform-gap-fill task 8.2 / Requirement 9.3 — wire the
    # production HTTP-based MCP caller so every outbound MCP request
    # from the ``execute_output_actions`` activity carries
    # ``X-Client-Source: automation-worker``. The caller resolves
    # Atlassian credentials per request via ``with_atlassian_creds``
    # and uses the canonical ``http_shared.make_mcp_client`` factory
    # under the hood (which also injects ``X-Trace-Id`` from the
    # contextvars trace plumbing). When the credential resolver
    # cannot be constructed (e.g. local dev without Vault wired up),
    # we log + continue; the activity will surface a clear error if
    # it ever tries to call the unset caller.
    try:
        credential_resolver = _build_credential_resolver()
        mcp_caller = build_default_mcp_caller(credential_resolver)
        output_actions_module.set_mcp_caller(mcp_caller)
        platform_io_activities.set_mcp_caller(mcp_caller)
        commenter = platform_io_activities.MCPJiraCommenter(mcp_caller)
        transitioner = platform_io_activities.MCPJiraTransitioner(mcp_caller)
        task_analyzer_module.set_jira_commenter(commenter)
        repo_resolver_module.set_jira_commenter(commenter)
        repo_resolver_module.set_jira_transitioner(transitioner)
        repo_resolver_module.set_llm_parser(platform_io_activities.SimpleRepoParser())
        _LOG.info(
            "automation-worker: MCP caller wired with X-Client-Source=automation-worker"
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "automation-worker: MCP caller not wired (%s); "
            "execute_output_actions will fail until credentials "
            "and the caller are available.",
            exc,
        )

    try:
        from llm_orchestrator.provider import LLMProviderFactory

        primary, fallback = LLMProviderFactory.from_env_with_fallback()
        task_analyzer_module.set_llm_caller(
            platform_io_activities.ProviderLLMCaller(primary, fallback)
        )
        _LOG.info("automation-worker: task analyzer LLM caller wired")
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "automation-worker: task analyzer LLM caller not wired (%s); "
            "analyze_task will fail until an LLM provider is configured.",
            exc,
        )

    # Postgres pool may fail to open in dev — log + continue so the
    # worker's other activities can still be scheduled. The cron
    # itself remains registered; AuditPruneWorkflow's lookup activity
    # will surface a clear RuntimeError when it tries to use the pool.
    try:
        pool = await _build_postgres_pool()
        audit_prune_activities.set_db_pool(pool)
        # platform-gap-fill task 22.3 — prepare_iteration also
        # consults Postgres (``shared.workflow_iterations``); share
        # the same pool so the worker only opens one connection
        # tree.  ``set_workspace_base_path`` is honoured by
        # ``build_iteration_workspace_path`` so the path layout
        # matches the execution-runner-worker's RUNNER_BASE_PATH.
        iteration_manager_activities.set_db_pool(pool)
        iteration_manager_activities.set_workspace_base_path(
            os.environ.get(
                "RUNNER_BASE_PATH",
                os.environ.get(
                    "SSH_BASE_PATH",
                    iteration_manager_activities.DEFAULT_WORKSPACE_BASE_PATH,
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "postgres pool unavailable at boot (%s); audit-prune "
            "activities will fail until the connection recovers.",
            exc,
        )

    # ---- Connect to Temporal ------------------------------------------
    client = await Client.connect(_temporal_address())

    # ---- Register the daily cron schedule -----------------------------
    try:
        await _ensure_audit_prune_schedule(client)
    except Exception as exc:  # noqa: BLE001
        # Do not crash the worker boot on schedule registration
        # issues — the worker can still consume one-off workflows
        # while an operator fixes the schedule manually.
        _LOG.error(
            "audit-prune schedule registration failed; worker will "
            "continue running: %s",
            exc,
        )

    # Single-runner canonical contract — G2: hourly workspace disk
    # auto-prune cron. Same best-effort registration semantics as the
    # audit-prune cron above.
    try:
        await _ensure_workspace_cleanup_schedule(client)
    except Exception as exc:  # noqa: BLE001
        _LOG.error(
            "workspace-cleanup schedule registration failed; worker "
            "will continue running: %s",
            exc,
        )

    # ---- Build the worker ---------------------------------------------
    #
    # Single ``Worker(task_queue=...)`` call — workflows-spec R1.2
    # mandates that a worker listens on exactly one task queue.
    # Three workflows share the queue:
    #
    #   * ``AutomationWorkflow``    — the gateway / capability gate /
    #     workflow_type router invoked from the webhook handler via
    #     ``signalWithStart``.
    #   * ``BotBranchRetention``    — daily cron at 02:30 UTC.
    #   * ``AuditPruneWorkflow``    — daily cron at 03:00 UTC.
    #
    # Activity registration follows the same single-queue contract:
    # only the ``audit_prune`` activities are wired here today.  The
    # ``AutomationWorkflow`` body resolves its activities by string
    # name (``workflow.execute_activity("jira_add_comment", ...)``,
    # etc.) so the activity callables can land in subsequent tasks
    # without churning this boot script.
    worker = Worker(
        client,
        task_queue=AUTOMATION_TASK_QUEUE,
        workflows=[
            AutomationWorkflow,
            BotBranchRetention,
            AuditPruneWorkflow,
            # platform-completion task 26.3 — new workflows
            MultiStepWorkflow,
            ApprovalGateWorkflow,
            # platform-gap-fill task 22.3 — IterationWorkflow is the
            # Temporal entry point for ``[iterate]``-driven re-runs
            # (R12.1-R12.8).  Hosted on the same automation-tq queue
            # as the gateway so the worker stays single-queue.
            IterationWorkflow,
            # platform-real-usage-gaps task 12.2 — EpicSubtaskWorkflow
            # orchestrates Epic subtasks sequentially, starting a child
            # AutomationWorkflow for each (R12.3, R12.4).
            EpicSubtaskWorkflow,
            # Single-runner canonical contract — G2: hourly workspace
            # disk auto-prune cron. The activities live in the
            # ``execution-runner-worker`` (the only worker with SSH
            # creds); this worker just hosts the workflow body and
            # uses ``execute_activity`` by string name.
            WorkspaceCleanupSchedulerWorkflow,
        ],
        activities=[
            audit_prune_activities.get_retention_setting,
            audit_prune_activities.archive_audit_to_minio,
            audit_prune_activities.delete_audit_older_than,
            audit_prune_activities.notify_audit_prune_failed,
            # platform-completion task 26.3 — new activities. Each
            # carries ``@activity.defn(name=...)`` so the wire name
            # matches the string literal used by the workflow side.
            execute_output_actions,
            resolve_repo_field,
            resolve_jira_status,
            evaluate_branch_rules,
            prepare_task_analysis_input,
            analyze_task,
            jira_add_comment,
            jira_transition_issue,
            load_branch_pattern_rules,
            audit_write,
            noop_test_post_result,
            # platform-gap-fill task 22.3 — prepare_iteration is the
            # first activity called by IterationWorkflow.  Wiring it
            # here keeps the [iterate] flow self-contained on the
            # automation-tq queue (no cross-worker call).
            iteration_manager_activities.prepare_iteration,
            # platform-mimari-ops 8.5 / R5.2 / R5.3 — workflow
            # completion dispatch + dept-config lookup activities.
            # The dispatch activity is best-effort: a Slack/email
            # transport failure never blocks the workflow's terminal
            # path. Idempotency is enforced inside
            # ``NotificationService.notify_workflow_completion`` via
            # the deterministic ``dedup_key`` so a Temporal-driven
            # retry is a safe no-op.
            notification_dispatch_activities.dispatch_notification,
        ],
    )

    _LOG.info(
        "automation-worker ready (queue=%s, schedule=%s)",
        AUTOMATION_TASK_QUEUE,
        AUDIT_PRUNE_CRON_SCHEDULE,
    )

    # Graceful shutdown on SIGTERM / SIGINT.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover — Windows
            pass

    worker_task = asyncio.create_task(worker.run())
    try:
        await stop_event.wait()
    finally:
        _LOG.info("automation-worker stopping…")
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def run() -> None:  # pragma: no cover — thin entrypoint
    """Console-script entrypoint."""

    asyncio.run(_run_async())


if __name__ == "__main__":  # pragma: no cover
    run()
