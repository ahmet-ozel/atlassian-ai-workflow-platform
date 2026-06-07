"""Unit tests for the SSH activity module.

Tests ssh_connect_and_run and ssh_cleanup activities including:
- Successful command execution via mocked paramiko
- Authentication failure raises SSHActivityError
- Connection timeout raises SSHActivityError
- ssh_cleanup swallows exceptions (best-effort)
- RunResult dataclass correctness
- Workspace path handling (cd prefix)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_WORKER_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _WORKER_ROOT / "src"
for _candidate in (_WORKER_ROOT, _SRC_DIR):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from src.activities.ssh import (
    RunResult,
    SSHActivityError,
    _build_full_command,
    _ssh_cleanup_workspace,
    _ssh_execute_command,
    ssh_cleanup,
    ssh_connect_and_run,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_VALID_CRED: dict[str, str | int] = {
    "host": "runner.internal",
    "port": 22,
    "user": "ai-runner",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
}


def test_build_full_command_creates_workdir_before_cd() -> None:
    command = _build_full_command(
        "pytest -q",
        workdir="/var/ai-runner/PAY-1/iter-0",
        env_prefix="",
    )

    assert command.startswith(
        "mkdir -p /var/ai-runner/PAY-1/iter-0 && "
        "cd /var/ai-runner/PAY-1/iter-0 && "
    )
    assert command.endswith("pytest -q")


def _mock_paramiko_success(
    stdout_data: str = "output",
    stderr_data: str = "",
    exit_code: int = 0,
) -> MagicMock:
    """Create a mock paramiko module that simulates successful execution."""
    mock_paramiko = MagicMock()

    # Mock key parsing
    mock_key = MagicMock()
    mock_paramiko.RSAKey.from_private_key.return_value = mock_key

    # Mock client
    mock_client_instance = MagicMock()
    mock_paramiko.SSHClient.return_value = mock_client_instance
    mock_paramiko.AutoAddPolicy.return_value = MagicMock()

    # Mock exec_command
    mock_stdin = MagicMock()
    mock_stdout = MagicMock()
    mock_stderr = MagicMock()
    mock_stdout.read.return_value = stdout_data.encode("utf-8")
    mock_stderr.read.return_value = stderr_data.encode("utf-8")
    mock_stdout.channel.recv_exit_status.return_value = exit_code
    mock_client_instance.exec_command.return_value = (
        mock_stdin,
        mock_stdout,
        mock_stderr,
    )

    return mock_paramiko


def _mock_paramiko_auth_failure() -> MagicMock:
    """Create a mock paramiko module that raises AuthenticationException."""
    mock_paramiko = MagicMock()

    mock_key = MagicMock()
    mock_paramiko.RSAKey.from_private_key.return_value = mock_key

    mock_client_instance = MagicMock()
    mock_paramiko.SSHClient.return_value = mock_client_instance
    mock_paramiko.AutoAddPolicy.return_value = MagicMock()

    # Import real exception class for isinstance checks
    import paramiko as real_paramiko

    mock_paramiko.AuthenticationException = real_paramiko.AuthenticationException
    mock_paramiko.SSHException = real_paramiko.SSHException
    mock_client_instance.connect.side_effect = real_paramiko.AuthenticationException(
        "Invalid key"
    )

    return mock_paramiko


# ---------------------------------------------------------------------------
# Tests: RunResult dataclass
# ---------------------------------------------------------------------------


class TestRunResult:
    def test_frozen_dataclass(self) -> None:
        result = RunResult(stdout="hello", stderr="", exit_code=0)
        assert result.stdout == "hello"
        assert result.stderr == ""
        assert result.exit_code == 0

    def test_immutable(self) -> None:
        result = RunResult(stdout="x", stderr="y", exit_code=1)
        with pytest.raises(AttributeError):
            result.exit_code = 0  # type: ignore[misc]

    def test_non_zero_exit_code(self) -> None:
        result = RunResult(stdout="", stderr="error", exit_code=127)
        assert result.exit_code == 127
        assert result.stderr == "error"


# ---------------------------------------------------------------------------
# Tests: SSHActivityError
# ---------------------------------------------------------------------------


class TestSSHActivityError:
    def test_inherits_runtime_error(self) -> None:
        err = SSHActivityError("host.example.com", "connection refused")
        assert isinstance(err, RuntimeError)

    def test_attributes(self) -> None:
        err = SSHActivityError("10.0.0.1", "auth failed")
        assert err.host == "10.0.0.1"
        assert err.cause == "auth failed"

    def test_str_contains_host_and_cause(self) -> None:
        err = SSHActivityError("runner.internal", "timeout")
        msg = str(err)
        assert "runner.internal" in msg
        assert "timeout" in msg


# ---------------------------------------------------------------------------
# Tests: _ssh_execute_command (blocking helper)
# ---------------------------------------------------------------------------


class TestSSHExecuteCommand:
    def test_success_with_workspace_path(self) -> None:
        mock_paramiko = _mock_paramiko_success(
            stdout_data="test passed", stderr_data="", exit_code=0
        )

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            result = _ssh_execute_command(
                host="runner.internal",
                port=22,
                user="ai-runner",
                private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                command="pytest tests/",
                workspace_path="/tmp/workspace/PAY-4211",
                timeout_seconds=1800,
            )

        assert result.stdout == "test passed"
        assert result.stderr == ""
        assert result.exit_code == 0

        # Verify command was wrapped with cd
        mock_client = mock_paramiko.SSHClient.return_value
        mock_client.exec_command.assert_called_once_with(
            "cd /tmp/workspace/PAY-4211 && pytest tests/",
            timeout=1800.0,
        )

    def test_success_without_workspace_path(self) -> None:
        mock_paramiko = _mock_paramiko_success(
            stdout_data="ok", stderr_data="", exit_code=0
        )

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            result = _ssh_execute_command(
                host="runner.internal",
                port=22,
                user="ai-runner",
                private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                command="echo hello",
                workspace_path="",
                timeout_seconds=60,
            )

        assert result.stdout == "ok"
        assert result.exit_code == 0

        # Verify command was NOT wrapped with cd
        mock_client = mock_paramiko.SSHClient.return_value
        mock_client.exec_command.assert_called_once_with(
            "echo hello",
            timeout=60.0,
        )

    def test_non_zero_exit_code(self) -> None:
        mock_paramiko = _mock_paramiko_success(
            stdout_data="", stderr_data="FAILED", exit_code=1
        )

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            result = _ssh_execute_command(
                host="runner.internal",
                port=22,
                user="ai-runner",
                private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                command="pytest",
                workspace_path="/workspace",
                timeout_seconds=1800,
            )

        # Non-zero exit code is NOT an error - it's a valid result
        assert result.exit_code == 1
        assert result.stderr == "FAILED"

    def test_auth_failure_raises(self) -> None:
        mock_paramiko = _mock_paramiko_auth_failure()

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            with pytest.raises(SSHActivityError) as exc_info:
                _ssh_execute_command(
                    host="runner.internal",
                    port=22,
                    user="ai-runner",
                    private_key="bad-key",
                    command="echo hello",
                    workspace_path="",
                    timeout_seconds=60,
                )

        assert "authentication failed" in exc_info.value.cause
        assert exc_info.value.host == "runner.internal"

    def test_connection_timeout_raises(self) -> None:
        mock_paramiko = MagicMock()
        mock_key = MagicMock()
        mock_paramiko.RSAKey.from_private_key.return_value = mock_key
        mock_client_instance = MagicMock()
        mock_paramiko.SSHClient.return_value = mock_client_instance
        mock_paramiko.AutoAddPolicy.return_value = MagicMock()

        # Import real exceptions for isinstance checks
        import paramiko as real_paramiko

        mock_paramiko.AuthenticationException = real_paramiko.AuthenticationException
        mock_paramiko.SSHException = real_paramiko.SSHException
        mock_client_instance.connect.side_effect = TimeoutError("Connection timed out")

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            with pytest.raises(SSHActivityError) as exc_info:
                _ssh_execute_command(
                    host="unreachable.host",
                    port=22,
                    user="user",
                    private_key="key",
                    command="echo",
                    workspace_path="",
                    timeout_seconds=30,
                )

        assert "timed out" in exc_info.value.cause

    def test_network_error_raises(self) -> None:
        mock_paramiko = MagicMock()
        mock_key = MagicMock()
        mock_paramiko.RSAKey.from_private_key.return_value = mock_key
        mock_client_instance = MagicMock()
        mock_paramiko.SSHClient.return_value = mock_client_instance
        mock_paramiko.AutoAddPolicy.return_value = MagicMock()

        import paramiko as real_paramiko

        mock_paramiko.AuthenticationException = real_paramiko.AuthenticationException
        mock_paramiko.SSHException = real_paramiko.SSHException
        mock_client_instance.connect.side_effect = OSError("Connection refused")

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            with pytest.raises(SSHActivityError) as exc_info:
                _ssh_execute_command(
                    host="down.host",
                    port=22,
                    user="user",
                    private_key="key",
                    command="echo",
                    workspace_path="",
                    timeout_seconds=30,
                )

        assert "network error" in exc_info.value.cause

    def test_client_close_called_on_success(self) -> None:
        mock_paramiko = _mock_paramiko_success()

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            _ssh_execute_command(
                host="h", port=22, user="u", private_key="k",
                command="echo", workspace_path="", timeout_seconds=60,
            )

        mock_paramiko.SSHClient.return_value.close.assert_called_once()

    def test_client_close_called_on_failure(self) -> None:
        mock_paramiko = MagicMock()
        mock_key = MagicMock()
        mock_paramiko.RSAKey.from_private_key.return_value = mock_key
        mock_client_instance = MagicMock()
        mock_paramiko.SSHClient.return_value = mock_client_instance
        mock_paramiko.AutoAddPolicy.return_value = MagicMock()

        import paramiko as real_paramiko

        mock_paramiko.AuthenticationException = real_paramiko.AuthenticationException
        mock_paramiko.SSHException = real_paramiko.SSHException
        mock_client_instance.connect.side_effect = OSError("fail")

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            with pytest.raises(SSHActivityError):
                _ssh_execute_command(
                    host="h", port=22, user="u", private_key="k",
                    command="echo", workspace_path="", timeout_seconds=60,
                )

        mock_client_instance.close.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: _ssh_cleanup_workspace (blocking helper)
# ---------------------------------------------------------------------------


class TestSSHCleanupWorkspace:
    def test_success(self) -> None:
        mock_paramiko = _mock_paramiko_success()

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            # Should not raise
            _ssh_cleanup_workspace(
                host="runner.internal",
                port=22,
                user="ai-runner",
                private_key="-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----",
                workspace_path="/tmp/workspace/PAY-4211",
            )

        mock_client = mock_paramiko.SSHClient.return_value
        mock_client.exec_command.assert_called_once_with(
            "rm -rf /tmp/workspace/PAY-4211",
            timeout=60.0,
        )

    def test_swallows_exceptions(self) -> None:
        """Cleanup is best-effort - exceptions are swallowed."""
        mock_paramiko = MagicMock()
        mock_key = MagicMock()
        mock_paramiko.RSAKey.from_private_key.return_value = mock_key
        mock_client_instance = MagicMock()
        mock_paramiko.SSHClient.return_value = mock_client_instance
        mock_paramiko.AutoAddPolicy.return_value = MagicMock()
        mock_client_instance.connect.side_effect = OSError("Connection refused")

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            # Should NOT raise
            _ssh_cleanup_workspace(
                host="unreachable",
                port=22,
                user="user",
                private_key="key",
                workspace_path="/tmp/workspace",
            )

    def test_key_parse_failure_swallowed(self) -> None:
        """If all key types fail to parse, cleanup returns silently."""
        mock_paramiko = MagicMock()

        import paramiko as real_paramiko

        mock_paramiko.RSAKey.from_private_key.side_effect = real_paramiko.SSHException("bad")
        mock_paramiko.Ed25519Key.from_private_key.side_effect = real_paramiko.SSHException("bad")
        mock_paramiko.ECDSAKey.from_private_key.side_effect = real_paramiko.SSHException("bad")
        mock_paramiko.SSHException = real_paramiko.SSHException
        mock_paramiko.SSHClient.return_value = MagicMock()
        mock_paramiko.AutoAddPolicy.return_value = MagicMock()

        with patch.dict("sys.modules", {"paramiko": mock_paramiko}):
            # Should NOT raise
            _ssh_cleanup_workspace(
                host="h", port=22, user="u",
                private_key="invalid-key",
                workspace_path="/tmp/ws",
            )


# ---------------------------------------------------------------------------
# Tests: ssh_connect_and_run (Temporal activity)
# ---------------------------------------------------------------------------


class TestSSHConnectAndRun:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        mock_result = RunResult(stdout="all tests passed", stderr="", exit_code=0)

        with (
            patch(
                "src.activities.ssh._ssh_execute_command",
                return_value=mock_result,
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await ssh_connect_and_run(
                cred=_VALID_CRED,
                command="pytest tests/",
                workspace_path="/tmp/workspace/PAY-4211",
                timeout_minutes=30,
            )

        assert result == {
            "stdout": "all tests passed",
            "stderr": "",
            "exit_code": 0,
        }

    @pytest.mark.asyncio
    async def test_non_zero_exit_code_is_not_error(self) -> None:
        mock_result = RunResult(stdout="", stderr="FAILED", exit_code=1)

        with (
            patch(
                "src.activities.ssh._ssh_execute_command",
                return_value=mock_result,
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await ssh_connect_and_run(
                cred=_VALID_CRED,
                command="pytest",
                workspace_path="/workspace",
                timeout_minutes=30,
            )

        assert result["exit_code"] == 1
        assert result["stderr"] == "FAILED"

    @pytest.mark.asyncio
    async def test_timeout_minutes_converted_to_seconds(self) -> None:
        mock_result = RunResult(stdout="ok", stderr="", exit_code=0)

        with (
            patch(
                "src.activities.ssh._ssh_execute_command",
                return_value=mock_result,
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            await ssh_connect_and_run(
                cred=_VALID_CRED,
                command="echo",
                workspace_path="",
                timeout_minutes=15,
            )

        # Verify timeout_seconds = 15 * 60 = 900
        call_args = mock_exec.call_args
        assert call_args[0][6] == 900  # timeout_seconds positional arg

    @pytest.mark.asyncio
    async def test_ssh_error_propagates(self) -> None:
        with (
            patch(
                "src.activities.ssh._ssh_execute_command",
                side_effect=SSHActivityError("host", "auth failed"),
            ),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(SSHActivityError) as exc_info:
                await ssh_connect_and_run(
                    cred=_VALID_CRED,
                    command="echo",
                    workspace_path="",
                    timeout_minutes=30,
                )

        assert exc_info.value.host == "host"
        assert "auth failed" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_default_timeout_is_30_minutes(self) -> None:
        mock_result = RunResult(stdout="", stderr="", exit_code=0)

        with (
            patch(
                "src.activities.ssh._ssh_execute_command",
                return_value=mock_result,
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            await ssh_connect_and_run(
                cred=_VALID_CRED,
                command="echo",
                workspace_path="",
            )

        call_args = mock_exec.call_args
        assert call_args[0][6] == 1800  # 30 * 60


# ---------------------------------------------------------------------------
# Tests: ssh_cleanup (Temporal activity)
# ---------------------------------------------------------------------------


class TestSSHCleanup:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        with (
            patch("src.activities.ssh._ssh_cleanup_workspace") as mock_cleanup,
            patch("temporalio.activity.logger"),
        ):
            await ssh_cleanup(
                cred=_VALID_CRED,
                workspace_path="/tmp/workspace/PAY-4211",
            )

        mock_cleanup.assert_called_once_with(
            "runner.internal",
            22,
            "ai-runner",
            _VALID_CRED["private_key"],
            "/tmp/workspace/PAY-4211",
        )

    @pytest.mark.asyncio
    async def test_swallows_exceptions(self) -> None:
        """ssh_cleanup is best-effort - exceptions are swallowed."""
        with (
            patch(
                "src.activities.ssh._ssh_cleanup_workspace",
                side_effect=RuntimeError("unexpected"),
            ),
            patch("temporalio.activity.logger"),
        ):
            # Should NOT raise
            await ssh_cleanup(
                cred=_VALID_CRED,
                workspace_path="/tmp/workspace",
            )

    @pytest.mark.asyncio
    async def test_returns_none(self) -> None:
        with (
            patch("src.activities.ssh._ssh_cleanup_workspace"),
            patch("temporalio.activity.logger"),
        ):
            result = await ssh_cleanup(
                cred=_VALID_CRED,
                workspace_path="/tmp/workspace",
            )

        assert result is None
