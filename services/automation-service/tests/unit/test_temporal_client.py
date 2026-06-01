"""Unit tests for the TemporalClient wrapper.

Tests the TemporalClient class logic without requiring a real Temporal
cluster. Uses mocks for the underlying temporalio.client.Client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the automation-service src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from temporal_client import (
    DEFAULT_TEMPORAL_HOST,
    DEFAULT_TEMPORAL_NAMESPACE,
    TemporalClient,
    WorkflowAlreadyStartedError,
)


class TestTemporalClientInit:
    """Tests for TemporalClient initialization."""

    def test_defaults_from_constants(self) -> None:
        client = TemporalClient()
        assert client._host == DEFAULT_TEMPORAL_HOST
        assert client._namespace == DEFAULT_TEMPORAL_NAMESPACE
        assert client._client is None

    def test_explicit_host_and_namespace(self) -> None:
        client = TemporalClient(host="localhost:7233", namespace="test-ns")
        assert client._host == "localhost:7233"
        assert client._namespace == "test-ns"

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_HOST", "env-host:7233")
        monkeypatch.setenv("TEMPORAL_NAMESPACE", "env-ns")
        client = TemporalClient()
        assert client._host == "env-host:7233"
        assert client._namespace == "env-ns"

    def test_explicit_params_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEMPORAL_HOST", "env-host:7233")
        client = TemporalClient(host="explicit:7233")
        assert client._host == "explicit:7233"

    def test_is_connected_false_initially(self) -> None:
        client = TemporalClient()
        assert client.is_connected is False


class TestTemporalClientConnect:
    """Tests for the connect() method."""

    @pytest.mark.asyncio
    async def test_connect_calls_client_connect(self) -> None:
        mock_client = AsyncMock()
        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_client,
        ) as mock_connect:
            tc = TemporalClient(host="test:7233", namespace="test-ns")
            await tc.connect()

            mock_connect.assert_called_once_with("test:7233", namespace="test-ns")
            assert tc.is_connected is True

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self) -> None:
        mock_client = AsyncMock()
        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_client,
        ) as mock_connect:
            tc = TemporalClient()
            await tc.connect()
            await tc.connect()  # second call should be no-op

            mock_connect.assert_called_once()


class TestTemporalClientStartWorkflow:
    """Tests for the start_workflow() method."""

    @pytest.mark.asyncio
    async def test_start_workflow_success(self) -> None:
        mock_handle = MagicMock()
        mock_underlying = AsyncMock()
        mock_underlying.start_workflow = AsyncMock(return_value=mock_handle)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            handle = await tc.start_workflow(
                "AutomationWorkflow",
                "automation-jira-PAY-4211",
                task_queue="agent-runner",
                args=[{"issue_key": "PAY-4211"}],
            )

            assert handle is mock_handle
            mock_underlying.start_workflow.assert_called_once()
            call_args = mock_underlying.start_workflow.call_args
            assert call_args[0][0] == "AutomationWorkflow"
            assert call_args[0][1] == {"issue_key": "PAY-4211"}
            assert call_args[1]["task_queue"] == "agent-runner"
            assert call_args[1]["id"] == "automation-jira-PAY-4211"

    @pytest.mark.asyncio
    async def test_start_workflow_accepts_sdk_style_id_and_args(self) -> None:
        mock_handle = MagicMock()
        mock_underlying = AsyncMock()
        mock_underlying.start_workflow = AsyncMock(return_value=mock_handle)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            handle = await tc.start_workflow(
                "AutomationWorkflow",
                {"issue_key": "PAY-4211"},
                id="automation-jira-PAY-4211",
                task_queue="agent-runner",
            )

            assert handle is mock_handle
            call_args = mock_underlying.start_workflow.call_args
            assert call_args[0] == (
                "AutomationWorkflow",
                {"issue_key": "PAY-4211"},
            )
            assert call_args[1]["id"] == "automation-jira-PAY-4211"
            assert call_args[1]["task_queue"] == "agent-runner"

    @pytest.mark.asyncio
    async def test_start_workflow_raises_when_not_connected(self) -> None:
        tc = TemporalClient()
        with pytest.raises(RuntimeError, match="not connected"):
            await tc.start_workflow(
                "AutomationWorkflow",
                "automation-jira-PAY-4211",
                task_queue="agent-runner",
            )

    @pytest.mark.asyncio
    async def test_start_workflow_already_started_error(self) -> None:
        from temporalio.service import RPCError

        mock_underlying = AsyncMock()
        rpc_error = RPCError(
            message="Workflow execution already started",
            status=None,
            raw_grpc_status=None,
        )
        mock_underlying.start_workflow = AsyncMock(side_effect=rpc_error)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            with pytest.raises(WorkflowAlreadyStartedError) as exc_info:
                await tc.start_workflow(
                    "AutomationWorkflow",
                    "automation-jira-PAY-4211",
                    task_queue="agent-runner",
                )

            assert exc_info.value.workflow_id == "automation-jira-PAY-4211"
            assert exc_info.value.workflow_type == "AutomationWorkflow"

    @pytest.mark.asyncio
    async def test_start_workflow_propagates_other_rpc_errors(self) -> None:
        from temporalio.service import RPCError

        mock_underlying = AsyncMock()
        rpc_error = RPCError(
            message="Connection refused",
            status=None,
            raw_grpc_status=None,
        )
        mock_underlying.start_workflow = AsyncMock(side_effect=rpc_error)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            with pytest.raises(RPCError, match="Connection refused"):
                await tc.start_workflow(
                    "AutomationWorkflow",
                    "automation-jira-PAY-4211",
                    task_queue="agent-runner",
                )


class TestTemporalClientSignalWorkflow:
    """Tests for the signal_workflow() method."""

    @pytest.mark.asyncio
    async def test_signal_workflow_success(self) -> None:
        mock_handle = AsyncMock()
        mock_underlying = AsyncMock()
        mock_underlying.get_workflow_handle = MagicMock(return_value=mock_handle)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            await tc.signal_workflow(
                "automation-jira-PAY-4211",
                "new_comment",
                {"text": "Please use Python 3.12"},
            )

            mock_underlying.get_workflow_handle.assert_called_once_with(
                "automation-jira-PAY-4211"
            )
            mock_handle.signal.assert_called_once_with(
                "new_comment", {"text": "Please use Python 3.12"}
            )

    @pytest.mark.asyncio
    async def test_signal_workflow_raises_when_not_connected(self) -> None:
        tc = TemporalClient()
        with pytest.raises(RuntimeError, match="not connected"):
            await tc.signal_workflow("wf-id", "signal", None)


class TestTemporalClientQueryWorkflow:
    """Tests for the query_workflow() method."""

    @pytest.mark.asyncio
    async def test_query_workflow_success(self) -> None:
        mock_handle = AsyncMock()
        mock_handle.query = AsyncMock(
            return_value={"question": "Which branch?", "issue_key": "PAY-4211"}
        )
        mock_underlying = AsyncMock()
        mock_underlying.get_workflow_handle = MagicMock(return_value=mock_handle)

        with patch(
            "temporal_client.Client.connect",
            new_callable=AsyncMock,
            return_value=mock_underlying,
        ):
            tc = TemporalClient()
            await tc.connect()

            result = await tc.query_workflow(
                "automation-jira-PAY-4211",
                "get_pending_question",
            )

            assert result == {"question": "Which branch?", "issue_key": "PAY-4211"}
            mock_handle.query.assert_called_once_with("get_pending_question")

    @pytest.mark.asyncio
    async def test_query_workflow_raises_when_not_connected(self) -> None:
        tc = TemporalClient()
        with pytest.raises(RuntimeError, match="not connected"):
            await tc.query_workflow("wf-id", "get_pending_question")


class TestWorkflowAlreadyStartedError:
    """Tests for the WorkflowAlreadyStartedError exception."""

    def test_error_attributes(self) -> None:
        err = WorkflowAlreadyStartedError(
            workflow_id="automation-jira-PAY-4211",
            workflow_type="AutomationWorkflow",
        )
        assert err.workflow_id == "automation-jira-PAY-4211"
        assert err.workflow_type == "AutomationWorkflow"
        assert "automation-jira-PAY-4211" in str(err)
        assert "AutomationWorkflow" in str(err)

    def test_is_exception(self) -> None:
        err = WorkflowAlreadyStartedError(
            workflow_id="wf-1",
            workflow_type="TestWorkflow",
        )
        assert isinstance(err, Exception)
