"""Workflow → Temporal task queue registry.

This module is the **single source of truth** for the workflow-name →
task-queue mapping.

The mapping is wrapped in :class:`types.MappingProxyType` so callers
cannot mutate the shared dictionary at runtime; this protects the
worker-boot contract that a worker listens on exactly one task
queue: if any module could rebind queue names, that invariant
would silently break replay determinism.

Public API
----------
* :data:`WORKFLOW_TASK_QUEUES` — immutable mapping (4 entries).
* :func:`task_queue_for` — pure function returning the queue for a
  registered workflow name; raises :class:`KeyError` for unknown
  names so callers must handle the error path explicitly (no silent
  fall-through to a default queue).
* :class:`SupportsWorkerBoot` — structural protocol that documents
  the signature worker boot scripts use when registering a worker
  with the Temporal SDK; takes a single ``task_queue`` parameter.

Queue ownership
---------------
Three workflow families host on three task queues; each worker listens
on exactly one queue. `BotBranchRetention` cron piggy-backs on the
``automation-tq`` queue so the automation-worker owns retention without
spinning up an extra worker.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Final, Mapping, Protocol

__all__ = [
    "WORKFLOW_TASK_QUEUES",
    "SupportsWorkerBoot",
    "task_queue_for",
]


# ---------------------------------------------------------------------------
# WORKFLOW_TASK_QUEUES — workflow-name → task-queue mapping
# ---------------------------------------------------------------------------

#: Workflow class name → Temporal task queue name. Wrapped in
#: ``MappingProxyType`` so callers cannot mutate the shared dictionary
#: at runtime.
#:
#: Entries
#: -------
#: * ``"AutomationWorkflow"`` → ``"automation-tq"`` — the gateway
#:   workflow that receives ``signalWithStart`` from the webhook
#:   handler and routes events to child workflows after the
#:   capability gate.
#: * ``"AgentRunnerWorkflow"`` → ``"agent-runner-tq"`` — hosts LLM
#:   and MCP activities; the iter loop, ``[fix]``/``[explain]``
#:   cooldown, and PR/Confluence work all run here.
#: * ``"ExecutionRunWorkflow"`` → ``"execution-runner-tq"`` — runs
#:   SSH/Docker test executions; isolated from the LLM workers so
#:   agent-runner LLM bottlenecks cannot block test runs.
#: * ``"BotBranchRetention"`` → ``"automation-tq"`` — daily cron
#:   workflow that piggy-backs on the automation worker; defining
#:   it in the same registry keeps the boot script declarative.
WORKFLOW_TASK_QUEUES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "AutomationWorkflow": "automation-tq",
        "AgentRunnerWorkflow": "agent-runner-tq",
        "ExecutionRunWorkflow": "execution-runner-tq",
        # Cron workflow piggy-backs on the automation-tq queue so the
        # automation-worker hosts both the gateway and the retention job
        # without requiring a fourth worker process.
        "BotBranchRetention": "automation-tq",
    }
)


# ---------------------------------------------------------------------------
# SupportsWorkerBoot — structural protocol for worker boot scripts
# ---------------------------------------------------------------------------


class SupportsWorkerBoot(Protocol):
    """Structural type for a Temporal-SDK worker constructor / factory.

    Worker boot scripts (``platform/workers/*/src/.../main.py``) call a
    constructor with the shape::

        Worker(
            client,
            task_queue=task_queue_for("AutomationWorkflow"),
            workflows=[...],
            activities=[...],
        )

    The protocol documents that a boot helper accepts **exactly one**
    ``task_queue`` parameter; a worker may not listen on multiple
    queues. Tests can substitute any object with the same shape; in
    production the real type is :class:`temporalio.worker.Worker`.
    """

    def __init__(
        self,
        client: Any,
        *,
        task_queue: str,
        workflows: list[Any],
        activities: list[Any],
        **kwargs: Any,
    ) -> None: ...


# ---------------------------------------------------------------------------
# task_queue_for — pure lookup helper
# ---------------------------------------------------------------------------


def task_queue_for(workflow_name: str) -> str:
    """Return the Temporal task queue for a registered workflow.

    Pure lookup against :data:`WORKFLOW_TASK_QUEUES`. Used by every
    worker boot script and by the workflow_type router so the queue
    name is computed from a single source of truth instead of being
    duplicated as a string literal at every call site.

    Parameters
    ----------
    workflow_name:
        Class name of the workflow (e.g. ``"AutomationWorkflow"``).
        Case-sensitive — matches the Temporal-registered workflow
        type name.

    Returns
    -------
    str
        The task queue name (e.g. ``"automation-tq"``).

    Raises
    ------
    KeyError
        If *workflow_name* is not a registered workflow. Raising
        :class:`KeyError` (rather than returning a default) forces
        callers to extend the registry whenever a new workflow is
        introduced; silent fall-through to a default queue would
        violate the single-queue-per-worker contract.

    Examples
    --------
    >>> task_queue_for("AutomationWorkflow")
    'automation-tq'
    >>> task_queue_for("AgentRunnerWorkflow")
    'agent-runner-tq'
    >>> task_queue_for("ExecutionRunWorkflow")
    'execution-runner-tq'
    >>> task_queue_for("BotBranchRetention")
    'automation-tq'
    >>> task_queue_for("UnknownWorkflow")
    Traceback (most recent call last):
        ...
    KeyError: 'UnknownWorkflow'
    """
    return WORKFLOW_TASK_QUEUES[workflow_name]
