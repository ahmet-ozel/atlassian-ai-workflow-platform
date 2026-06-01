"""Unit tests for ``temporal_shared.start_helper``.

Validates the idempotency contract documented in design.md
§"WorkflowAlreadyStarted" and Requirement 1.6:

  WHEN aynı `workflow_id` ile bir Temporal workflow zaten
  çalışıyorken ikinci bir start denemesi gelirse, THE
  Automation_Service SHALL Temporal'ın `WorkflowAlreadyStarted`
  hatasını yakalar, mevcut workflow'a sinyal/yönlendirme yapar ve
  HTTP 202 ile çağırana mevcut workflow'un id'sini döner.

The helper itself is the *catch* + *return existing id* mechanism;
the signal-redirect is the caller's responsibility (task 5.2).

Tests use a hand-rolled async fake instead of ``unittest.mock`` for
two reasons:

* The helper accepts a structurally-typed client (``SupportsStartWorkflow``);
  a fake records what the helper actually forwards.
* It avoids importing the heavy ``temporalio.client.Client`` at test
  time — only ``WorkflowAlreadyStartedError`` from
  ``temporalio.exceptions`` is needed.

Validates: Requirement 1.6.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from temporal_shared.start_helper import (
    StartResult,
    start_workflow_idempotent,
)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _RecordingClient:
    """A minimal :class:`SupportsStartWorkflow` implementation.

    Captures every ``start_workflow`` invocation and either returns
    a sentinel handle (success path) or raises a configured exception
    (failure path).
    """

    def __init__(
        self,
        *,
        raise_exc: BaseException | None = None,
        handle: Any = "fake-handle",
    ) -> None:
        self._raise_exc = raise_exc
        self._handle = handle
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._handle


# ---------------------------------------------------------------------------
# Happy path — fresh start
# ---------------------------------------------------------------------------


class TestFreshStart:
    """When no workflow with the given id exists, the helper starts it."""

    @pytest.mark.asyncio
    async def test_returns_was_existing_false(self) -> None:
        """**Validates: Requirement 1.6**"""
        client = _RecordingClient()

        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "automation-jira-PAY-4211",
            [{"issue_key": "PAY-4211"}],
            task_queue="automation-tq",
        )

        assert result == StartResult(
            execution_id="automation-jira-PAY-4211", was_existing=False
        )

    @pytest.mark.asyncio
    async def test_forwards_workflow_type_and_args_positionally(self) -> None:
        """**Validates: Requirement 1.6**

        The Temporal SDK splats positional args; the helper must too.
        """
        client = _RecordingClient()
        payload_a = {"issue_key": "PAY-4211"}
        payload_b = {"trace_id": "abc"}

        await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "automation-jira-PAY-4211",
            [payload_a, payload_b],
            task_queue="automation-tq",
        )

        assert len(client.calls) == 1
        positional, kwargs = client.calls[0]
        # workflow_type then *args
        assert positional == ("AutomationWorkflow", payload_a, payload_b)
        assert kwargs["id"] == "automation-jira-PAY-4211"
        assert kwargs["task_queue"] == "automation-tq"

    @pytest.mark.asyncio
    async def test_forwards_extra_start_kwargs(self) -> None:
        """**Validates: Requirement 1.6**

        Optional Temporal SDK kwargs (timeouts, reuse policy, retry
        policy) must reach the underlying client unchanged.
        """
        client = _RecordingClient()
        sentinel_retry = object()

        await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "wf-1",
            [],
            task_queue="automation-tq",
            execution_timeout="2h",
            retry_policy=sentinel_retry,
        )

        _, kwargs = client.calls[0]
        assert kwargs["execution_timeout"] == "2h"
        assert kwargs["retry_policy"] is sentinel_retry

    @pytest.mark.asyncio
    async def test_empty_args_sequence_yields_no_positional_payload(self) -> None:
        """**Validates: Requirement 1.6**"""
        client = _RecordingClient()

        await start_workflow_idempotent(
            client,
            "NoopWorkflow",
            "wf-noop",
            [],
            task_queue="noop-tq",
        )

        positional, _ = client.calls[0]
        # Only the workflow_type — no payload was supplied.
        assert positional == ("NoopWorkflow",)


# ---------------------------------------------------------------------------
# Idempotency — duplicate start
# ---------------------------------------------------------------------------


class TestDuplicateStart:
    """Second call with the same workflow_id must be a no-op success."""

    @pytest.mark.asyncio
    async def test_catches_workflow_already_started_error(self) -> None:
        """**Validates: Requirement 1.6**"""
        client = _RecordingClient(
            raise_exc=WorkflowAlreadyStartedError(
                workflow_id="automation-jira-PAY-4211",
                workflow_type="AutomationWorkflow",
                run_id="abc-123-existing-run",
            )
        )

        # Must NOT propagate the exception.
        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "automation-jira-PAY-4211",
            [{"issue_key": "PAY-4211"}],
            task_queue="automation-tq",
        )

        assert result.was_existing is True
        assert result.execution_id == "automation-jira-PAY-4211"

    @pytest.mark.asyncio
    async def test_returns_caller_supplied_workflow_id(self) -> None:
        """**Validates: Requirement 1.6**

        The helper must echo the caller's ``workflow_id`` rather than
        whatever the SDK exception happens to carry — this keeps the
        contract provable from the function inputs alone.
        """
        client = _RecordingClient(
            raise_exc=WorkflowAlreadyStartedError(
                workflow_id="some-other-id-from-sdk",
                workflow_type="AutomationWorkflow",
            )
        )

        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "automation-jira-PAY-4211",
            [],
            task_queue="automation-tq",
        )

        assert result.execution_id == "automation-jira-PAY-4211"

    @pytest.mark.asyncio
    async def test_two_consecutive_starts_with_same_id_yield_one_was_existing(
        self,
    ) -> None:
        """**Validates: Requirement 1.6**

        Realistic idempotency scenario: first call starts, second call
        sees ``WorkflowAlreadyStartedError`` and returns existing.
        """
        first_client = _RecordingClient()
        second_client = _RecordingClient(
            raise_exc=WorkflowAlreadyStartedError(
                workflow_id="wf-1",
                workflow_type="AutomationWorkflow",
            )
        )

        first = await start_workflow_idempotent(
            first_client,
            "AutomationWorkflow",
            "wf-1",
            [],
            task_queue="automation-tq",
        )
        second = await start_workflow_idempotent(
            second_client,
            "AutomationWorkflow",
            "wf-1",
            [],
            task_queue="automation-tq",
        )

        assert first.was_existing is False
        assert second.was_existing is True
        assert first.execution_id == second.execution_id == "wf-1"


# ---------------------------------------------------------------------------
# Other failures must propagate
# ---------------------------------------------------------------------------


class TestOtherErrorsPropagate:
    """The helper must not swallow anything except the duplicate case."""

    @pytest.mark.asyncio
    async def test_runtime_error_propagates(self) -> None:
        """**Validates: Requirement 1.6**"""
        client = _RecordingClient(raise_exc=RuntimeError("connection refused"))

        with pytest.raises(RuntimeError, match="connection refused"):
            await start_workflow_idempotent(
                client,
                "AutomationWorkflow",
                "wf-1",
                [],
                task_queue="automation-tq",
            )

    @pytest.mark.asyncio
    async def test_value_error_propagates(self) -> None:
        """**Validates: Requirement 1.6**"""
        client = _RecordingClient(raise_exc=ValueError("bad payload"))

        with pytest.raises(ValueError, match="bad payload"):
            await start_workflow_idempotent(
                client,
                "AutomationWorkflow",
                "wf-1",
                [],
                task_queue="automation-tq",
            )

    @pytest.mark.asyncio
    async def test_subclass_of_workflow_already_started_is_caught(self) -> None:
        """**Validates: Requirement 1.6**

        Defensive: any subclass of the SDK exception is also a duplicate.
        """

        class _CustomDup(WorkflowAlreadyStartedError):
            pass

        client = _RecordingClient(
            raise_exc=_CustomDup(
                workflow_id="wf-1",
                workflow_type="AutomationWorkflow",
            )
        )

        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "wf-1",
            [],
            task_queue="automation-tq",
        )
        assert result.was_existing is True

    @pytest.mark.asyncio
    async def test_service_level_workflow_already_started_is_caught(self) -> None:
        """**Validates: Requirement 1.6**

        Production service wrappers may translate the SDK duplicate
        into a local error class with the same semantic name.
        """

        class WorkflowAlreadyStartedError(Exception):
            workflow_id = "wf-1"

        client = _RecordingClient(raise_exc=WorkflowAlreadyStartedError())

        result = await start_workflow_idempotent(
            client,
            "AutomationWorkflow",
            "wf-1",
            [],
            task_queue="automation-tq",
        )
        assert result.was_existing is True


# ---------------------------------------------------------------------------
# StartResult contract
# ---------------------------------------------------------------------------


class TestStartResult:
    """``StartResult`` is a simple ``NamedTuple`` for ergonomic unpacking."""

    def test_is_unpackable_as_tuple(self) -> None:
        result = StartResult(execution_id="wf-1", was_existing=False)
        execution_id, was_existing = result
        assert execution_id == "wf-1"
        assert was_existing is False

    def test_field_access_by_name(self) -> None:
        result = StartResult(execution_id="wf-1", was_existing=True)
        assert result.execution_id == "wf-1"
        assert result.was_existing is True

    def test_equality(self) -> None:
        a = StartResult(execution_id="wf-1", was_existing=False)
        b = StartResult(execution_id="wf-1", was_existing=False)
        c = StartResult(execution_id="wf-1", was_existing=True)
        assert a == b
        assert a != c


# ---------------------------------------------------------------------------
# Mock-based smoke test (verifies AsyncMock-style clients also work)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_works_with_async_mock_client() -> None:
    """**Validates: Requirement 1.6**

    Exercises the helper against the canonical ``unittest.mock``
    pattern callers will use in higher-level tests, ensuring the
    structural protocol is satisfied by ``AsyncMock``.
    """
    client = AsyncMock()
    client.start_workflow = AsyncMock(return_value="handle")

    result = await start_workflow_idempotent(
        client,
        "AutomationWorkflow",
        "automation-jira-PAY-4211",
        [{"issue_key": "PAY-4211"}],
        task_queue="automation-tq",
    )

    assert result == StartResult(
        execution_id="automation-jira-PAY-4211", was_existing=False
    )
    client.start_workflow.assert_awaited_once()
    call = client.start_workflow.await_args
    assert call.args == ("AutomationWorkflow", {"issue_key": "PAY-4211"})
    assert call.kwargs["id"] == "automation-jira-PAY-4211"
    assert call.kwargs["task_queue"] == "automation-tq"
