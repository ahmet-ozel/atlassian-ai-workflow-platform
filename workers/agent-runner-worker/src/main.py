"""Asyncio entrypoint for the ``agent-runner-worker`` Temporal worker.

The worker connects to the Temporal cluster pointed at by the
``TEMPORAL_HOST`` environment variable (default ``temporal:7233``) and
registers a ``Worker`` on the ``agent-runner`` task queue.

Workflow and activity registrations are intentionally empty in this
scaffold task; subsequent tasks will populate them. If the connection
to Temporal cannot be established the process exits with a non-zero
status code so that the orchestrating Compose stack / supervisor can
restart it (Requirement 3.7).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from temporalio.client import Client
from temporalio.worker import Worker

TASK_QUEUE: str = "agent-runner"
DEFAULT_TEMPORAL_HOST: str = "temporal:7233"

logger = logging.getLogger(__name__)


async def main() -> None:
    """Connect to Temporal and run the worker until cancelled."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Y4 fix (GEREKSINIM_ANALIZI.md): worker entry points were missing
    # the redaction filter installation that the HTTP services already
    # had — TEST_REPORT critical #2/#3 found token/password leaks in
    # worker logs (which handle SSH credentials, Jira tokens, etc.).
    # Install it immediately after basicConfig so every later
    # ``logger.info`` / structlog record passes through the regex
    # scrubber before reaching stdout.
    from http_shared import install_redaction_filter  # noqa: PLC0415

    install_redaction_filter(
        loggers=[logging.getLogger()], attach_to_root=True
    )

    target_host = os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST)
    task_queue = os.environ.get("TEMPORAL_TASK_QUEUE", TASK_QUEUE)
    logger.info("Connecting to Temporal at %s", target_host)

    client = await Client.connect(target_host)

    # TODO: register workflows from src.workflows and activities from
    # src.activities once the placeholder modules are implemented.
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[],
        activities=[],
    )

    logger.info("agent-runner-worker started on task queue %s", task_queue)
    await worker.run()


def run() -> None:
    """Synchronous wrapper used as the console-script entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - operator interrupt
        logger.info("agent-runner-worker stopped by signal")
    except Exception:  # noqa: BLE001 - top-level guard
        logger.exception("agent-runner-worker failed to start")
        sys.exit(1)


if __name__ == "__main__":
    run()
