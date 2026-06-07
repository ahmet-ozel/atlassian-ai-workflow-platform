"""Idempotent Temporal workflow start helper.

Provides :func:`start_workflow_idempotent`, a thin wrapper around
:meth:`temporalio.client.Client.start_workflow` that absorbs the native
``WorkflowAlreadyStartedError`` and returns a tuple
``(execution_id, was_existing)`` so that callers (HTTP handlers,
webhook adapters, admin endpoints) can implement the contract:

* New start   HTTP 202, ``was_existing=False``.
* Duplicate   HTTP 202, ``was_existing=True`` with the *same*
  ``execution_id`` (which equals the caller-supplied ``workflow_id``).

This helper is the single source of truth for the idempotency rule
for duplicate workflow starts.  It does **not** perform any
signal-with-start or other re-trigger logic - that decision belongs in
the caller.

Notes
-----
The helper accepts a *structurally-typed* client.  In production this
is :class:`temporalio.client.Client` and the call goes over gRPC; in
tests callers can pass any object exposing an awaitable
``start_workflow(workflow_type, *args, **kwargs)`` method.  This keeps
the helper trivially mockable without forcing test code to import the
heavy ``temporalio`` runtime.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol, Sequence

from temporalio.exceptions import WorkflowAlreadyStartedError

__all__ = [
    "StartResult",
    "SupportsStartWorkflow",
    "start_workflow_idempotent",
]


class SupportsStartWorkflow(Protocol):
    """Structural type for any object that can start a Temporal workflow.

    The real implementation is :class:`temporalio.client.Client`; tests
    may substitute any object with the same ``start_workflow`` shape.
    """

    async def start_workflow(  # noqa: D401 - protocol method
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> Any: ...


class StartResult(NamedTuple):
    """Result of an idempotent workflow start.

    Attributes
    ----------
    execution_id:
        The Temporal ``workflow_id`` of the resulting execution.  This
        is **always** the ``workflow_id`` argument that was passed in:
        on a successful new start it is the id Temporal assigned, and
        on a duplicate it is the id of the already-running workflow
        (which by Temporal's contract equals the caller-supplied id).
    was_existing:
        ``True`` if a workflow with the same ``workflow_id`` was
        already running and Temporal raised
        :class:`temporalio.exceptions.WorkflowAlreadyStartedError`;
        ``False`` if a fresh workflow was started by this call.
    """

    execution_id: str
    was_existing: bool


async def start_workflow_idempotent(
    client: SupportsStartWorkflow,
    workflow_type: str,
    workflow_id: str,
    args: Sequence[Any],
    *,
    task_queue: str,
    **start_kwargs: Any,
) -> StartResult:
    """Start a Temporal workflow, treating duplicates as success.

    Calls ``client.start_workflow(workflow_type, *args, id=workflow_id,
    task_queue=task_queue, **start_kwargs)``.  If Temporal raises
    :class:`temporalio.exceptions.WorkflowAlreadyStartedError` (the
    second-start case for the same ``workflow_id``), the exception is
    swallowed and a :class:`StartResult` with ``was_existing=True`` is
    returned instead.

    Parameters
    ----------
    client:
        Anything implementing :class:`SupportsStartWorkflow` - in
        production a connected :class:`temporalio.client.Client`.
    workflow_type:
        Registered workflow name, e.g. ``"AutomationWorkflow"``.
    workflow_id:
        Idempotency key.  See design.md §"Workflow ID ve Idempotency
        Şeması" for the canonical formats produced by
        :mod:`temporal_shared.identifiers`.
    args:
        Sequence of positional arguments forwarded to the workflow's
        ``run`` method.  Each element is splatted as a separate
        positional argument (matching the Temporal SDK contract).
    task_queue:
        The task queue the worker is polling - required by Temporal.
    **start_kwargs:
        Any additional keyword arguments accepted by
        :meth:`temporalio.client.Client.start_workflow` (e.g.
        ``execution_timeout``, ``id_reuse_policy``,
        ``id_conflict_policy``, ``retry_policy``).

    Returns
    -------
    StartResult
        ``StartResult(execution_id=workflow_id, was_existing=False)``
        on a successful new start;
        ``StartResult(execution_id=workflow_id, was_existing=True)``
        if a workflow with the same id was already running.

    Raises
    ------
    Any exception other than
    :class:`temporalio.exceptions.WorkflowAlreadyStartedError` raised
    by the underlying client (e.g. RPC connection errors) is
    propagated unchanged - duplicate detection is the only special
    case this helper handles.

    Examples
    --------
    >>> # doctest: +SKIP
    >>> from temporalio.client import Client
    >>> client = await Client.connect("temporal:7233")
    >>> result = await start_workflow_idempotent(
    ...     client,
    ...     "AutomationWorkflow",
    ...     "automation-jira-PAY-4211",
    ...     [{"issue_key": "PAY-4211"}],
    ...     task_queue="automation-tq",
    ... )
    >>> if result.was_existing:
    ...     ...  # respond HTTP 202 with the existing execution_id
    """
    try:
        await client.start_workflow(
            workflow_type,
            *args,
            id=workflow_id,
            task_queue=task_queue,
            **start_kwargs,
        )
    except WorkflowAlreadyStartedError:
        # Temporal guarantees the existing workflow has the same id we
        # supplied; surface the caller-supplied workflow_id rather than
        # the exception's workflow_id so the contract is provable from
        # the inputs to this function alone.
        return StartResult(execution_id=workflow_id, was_existing=True)
    except Exception as exc:
        # Some service-level wrappers translate the SDK duplicate error
        # into their own WorkflowAlreadyStartedError class. Keep the
        # shared idempotency contract structural so callers do not need
        # to leak SDK exceptions through every adapter layer.
        if exc.__class__.__name__ == "WorkflowAlreadyStartedError" and hasattr(
            exc, "workflow_id"
        ):
            return StartResult(execution_id=workflow_id, was_existing=True)
        raise
    return StartResult(execution_id=workflow_id, was_existing=False)
