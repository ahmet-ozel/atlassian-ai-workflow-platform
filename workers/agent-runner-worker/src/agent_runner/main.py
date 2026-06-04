"""``agent-runner-worker`` boot script (canonical location).

Hosts the **single** Temporal worker that polls the
``agent-runner-tq`` task queue (workflows-spec Requirements 1.1, 1.2 —
*"a worker SHALL listen on exactly one task queue"*). The queue name
is sourced from
:func:`temporal_shared.workflow_registry.task_queue_for` so the boot
script and the workflow modules share a single source of truth — the
queue string is never duplicated as a string literal anywhere in the
worker package.

The worker registers the canonical
:class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
plus the existing Jira / Bitbucket / Confluence / LLM / artifact /
opencode / precommit-scan activities (under the legacy ``src.*``
import path until the activity modules are relocated alongside the
``agent_runner`` package).

Configuration is read from the process environment (no ``.env`` file
loader at this layer — Compose / Kubernetes injects the values). The
keys consumed are documented in
``platform/workers/agent-runner-worker/.env.example``.

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

# ``temporalio`` is the only mandatory runtime import. We guard it so
# tests that exercise the boot script via AST inspection / patching can
# still import this module on a host that does not ship the SDK.
try:  # pragma: no cover — exercised at boot only
    from temporalio.client import Client
    from temporalio.worker import Worker
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment,misc]
    Worker = None  # type: ignore[assignment,misc]

# The single source of truth for the queue this worker polls. The
# helper raises ``KeyError`` for unknown workflow names, so adding a
# new workflow to this worker also requires extending the registry —
# the boot cannot silently fall through to a default queue (R1.2).
from temporal_shared.workflow_registry import task_queue_for

from agent_runner.workflows import AgentRunnerWorkflow

#: Materialise the queue name at import time so tests can assert it
#: without spinning up the worker. The lookup is pure and
#: side-effect-free so it is safe to run during module import.
AGENT_RUNNER_TASK_QUEUE: str = task_queue_for("AgentRunnerWorkflow")

#: Default Temporal cluster address — overridden via ``TEMPORAL_HOST``
#: in the worker's ``.env`` when the cluster runs on a non-default
#: hostname / port.
DEFAULT_TEMPORAL_HOST: str = "temporal:7233"

_LOG = logging.getLogger("agent_runner_worker")


# ---------------------------------------------------------------------------
# Activity discovery
# ---------------------------------------------------------------------------


def _load_activities() -> list[Any]:
    """Return the list of activity callables to register on the worker.

    Activity modules currently live under the legacy ``src.activities``
    namespace; the import is wrapped so the boot script remains
    importable on hosts that do not ship every transitive dependency
    (httpx, jinja2, aiobotocore, ...) — production deployments do
    ship them, tests stub the worker out.

    Each ``@activity.defn``-decorated callable is collected by name
    so the registration list stays explicit (rather than relying on
    ``dir()`` / introspection that would silently pick up helper
    coroutines).
    """

    activities: list[Any] = []
    try:  # pragma: no cover — exercised at boot only
        from src.activities.jira import (  # type: ignore[import-not-found]
            jira_add_comment,
            jira_get_issue,
            jira_transition_issue,
        )
        from src.activities.confluence import (  # type: ignore[import-not-found]
            confluence_create_page,
            confluence_get_page,
            confluence_update_page,
        )
        from src.activities.jira_assign import (  # type: ignore[import-not-found]
            set_assignee_to_bot,
        )
        from src.activities.bitbucket import (  # type: ignore[import-not-found]
            bitbucket_add_pr_comment,
            bitbucket_create_branch,
            bitbucket_create_commit,
            bitbucket_create_pull_request_cloud,
            bitbucket_delete_branch,
            bitbucket_fetch_pr_diff,
            bitbucket_open_pr,
        )
        from src.activities.llm import (  # type: ignore[import-not-found]
            firecrawl_scrape,
            firecrawl_search,
            llm_analyze_task,
            llm_generate_code,
            llm_generate_doc,
            llm_generate_pr_description,
            llm_research,
            llm_review_code,
        )
        from src.activities.artifact import (  # type: ignore[import-not-found]
            artifact_delete,
            artifact_download,
            artifact_upload,
        )
        from src.activities.jira_attachment_pipe import (  # type: ignore[import-not-found]
            upload_artifact_to_jira,
        )
        from src.activities.opencode import (  # type: ignore[import-not-found]
            opencode_generate_code,
        )
        from src.activities.precommit_scan import (  # type: ignore[import-not-found]
            precommit_scanner,
        )
        from src.activities.work_item import (  # type: ignore[import-not-found]
            update_work_item_status,
        )

        activities.extend(
            [
                # Jira
                jira_get_issue,
                jira_add_comment,
                jira_transition_issue,
                set_assignee_to_bot,
                # Confluence
                confluence_get_page,
                confluence_create_page,
                confluence_update_page,
                # Bitbucket
                bitbucket_create_branch,
                bitbucket_create_commit,
                bitbucket_create_pull_request_cloud,
                bitbucket_open_pr,
                bitbucket_delete_branch,
                bitbucket_fetch_pr_diff,
                bitbucket_add_pr_comment,
                # LLM
                llm_analyze_task,
                llm_generate_code,
                llm_generate_pr_description,
                llm_generate_doc,
                llm_review_code,
                llm_research,
                # Firecrawl (web research)
                firecrawl_search,
                firecrawl_scrape,
                # Artifact
                artifact_upload,
                artifact_download,
                artifact_delete,
                # MinIO → Jira binary attachment pipeline
                upload_artifact_to_jira,
                # OpenCode
                opencode_generate_code,
                # Pre-commit / work item
                precommit_scanner,
                update_work_item_status,
            ]
        )
    except ImportError as exc:  # pragma: no cover — diagnostic-only
        _LOG.warning(
            "agent-runner-worker: activity module import failed (%s); "
            "the worker will start with the activity list empty. "
            "Production deployments must ship all activity dependencies.",
            exc,
        )
    return activities


# ---------------------------------------------------------------------------
# Credential resolver wiring
# ---------------------------------------------------------------------------


def _build_credential_resolver() -> Any:
    """Construct the worker-local Atlassian credential resolver.

    The Jira/Bitbucket/Confluence activities route through the
    stateless Atlassian MCP and therefore need a per-request credential
    block. The resolver mirrors automation-worker's Vault-backed
    resolver so AgentRunnerWorkflow can run real code-change tasks
    without falling through to ``Credential resolver not initialized``.
    """

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
                raise ValueError("agent-runner-worker only supports org scope")
            if service not in {"jira", "bitbucket", "confluence"}:
                raise ValueError(
                    "service must be one of jira, bitbucket, confluence"
                )
            path = VaultPath.parse(f"vault:atlassian/{dept_id}/{service}")
            return dict(vault.read(path))

    return _VaultAtlassianCredentialResolver()


def _wire_activity_collaborators() -> None:
    """Install shared activity collaborators before worker registration."""

    from src.activities import set_credential_resolver  # type: ignore[import-not-found]

    set_credential_resolver(_build_credential_resolver())
    _LOG.info("agent-runner-worker: Atlassian credential resolver wired")


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def _run_async() -> None:
    """Connect to Temporal and run the worker until cancelled."""

    if Client is None or Worker is None:  # pragma: no cover
        raise RuntimeError(
            "temporalio is required to run agent-runner-worker"
        )

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    target_host = os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST)
    _LOG.info("Connecting to Temporal at %s", target_host)

    _wire_activity_collaborators()

    client = await Client.connect(target_host)

    # Single-queue contract (R1.2): one task queue per worker. The
    # value is sourced from the shared registry so tests and ops
    # tooling can assert the contract without importing the boot
    # script (they import the registry directly).
    worker = Worker(
        client,
        task_queue=AGENT_RUNNER_TASK_QUEUE,
        workflows=[AgentRunnerWorkflow],
        activities=_load_activities(),
    )

    _LOG.info(
        "agent-runner-worker ready (queue=%s)", AGENT_RUNNER_TASK_QUEUE
    )

    # Graceful shutdown on SIGTERM / SIGINT. The signal handler set
    # is best-effort — Windows does not implement
    # ``loop.add_signal_handler`` so the boot still runs there for
    # tests but a Ctrl-C will fall through to KeyboardInterrupt in
    # ``run()``.
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
        _LOG.info("agent-runner-worker stopping…")
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
        _LOG.info("agent-runner-worker stopped by signal")
    except Exception:  # noqa: BLE001 — top-level guard
        _LOG.exception("agent-runner-worker failed to start")
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    run()
