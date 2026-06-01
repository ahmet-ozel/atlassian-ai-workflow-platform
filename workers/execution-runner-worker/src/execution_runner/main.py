"""``execution-runner-worker`` boot script (canonical location).

Hosts the **single** Temporal worker that polls the
``execution-runner-tq`` task queue (workflows-spec Requirements 1.1,
1.2 — *"a worker SHALL listen on exactly one task queue"*). The
queue name is sourced from
:func:`temporal_shared.workflow_registry.task_queue_for` so the boot
script and the workflow modules share a single source of truth — the
queue string is never duplicated as a string literal anywhere in the
worker package.

The worker registers
:class:`src.workflows.execution_run_workflow.ExecutionRunWorkflow`
plus the SSH / Vault / MinIO / Docker activities exported from
:mod:`src.activities`. Activity modules currently live under the
legacy ``src.*`` namespace; the boot script imports them lazily so
the module remains importable on hosts without every transitive
runtime dependency (used by AST inspection in tests).

Configuration is read from the process environment (no ``.env`` file
loader at this layer — Compose / Kubernetes injects the values). The
keys consumed are documented in
``platform/workers/execution-runner-worker/.env.example``.

Validates Requirements: 1.1, 1.2 — single-queue / canonical-workflow
invariant.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Any

# ``temporalio`` is the only mandatory runtime import. Guarded so
# tests that exercise the boot script via AST inspection / patching
# can still import this module on a host without the SDK.
try:  # pragma: no cover — exercised at boot only
    from temporalio.client import Client
    from temporalio.worker import Worker
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment,misc]
    Worker = None  # type: ignore[assignment,misc]

# Single source of truth for the queue this worker polls. The helper
# raises ``KeyError`` for unknown workflow names, so adding a new
# workflow to this worker also requires extending the registry —
# the boot cannot silently fall through to a default queue (R1.2).
from temporal_shared.workflow_registry import task_queue_for

#: Materialise the queue name at import time so tests can assert it
#: without spinning up the worker. The lookup is pure and
#: side-effect-free so it is safe to run during module import.
EXECUTION_RUNNER_TASK_QUEUE: str = task_queue_for("ExecutionRunWorkflow")

#: Default Temporal cluster address — overridden via ``TEMPORAL_HOST``
#: in the worker's ``.env`` when the cluster runs on a non-default
#: hostname / port.
DEFAULT_TEMPORAL_HOST: str = "temporal:7233"

_LOG = logging.getLogger("execution_runner_worker")


# ---------------------------------------------------------------------------
# Workflow + activity discovery
# ---------------------------------------------------------------------------


def _load_workflow() -> Any | None:
    """Return the canonical :class:`ExecutionRunWorkflow` class.

    Imported lazily so the boot script remains importable on hosts
    without the workflow's transitive dependencies (``asyncpg``,
    ``httpx``). Production deployments do ship them; tests stub the
    worker out via ``Worker = None``.
    """

    try:  # pragma: no cover — exercised at boot only
        from src.workflows.execution_run_workflow import (  # type: ignore[import-not-found]
            ExecutionRunWorkflow,
        )

        return ExecutionRunWorkflow
    except ImportError as exc:  # pragma: no cover
        _LOG.error(
            "execution-runner-worker: ExecutionRunWorkflow import "
            "failed (%s); the worker cannot start.",
            exc,
        )
        return None


def _load_activities() -> list[Any]:
    """Return the list of activity callables to register on the worker.

    Activity modules live under :mod:`src.activities`. Each
    ``@activity.defn``-decorated callable is collected by name so the
    registration list stays explicit (rather than relying on
    ``dir()`` / introspection that would silently pick up helper
    coroutines).
    """

    activities: list[Any] = []
    try:  # pragma: no cover — exercised at boot only
        from src.activities.docker import (  # type: ignore[import-not-found]
            docker_cleanup_container,
            docker_run_container,
            docker_stop_container,
        )
        from src.activities.minio import (  # type: ignore[import-not-found]
            minio_download_artifact,
            minio_upload_artifact,
        )
        from src.activities.ssh import (  # type: ignore[import-not-found]
            ssh_cleanup,
            ssh_connect_and_run,
            ssh_run_test,
        )
        from src.activities.vault import (  # type: ignore[import-not-found]
            vault_fetch_ssh_credentials,
        )

        activities.extend(
            [
                # Vault
                vault_fetch_ssh_credentials,
                # SSH
                ssh_connect_and_run,
                ssh_run_test,
                ssh_cleanup,
                # MinIO
                minio_upload_artifact,
                minio_download_artifact,
                # Docker (P0 stubs — deferred to next spec)
                docker_run_container,
                docker_stop_container,
                docker_cleanup_container,
            ]
        )
    except ImportError as exc:  # pragma: no cover — diagnostic-only
        _LOG.warning(
            "execution-runner-worker: activity module import failed "
            "(%s); the worker will start with the activity list empty. "
            "Production deployments must ship all activity dependencies.",
            exc,
        )
    return activities


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def _run_async() -> None:
    """Connect to Temporal and run the worker until cancelled."""

    if Client is None or Worker is None:  # pragma: no cover
        raise RuntimeError(
            "temporalio is required to run execution-runner-worker"
        )

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    target_host = os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST)
    _LOG.info("Connecting to Temporal at %s", target_host)

    client = await Client.connect(target_host)

    workflow_cls = _load_workflow()
    if workflow_cls is None:  # pragma: no cover
        raise RuntimeError(
            "ExecutionRunWorkflow is not importable; refusing to start "
            "an empty execution-runner-worker."
        )

    # Single-queue contract (R1.2): one task queue per worker. The
    # value is sourced from the shared registry so tests and ops
    # tooling can assert the contract without importing the boot
    # script (they import the registry directly).
    worker = Worker(
        client,
        task_queue=EXECUTION_RUNNER_TASK_QUEUE,
        workflows=[workflow_cls],
        activities=_load_activities(),
    )

    _LOG.info(
        "execution-runner-worker ready (queue=%s)",
        EXECUTION_RUNNER_TASK_QUEUE,
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
        _LOG.info("execution-runner-worker stopping…")
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def run() -> None:
    """Synchronous wrapper used as the console-script entry point."""

    try:
        asyncio.run(_run_async())
    except KeyboardInterrupt:  # pragma: no cover — operator interrupt
        _LOG.info("execution-runner-worker stopped by signal")
    except Exception:  # noqa: BLE001 — top-level guard
        _LOG.exception("execution-runner-worker failed to start")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    run()
