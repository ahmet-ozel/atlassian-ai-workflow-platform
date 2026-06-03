"""Asyncio entrypoint for the ``execution-runner-worker`` Temporal worker.

The worker connects to the Temporal cluster pointed at by the
``TEMPORAL_HOST`` environment variable (default ``temporal:7233``) and
registers a ``Worker`` on the ``execution-runner`` task queue.

If the connection to Temporal cannot be established the process exits with a
non-zero status code so that the orchestrating Compose stack / supervisor can
restart it.

Boot-time seed:
When ``SSH_HOST`` env is set, the worker seeds a ``runner_id='default'``
row into ``infrastructure.ssh_runners`` and assigns all existing
departments to it. This provides backward compatibility for deployments
migrating from the single-runner model to the multi-runner pool.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from temporal_shared.workflow_registry import task_queue_for

TASK_QUEUE: str = task_queue_for("ExecutionRunWorkflow")
DEFAULT_TEMPORAL_HOST: str = "temporal:7233"

logger = logging.getLogger(__name__)


async def main() -> None:
    """Connect to Temporal and run the worker until cancelled."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Install the redaction filter immediately after basicConfig. The
    # execution-runner-worker
    # handles SSH credentials and Vault tokens — leaks here have the
    # widest blast radius. Idempotent: re-installing on the same root
    # is a no-op (Python ``addFilter`` deduplicates).
    from http_shared import install_redaction_filter  # noqa: PLC0415

    install_redaction_filter(
        loggers=[logging.getLogger()], attach_to_root=True
    )

    target_host = os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST)
    configured_queue = os.environ.get("TEMPORAL_TASK_QUEUE")
    if configured_queue and configured_queue != TASK_QUEUE:
        logger.warning(
            "Ignoring TEMPORAL_TASK_QUEUE=%s; ExecutionRunWorkflow must "
            "poll canonical queue %s",
            configured_queue,
            TASK_QUEUE,
        )
    task_queue = TASK_QUEUE
    logger.info("Connecting to Temporal at %s", target_host)

    client = await Client.connect(target_host)

    # TODO: register workflows from src.workflows and activities from
    # src.activities once the placeholder modules are implemented.
    from src.activities import (
        apply_cleanup_policy,
        check_disk_quota,
        docker_cleanup_container,
        docker_run_container,
        docker_stop_container,
        emit_workspace_disk_warning,
        list_workspace_iter_dirs_oldest_first,
        minio_download_artifact,
        minio_upload_artifact,
        probe_workspace_disk_usage,
        prune_workspace_iter,
        resolve_runner,
        ssh_cleanup,
        ssh_connect_and_run,
        ssh_healthcheck,
        ssh_run_test,
        vault_fetch_ssh_credentials,
    )
    # Docker, credential, and disk-quota activities. The four
    # already-exported docker activities plus
    # check_disk_quota come through ``src.activities`` above; the
    # remaining docker activities (build_image, collect_logs,
    # daemon_healthcheck) and the credential injector pair are
    # imported directly from their submodules so the worker can hand
    # them to Temporal even before they land in ``__init__.__all__``
    # downstream.
    from src.activities.credential_injector import (
        cleanup_git_credentials,
        inject_git_credentials,
    )
    from src.activities.docker import (
        docker_build_image,
        docker_collect_logs,
        docker_daemon_healthcheck,
    )
    from src.workflows.execution_run_workflow import (
        ExecutionRunWorkflow,
        LegacyExecutionRunWorkflow,
    )
    # Periodic SSH healthcheck cron workflow.
    from src.workflows.ssh_healthcheck_cron import SSHHealthcheckCronWorkflow

    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[
            ExecutionRunWorkflow,
            LegacyExecutionRunWorkflow,
            # Periodic SSH healthcheck cron workflow.
            SSHHealthcheckCronWorkflow,
        ],
        activities=[
            vault_fetch_ssh_credentials,
            ssh_connect_and_run,
            ssh_cleanup,
            ssh_run_test,
            ssh_healthcheck,
            apply_cleanup_policy,
            minio_upload_artifact,
            minio_download_artifact,
            docker_run_container,
            docker_stop_container,
            docker_cleanup_container,
            # Docker, credential, and disk-quota activities. The
            # docker_run/stop/cleanup trio above is preserved verbatim
            # from the existing worker; the additions below cover
            # build, log collection, daemon healthcheck, the git
            # credential injector pair, and disk-quota enforcement.
            docker_build_image,
            docker_collect_logs,
            docker_daemon_healthcheck,
            inject_git_credentials,
            cleanup_git_credentials,
            resolve_runner,
            check_disk_quota,
            # Single-runner canonical contract — G2: workspace disk
            # auto-prune activities driven by
            # ``WorkspaceCleanupSchedulerWorkflow`` (hosted in
            # ``automation-worker``). The activities are hosted here
            # because this is the only worker with SSH credentials.
            probe_workspace_disk_usage,
            emit_workspace_disk_warning,
            list_workspace_iter_dirs_oldest_first,
            prune_workspace_iter,
        ],
    )

    # ---- Boot-time seed: default runner from SSH_HOST ----
    # When SSH_HOST env is set, seed a 'default' runner into the
    # infrastructure.ssh_runners table and assign all existing departments
    # to it. This provides backward compatibility for deployments migrating
    # from the single-runner model to the multi-runner pool.
    try:
        import asyncpg  # type: ignore[import-not-found]

        from src.seed_default_runner import seed_default_runner

        postgres_dsn = os.environ.get(
            "POSTGRES_DSN",
            "postgresql://ai:ai_dev_only@postgres:5432/ai",
        )
        pool = await asyncpg.create_pool(dsn=postgres_dsn, min_size=1, max_size=2)
        try:
            await seed_default_runner(pool)
        finally:
            await pool.close()
    except ImportError:
        logger.debug(
            "asyncpg not available — skipping default runner seed "
            "(expected in minimal test environments)"
        )
    except Exception as exc:  # noqa: BLE001
        # Non-fatal: the worker can still start without the seed.
        # Execution capability may be limited until runners are
        # configured via the admin panel.
        logger.warning(
            "Default runner seed failed (non-fatal): %s — "
            "execution capability may be unavailable until runners "
            "are configured via the admin panel.",
            exc,
        )

    logger.info("execution-runner-worker started on task queue %s", task_queue)
    await worker.run()


def run() -> None:
    """Synchronous wrapper used as the console-script entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        logger.info("execution-runner-worker stopped by signal")
    except Exception:  # noqa: BLE001 - top-level guard
        logger.exception("execution-runner-worker failed to start")
        sys.exit(1)


if __name__ == "__main__":
    run()
