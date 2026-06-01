"""Temporal-backed workflow control adapter for the admin dashboard."""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Any

from temporalio.client import Client, WorkflowExecution, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode

from .routers.workflow_control import (
    RestartedWorkflow,
    WorkflowControlError,
    WorkflowDescription,
    WorkflowNotFoundError,
    WorkflowPage,
    WorkflowSummary,
)


class TemporalWorkflowControl:
    """Production implementation of the workflow-control router protocol."""

    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    async def connect(cls, target_host: str) -> "TemporalWorkflowControl":
        return cls(await Client.connect(target_host))

    async def get_workflow_description(
        self, workflow_id: str
    ) -> WorkflowDescription:
        try:
            description = await self._client.get_workflow_handle(
                workflow_id
            ).describe()
        except Exception as exc:  # noqa: BLE001
            self._raise_control_error(exc, workflow_id)

        return WorkflowDescription(
            workflow_id=description.id,
            workflow_type=description.workflow_type,
            task_queue=description.task_queue,
            status=_status_name(description.status),
            dept_id=_dept_id(description.search_attributes),
            started_at=description.start_time,
            closed_at=description.close_time,
        )

    async def cancel_workflow(self, workflow_id: str) -> None:
        try:
            await self._client.get_workflow_handle(workflow_id).cancel()
        except Exception as exc:  # noqa: BLE001
            self._raise_control_error(exc, workflow_id)

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None:
        try:
            await self._client.get_workflow_handle(workflow_id).signal(
                signal_name, payload
            )
        except Exception as exc:  # noqa: BLE001
            self._raise_control_error(exc, workflow_id)

    async def restart_workflow(self, workflow_id: str) -> RestartedWorkflow:
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            history = await handle.fetch_history()
            started = history.events[0].workflow_execution_started_event_attributes
            args = await self._client.data_converter.decode(
                started.input.payloads
            )
            workflow_type = started.workflow_type.name
            task_queue = started.task_queue.name
            new_workflow_id = f"{workflow_id}-retry-{uuid.uuid4().hex[:8]}"
            new_handle = await self._client.start_workflow(
                workflow_type,
                args=args,
                id=new_workflow_id,
                task_queue=task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except Exception as exc:  # noqa: BLE001
            self._raise_control_error(exc, workflow_id)

        return RestartedWorkflow(
            new_workflow_id=new_workflow_id,
            workflow_type=workflow_type,
            run_id=getattr(new_handle, "run_id", None),
        )

    async def list_workflows(
        self,
        *,
        dept_id: str | None,
        wf_status: str | None,
        page: int,
        page_size: int,
        page_token: str | None,
    ) -> WorkflowPage:
        next_page_token = _decode_page_token(page_token)
        fetch_limit = max(page * page_size, page_size)
        iterator = self._client.list_workflows(
            limit=fetch_limit,
            page_size=page_size,
            next_page_token=next_page_token,
        )

        rows: list[WorkflowSummary] = []
        try:
            async for item in iterator:
                summary = _summary(item)
                if not _matches_filter(summary, dept_id, wf_status):
                    continue
                rows.append(summary)
                if len(rows) >= fetch_limit:
                    break
        except Exception as exc:  # noqa: BLE001
            self._raise_control_error(exc, None)

        start = (page - 1) * page_size if page_token is None else 0
        stop = start + page_size
        return WorkflowPage(
            items=rows[start:stop],
            page=page,
            page_size=page_size,
            next_page_token=_encode_page_token(iterator.next_page_token),
        )

    @staticmethod
    def _raise_control_error(exc: Exception, workflow_id: str | None) -> None:
        if isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND:
            raise WorkflowNotFoundError(workflow_id or "<unknown>") from exc
        raise WorkflowControlError(str(exc)) from exc


def _summary(item: WorkflowExecution) -> WorkflowSummary:
    return WorkflowSummary(
        workflow_id=item.id,
        workflow_type=item.workflow_type,
        status=_status_name(item.status),
        dept_id=_dept_id(item.search_attributes),
        started_at=item.start_time,
        closed_at=item.close_time,
    )


def _status_name(status: WorkflowExecutionStatus | Any) -> str:
    name = getattr(status, "name", None)
    return str(name or status).lower()


def _dept_id(search_attributes: dict[str, Any] | None) -> str | None:
    if not search_attributes:
        return None
    for key in ("DepartmentId", "DeptId", "department_id", "dept_id"):
        value = search_attributes.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return str(value)
    return None


def _matches_filter(
    summary: WorkflowSummary,
    dept_id: str | None,
    wf_status: str | None,
) -> bool:
    if wf_status and summary.status != wf_status.lower():
        return False
    # Existing automation workflows do not all carry a department search
    # attribute yet; keep them visible to the operator instead of hiding
    # the exact workflow the webhook just started.
    if dept_id and summary.dept_id not in {None, dept_id}:
        return False
    return True


def _decode_page_token(page_token: str | None) -> bytes | None:
    if not page_token:
        return None
    try:
        return base64.urlsafe_b64decode(page_token.encode("ascii"))
    except Exception:
        return None


def _encode_page_token(page_token: bytes | None) -> str | None:
    if not page_token:
        return None
    return base64.urlsafe_b64encode(page_token).decode("ascii")
