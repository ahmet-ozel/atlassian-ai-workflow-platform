"""Temporal client wrapper for the automation-service.

Provides a thin async wrapper around ``temporalio.client.Client`` that
lazily connects to the Temporal cluster on first use. The webhook handlers
and decision engine use this to start workflows, send signals, and query
workflow state.

Connection parameters are read from environment variables:
- ``TEMPORAL_HOST`` - Temporal frontend address (default ``temporal:7233``)
- ``TEMPORAL_NAMESPACE`` - Temporal namespace (default ``default``)

The class re-exports ``WorkflowAlreadyStartedError`` so that callers can
catch duplicate workflow starts without importing from the SDK directly.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from temporalio.client import (
    Client,
    WorkflowHandle,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

__all__ = [
    "TemporalClient",
    "WorkflowAlreadyStartedError",
    "WorkflowNotFoundError",
]

logger = logging.getLogger(__name__)

# Default connection parameters
DEFAULT_TEMPORAL_HOST: str = "temporal:7233"
DEFAULT_TEMPORAL_NAMESPACE: str = "default"


class WorkflowAlreadyStartedError(Exception):
    """Raised when a workflow with the given ID is already running.

    Wraps the underlying Temporal ``RPCError`` with status ALREADY_EXISTS.
    The webhook handler interprets this as a duplicate delivery and returns
    HTTP 200 with status "duplicate".
    """

    def __init__(self, workflow_id: str, workflow_type: str, cause: RPCError | None = None) -> None:
        super().__init__(
            f"Workflow already started: id={workflow_id!r}, type={workflow_type!r}"
        )
        self.workflow_id = workflow_id
        self.workflow_type = workflow_type
        self.__cause__ = cause


class WorkflowNotFoundError(Exception):
    """Raised when a Temporal operation targets a workflow that doesn't exist.

    Wraps the underlying Temporal ``RPCError`` with status ``NOT_FOUND``
    (no execution found for the given workflow ID - neither running nor
    closed). The Jira ``comment_created`` handler distinguishes this
    from generic transport errors so it can decide whether to restart a
    new workflow on the issue.
    """

    def __init__(self, workflow_id: str, cause: RPCError | None = None) -> None:
        super().__init__(f"Workflow not found: id={workflow_id!r}")
        self.workflow_id = workflow_id
        self.__cause__ = cause


class TemporalClient:
    """Async Temporal client wrapper with lazy connection.

    Connects to a real Temporal cluster via ``temporalio.client.Client.connect()``.
    The webhook handlers use this to start workflows, send signals, and query
    workflow state.

    Usage::

        client = TemporalClient()
        await client.connect()

        handle = await client.start_workflow(
            workflow_type="AutomationWorkflow",
            workflow_id="automation-jira-PAY-4211",
            task_queue="automation-tq",
            args=[{"issue_key": "PAY-4211"}],
        )

    The ``connect()`` method must be called once (typically during app
    lifespan startup) before any workflow operations.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        namespace: str | None = None,
    ) -> None:
        self._host = host or os.environ.get("TEMPORAL_HOST", DEFAULT_TEMPORAL_HOST)
        self._namespace = namespace or os.environ.get(
            "TEMPORAL_NAMESPACE", DEFAULT_TEMPORAL_NAMESPACE
        )
        self._client: Client | None = None

    @property
    def is_connected(self) -> bool:
        """Return whether the client has an active connection."""
        return self._client is not None

    async def connect(self) -> None:
        """Establish connection to the Temporal cluster.

        This must be called once during application startup (e.g., in the
        FastAPI lifespan handler). Subsequent calls are no-ops if already
        connected.
        """
        if self._client is not None:
            return

        logger.info(
            "Connecting to Temporal at %s (namespace=%s)",
            self._host,
            self._namespace,
        )
        self._client = await Client.connect(
            self._host,
            namespace=self._namespace,
        )
        logger.info("Temporal connection established")

    def _ensure_connected(self) -> Client:
        """Return the underlying client, raising if not yet connected."""
        if self._client is None:
            raise RuntimeError(
                "TemporalClient is not connected. Call `await client.connect()` first."
            )
        return self._client

    async def start_workflow(
        self,
        workflow_type: str,
        workflow_id: Any | None = None,
        *workflow_args: Any,
        id: str | None = None,
        task_queue: str,
        args: Sequence[Any] | None = None,
        execution_timeout: Any | None = None,
        run_timeout: Any | None = None,
        task_timeout: Any | None = None,
        id_reuse_policy: WorkflowIDReusePolicy = WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy: WorkflowIDConflictPolicy = WorkflowIDConflictPolicy.FAIL,
    ) -> WorkflowHandle[Any, Any]:
        """Start a Temporal workflow.

        Parameters
        ----------
        workflow_type:
            The registered workflow name (e.g., ``"AutomationWorkflow"``).
        workflow_id:
            Unique workflow ID. Temporal rejects duplicates natively when
            ``id_reuse_policy`` is ``REJECT_DUPLICATE`` and
            ``id_conflict_policy`` is ``FAIL`` (the defaults). When the
            SDK-style ``id=`` alias is provided, this positional slot is
            treated as the first workflow argument instead.
        *workflow_args:
            SDK-compatible positional arguments passed to the workflow's
            ``run`` method. This keeps the wrapper structurally compatible
            with ``temporalio.client.Client.start_workflow`` and shared
            helpers that call ``start_workflow(workflow, *args, id=...)``.
        id:
            SDK-compatible alias for ``workflow_id``.
        task_queue:
            The task queue the workflow worker is polling.
        args:
            Positional arguments passed to the workflow's ``run`` method.
            Each element is passed as a separate positional argument.
        execution_timeout:
            Optional maximum total workflow execution time.
        run_timeout:
            Optional maximum single workflow run time.
        task_timeout:
            Optional maximum workflow task (decision) time.
        id_reuse_policy:
            How to handle reuse of completed workflow IDs. Defaults to
            ``REJECT_DUPLICATE`` which prevents restarting a workflow
            with the same ID even after completion.
        id_conflict_policy:
            How to handle conflict with a currently-running workflow ID.
            Defaults to ``FAIL``, which provides native idempotency
            (Req 10.4). Temporal raises an error if a workflow with the
            same ID is already running.

        Returns
        -------
        WorkflowHandle
            A handle to the started workflow.

        Raises
        ------
        WorkflowAlreadyStartedError
            If a workflow with the same ``workflow_id`` is already running.
        """
        client = self._ensure_connected()
        if id is not None:
            resolved_workflow_id = id
            positional_args = (
                (() if workflow_id is None else (workflow_id,)) + workflow_args
            )
        else:
            resolved_workflow_id = workflow_id
            positional_args = workflow_args
        if not resolved_workflow_id:
            raise ValueError("workflow_id or id is required")
        if not isinstance(resolved_workflow_id, str):
            raise TypeError("workflow_id or id must be a string")
        if positional_args and args is not None:
            raise ValueError("use either positional workflow args or args=, not both")
        resolved_args: Sequence[Any] = positional_args if positional_args else (args or ())

        start_kwargs: dict[str, Any] = {
            "task_queue": task_queue,
            "id": resolved_workflow_id,
            "id_reuse_policy": id_reuse_policy,
            "id_conflict_policy": id_conflict_policy,
        }
        if execution_timeout is not None:
            start_kwargs["execution_timeout"] = execution_timeout
        if run_timeout is not None:
            start_kwargs["run_timeout"] = run_timeout
        if task_timeout is not None:
            start_kwargs["task_timeout"] = task_timeout

        try:
            handle = await client.start_workflow(
                workflow_type,
                *resolved_args,
                **start_kwargs,
            )
            logger.info(
                "Started workflow %s (id=%s, queue=%s)",
                workflow_type,
                resolved_workflow_id,
                task_queue,
            )
            return handle
        except RPCError as exc:
            # Temporal raises RPCError with status ALREADY_EXISTS for
            # duplicate workflow IDs. We wrap it in our typed exception
            # so the webhook handler can interpret it as a duplicate.
            if "already started" in str(exc).lower() or "already exists" in str(exc).lower():
                raise WorkflowAlreadyStartedError(
                    workflow_id=resolved_workflow_id,
                    workflow_type=workflow_type,
                    cause=exc,
                ) from exc
            raise

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None:
        """Send a signal to a running workflow.

        Parameters
        ----------
        workflow_id:
            The target workflow's ID.
        signal_name:
            Name of the signal (e.g., ``"new_comment"``).
        payload:
            Optional data payload to include with the signal.

        Raises
        ------
        WorkflowNotFoundError
            If no execution exists for ``workflow_id`` (neither running
            nor closed). The Jira ``comment_created`` handler relies on
            this typed exception to decide whether to restart a new
            workflow on the issue.
        """
        client = self._ensure_connected()
        handle = client.get_workflow_handle(workflow_id)
        try:
            await handle.signal(signal_name, payload)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise WorkflowNotFoundError(
                    workflow_id=workflow_id, cause=exc
                ) from exc
            raise
        logger.debug(
            "Sent signal %s to workflow %s",
            signal_name,
            workflow_id,
        )

    async def signal_with_start(
        self,
        workflow_type: str,
        workflow_id: str,
        *,
        task_queue: str,
        signal_name: str,
        signal_payload: Any = None,
        args: Sequence[Any] = (),
    ) -> WorkflowHandle[Any, Any]:
        """Atomically start a workflow and deliver a signal to it.

        Wraps Temporal's ``start_signal`` parameter on
        :meth:`Client.start_workflow`: if the workflow with
        ``workflow_id`` is already running, the signal is delivered to
        the existing execution; otherwise a new execution is started
        and the signal is buffered for the first task. This is the
        primitive the Jira ``comment_created`` handler uses to restart
        a workflow after Temporal has reported ``WorkflowNotFound``.

        Parameters
        ----------
        workflow_type:
            The registered workflow name (e.g. ``"AutomationWorkflow"``).
        workflow_id:
            The target workflow's ID.
        task_queue:
            The task queue the worker is polling (e.g.
            ``"automation-tq"``).
        signal_name:
            Name of the signal to deliver (e.g. ``"new_comment"``).
        signal_payload:
            Optional payload for the signal handler.
        args:
            Positional arguments passed to the workflow's ``run``
            method when a new execution is started.

        Returns
        -------
        WorkflowHandle
            A handle to the (possibly newly-started) workflow.
        """
        client = self._ensure_connected()
        handle = await client.start_workflow(
            workflow_type,
            *args,
            id=workflow_id,
            task_queue=task_queue,
            start_signal=signal_name,
            start_signal_args=[signal_payload] if signal_payload is not None else [],
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        logger.info(
            "signal_with_start workflow=%s id=%s signal=%s queue=%s",
            workflow_type,
            workflow_id,
            signal_name,
            task_queue,
        )
        return handle

    async def query_workflow(
        self,
        workflow_id: str,
        query_name: str,
    ) -> Any:
        """Query a running workflow's state.

        Parameters
        ----------
        workflow_id:
            The target workflow's ID.
        query_name:
            Name of the query handler (e.g., ``"get_pending_question"``).

        Returns
        -------
        Any
            The query result as returned by the workflow's query handler.
        """
        client = self._ensure_connected()
        handle = client.get_workflow_handle(workflow_id)
        result: Any = await handle.query(query_name)
        logger.debug(
            "Queried workflow %s with %s",
            workflow_id,
            query_name,
        )
        return result

    async def get_workflow_handle(self, workflow_id: str) -> WorkflowHandle[Any, Any]:
        """Get a handle to an existing workflow by ID.

        Useful for advanced operations beyond signal/query.
        """
        client = self._ensure_connected()
        return client.get_workflow_handle(workflow_id)
