"""Unit tests for the Docker activity module.

Tests docker_build_image, docker_run_container, docker_stop_container,
docker_cleanup_container, docker_collect_logs, and docker_daemon_healthcheck
activities including:
- DockerBuildInput/DockerBuildResult/DockerRunInput/DockerRunResult/DockerCleanupInput dataclass correctness
- Build timeout handling
- Dockerfile not found error handling
- Docker daemon unavailable handling
- Resource limit parameters passed correctly
- Cleanup policy logic (always, on_success, never)
- Environment variable injection into docker run command
- docker_daemon_healthcheck returns bool

Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 1.8, 1.9
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.docker import (
    DockerBuildInput,
    DockerBuildResult,
    DockerCleanupInput,
    DockerHealthcheckInput,
    DockerRunInput,
    DockerRunResult,
    _should_perform_cleanup,
    build_docker_run_command,
    docker_build_image,
    docker_cleanup_container,
    docker_daemon_healthcheck,
    docker_stop_container,
)


# ---------------------------------------------------------------------------
# Tests: Dataclass correctness
# ---------------------------------------------------------------------------


class TestDockerBuildInput:
    def test_frozen_dataclass(self) -> None:
        inp = DockerBuildInput(
            dockerfile_path="/app/Dockerfile",
            image_tag="myapp:latest",
            workspace_path="/workspace",
        )
        assert inp.dockerfile_path == "/app/Dockerfile"
        assert inp.image_tag == "myapp:latest"
        assert inp.workspace_path == "/workspace"
        assert inp.build_timeout_seconds == 300
        assert inp.workflow_id == ""

    def test_immutable(self) -> None:
        inp = DockerBuildInput(
            dockerfile_path="/Dockerfile",
            image_tag="test:1",
            workspace_path="/ws",
        )
        with pytest.raises(AttributeError):
            inp.image_tag = "changed"  # type: ignore[misc]

    def test_custom_timeout(self) -> None:
        inp = DockerBuildInput(
            dockerfile_path="/Dockerfile",
            image_tag="test:1",
            workspace_path="/ws",
            build_timeout_seconds=600,
            workflow_id="wf-123",
        )
        assert inp.build_timeout_seconds == 600
        assert inp.workflow_id == "wf-123"

    def test_runner_context_fields(self) -> None:
        inp = DockerBuildInput(
            dockerfile_path="/Dockerfile",
            image_tag="test:1",
            workspace_path="/ws",
            workflow_id="wf-123",
            runner_id="runner-1",
            vault_path="vault:ssh/runners/runner-1/active",
        )
        assert inp.runner_id == "runner-1"
        assert inp.vault_path == "vault:ssh/runners/runner-1/active"


class TestDockerBuildResult:
    def test_success_result(self) -> None:
        result = DockerBuildResult(
            success=True,
            image_id="sha256:abc123",
            error=None,
            duration_seconds=45.2,
        )
        assert result.success is True
        assert result.image_id == "sha256:abc123"
        assert result.error is None
        assert result.duration_seconds == 45.2

    def test_failure_result(self) -> None:
        result = DockerBuildResult(
            success=False,
            image_id=None,
            error="Dockerfile not found",
            duration_seconds=1.5,
        )
        assert result.success is False
        assert result.image_id is None
        assert result.error == "Dockerfile not found"


class TestDockerRunInput:
    def test_defaults(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace",
        )
        assert inp.cpu_limit == 2.0
        assert inp.memory_limit_mb == 2048
        assert inp.timeout_seconds == 1800
        assert inp.max_timeout_seconds == 7200
        assert inp.environment is None
        assert inp.workflow_id == ""

    def test_custom_values(self) -> None:
        inp = DockerRunInput(
            image="node:18",
            command="npm test",
            workspace_path="/app",
            environment={"NODE_ENV": "test", "CI": "true"},
            cpu_limit=4.0,
            memory_limit_mb=4096,
            timeout_seconds=3600,
            max_timeout_seconds=7200,
            workflow_id="wf-456",
        )
        assert inp.cpu_limit == 4.0
        assert inp.memory_limit_mb == 4096
        assert inp.environment == {"NODE_ENV": "test", "CI": "true"}


class TestDockerRunResult:
    def test_success_result(self) -> None:
        result = DockerRunResult(
            container_id="abc123def",
            exit_code=0,
            stdout="all tests passed",
            stderr="",
            log_artifact_uri="s3://ai-runs/docker-logs/abc123def/container.log",
        )
        assert result.container_id == "abc123def"
        assert result.exit_code == 0
        assert result.log_artifact_uri is not None

    def test_failure_result(self) -> None:
        result = DockerRunResult(
            container_id="xyz789",
            exit_code=1,
            stdout="",
            stderr="test failed",
            log_artifact_uri=None,
        )
        assert result.exit_code == 1
        assert result.log_artifact_uri is None


class TestDockerCleanupInput:
    def test_on_success_policy(self) -> None:
        inp = DockerCleanupInput(
            container_id="abc123",
            image_id="sha256:def456",
            policy="on_success",
            task_succeeded=True,
        )
        assert inp.policy == "on_success"
        assert inp.task_succeeded is True

    def test_always_policy(self) -> None:
        inp = DockerCleanupInput(
            container_id="abc123",
            image_id=None,
            policy="always",
            task_succeeded=False,
        )
        assert inp.policy == "always"
        assert inp.image_id is None

    def test_never_policy(self) -> None:
        inp = DockerCleanupInput(
            container_id="abc123",
            image_id="img:1",
            policy="never",
            task_succeeded=True,
        )
        assert inp.policy == "never"


# ---------------------------------------------------------------------------
# Tests: _should_perform_cleanup helper
# ---------------------------------------------------------------------------


class TestShouldPerformCleanup:
    """Tests for the cleanup policy decision logic.

    Requirements: 1.6, 1.7
    """

    def test_always_policy_task_succeeded(self) -> None:
        assert _should_perform_cleanup("always", True) is True

    def test_always_policy_task_failed(self) -> None:
        assert _should_perform_cleanup("always", False) is True

    def test_on_success_policy_task_succeeded(self) -> None:
        assert _should_perform_cleanup("on_success", True) is True

    def test_on_success_policy_task_failed(self) -> None:
        assert _should_perform_cleanup("on_success", False) is False

    def test_never_policy_task_succeeded(self) -> None:
        assert _should_perform_cleanup("never", True) is False

    def test_never_policy_task_failed(self) -> None:
        assert _should_perform_cleanup("never", False) is False


# ---------------------------------------------------------------------------
# Tests: build_docker_run_command (env var injection)
# ---------------------------------------------------------------------------


class TestBuildDockerRunCommand:
    """Tests for docker run command construction.

    Requirements: 1.3, 1.9
    """

    def test_resource_limits_in_command(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace",
            cpu_limit=2.0,
            memory_limit_mb=2048,
        )
        cmd = build_docker_run_command(inp)
        assert "--cpus=2.0" in cmd
        assert "--memory=2048m" in cmd

    def test_volume_mount_in_command(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace/project",
        )
        cmd = build_docker_run_command(inp)
        assert "-v" in cmd
        assert ":rw" in cmd

    def test_environment_variables_injected(self) -> None:
        """All env vars must appear as --env parameters (Requirement 1.9)."""
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace",
            environment={
                "DB_HOST": "localhost",
                "DB_PORT": "5432",
                "APP_ENV": "test",
            },
        )
        cmd = build_docker_run_command(inp)
        assert "--env" in cmd
        # All three env vars should be present
        assert "DB_HOST=localhost" in cmd
        assert "DB_PORT=5432" in cmd
        assert "APP_ENV=test" in cmd

    def test_no_environment_variables(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace",
            environment=None,
        )
        cmd = build_docker_run_command(inp)
        assert "--env" not in cmd

    def test_empty_environment_variables(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest",
            workspace_path="/workspace",
            environment={},
        )
        cmd = build_docker_run_command(inp)
        assert "--env" not in cmd

    def test_image_in_command(self) -> None:
        inp = DockerRunInput(
            image="node:18-alpine",
            command="npm test",
            workspace_path="/workspace",
        )
        cmd = build_docker_run_command(inp)
        assert "node:18-alpine" in cmd

    def test_command_in_docker_run(self) -> None:
        inp = DockerRunInput(
            image="python:3.12",
            command="pytest tests/ -v",
            workspace_path="/workspace",
        )
        cmd = build_docker_run_command(inp)
        assert "pytest tests/ -v" in cmd


# ---------------------------------------------------------------------------
# Tests: docker_build_image activity
# ---------------------------------------------------------------------------


class TestDockerBuildImage:
    """Tests for docker_build_image activity.

    Requirements: 1.1, 1.2
    """

    @pytest.mark.asyncio
    async def test_build_success(self) -> None:
        """Successful build returns success=True with image_id."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        build_output = "Successfully built sha256:abc123def456"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=(build_output, "", 0),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerBuildInput(
                dockerfile_path="/app/Dockerfile",
                image_tag="myapp:latest",
                workspace_path="/workspace",
            )
            result = await docker_build_image(inp)

        assert result.success is True
        assert result.image_id is not None
        assert result.error is None
        assert result.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_build_uses_resolved_vault_path(self) -> None:
        """Build fetches credentials from the selected runner path."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"
        mock_get_credentials = AsyncMock(return_value=mock_cred)

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new=mock_get_credentials,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("Successfully built sha256:abc", "", 0),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerBuildInput(
                dockerfile_path="/app/Dockerfile",
                image_tag="myapp:latest",
                workspace_path="/workspace",
                workflow_id="wf-123",
                runner_id="runner-1",
                vault_path="vault:ssh/runners/runner-1/active",
            )
            result = await docker_build_image(inp)

        assert result.success is True
        mock_get_credentials.assert_awaited_once_with(
            "wf-123",
            "vault:ssh/runners/runner-1/active",
        )

    @pytest.mark.asyncio
    async def test_build_timeout(self) -> None:
        """Build timeout returns success=False with timeout error."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                side_effect=RuntimeError("Command timed out after 300s"),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerBuildInput(
                dockerfile_path="/app/Dockerfile",
                image_tag="myapp:latest",
                workspace_path="/workspace",
                build_timeout_seconds=300,
            )
            result = await docker_build_image(inp)

        assert result.success is False
        assert result.image_id is None
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_dockerfile_not_found(self) -> None:
        """Missing Dockerfile returns success=False with descriptive error."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=(
                    "",
                    "unable to prepare context: unable to evaluate symlinks in Dockerfile path: lstat /app/Dockerfile: no such file or directory",
                    1,
                ),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerBuildInput(
                dockerfile_path="/app/Dockerfile",
                image_tag="myapp:latest",
                workspace_path="/workspace",
            )
            result = await docker_build_image(inp)

        assert result.success is False
        assert result.image_id is None
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_credential_fetch_failure(self) -> None:
        """Credential fetch failure returns success=False."""
        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Vault unreachable"),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerBuildInput(
                dockerfile_path="/app/Dockerfile",
                image_tag="myapp:latest",
                workspace_path="/workspace",
            )
            result = await docker_build_image(inp)

        assert result.success is False
        assert "credential" in result.error.lower()


# ---------------------------------------------------------------------------
# Tests: docker_stop_container activity
# ---------------------------------------------------------------------------


class TestDockerStopContainer:
    """Tests for docker_stop_container activity.

    Requirements: 1.5
    """

    @pytest.mark.asyncio
    async def test_stop_success(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("container_id", "", 0),
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            await docker_stop_container("abc123", grace_period=30)

        # Verify the stop command includes grace period
        call_args = mock_exec.call_args
        cmd = call_args[0][4]  # command argument
        assert "docker stop" in cmd
        assert "-t 30" in cmd
        assert "abc123" in cmd

    @pytest.mark.asyncio
    async def test_stop_custom_grace_period(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "", 0),
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            await docker_stop_container("xyz789", grace_period=60)

        call_args = mock_exec.call_args
        cmd = call_args[0][4]
        assert "-t 60" in cmd

    @pytest.mark.asyncio
    async def test_stop_credential_failure_raises(self) -> None:
        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Vault down"),
            ),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(RuntimeError, match="credential"):
                await docker_stop_container("abc123")


# ---------------------------------------------------------------------------
# Tests: docker_cleanup_container activity
# ---------------------------------------------------------------------------


class TestDockerCleanupContainer:
    """Tests for docker_cleanup_container activity.

    Requirements: 1.6, 1.7
    """

    @pytest.mark.asyncio
    async def test_always_policy_cleans_up(self) -> None:
        """Policy 'always' should always perform cleanup."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "", 0),
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id="sha256:def456",
                policy="always",
                task_succeeded=False,
            )
            await docker_cleanup_container(inp)

        # Should have called docker rm and docker rmi
        calls = mock_exec.call_args_list
        commands = [call[0][4] for call in calls]
        assert any("docker rm" in cmd for cmd in commands)
        assert any("docker rmi" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_cleanup_uses_resolved_vault_path(self) -> None:
        """Cleanup uses the same runner credential path as build/run."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"
        mock_get_credentials = AsyncMock(return_value=mock_cred)

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new=mock_get_credentials,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "", 0),
            ),
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id="sha256:def456",
                policy="always",
                task_succeeded=True,
                workflow_id="wf-clean",
                runner_id="runner-1",
                vault_path="vault:ssh/runners/runner-1/active",
            )
            await docker_cleanup_container(inp)

        mock_get_credentials.assert_awaited_once_with(
            "wf-clean",
            "vault:ssh/runners/runner-1/active",
        )

    @pytest.mark.asyncio
    async def test_on_success_policy_task_succeeded(self) -> None:
        """Policy 'on_success' with success should cleanup."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "", 0),
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id="sha256:def456",
                policy="on_success",
                task_succeeded=True,
            )
            await docker_cleanup_container(inp)

        # Should have called docker rm and docker rmi
        calls = mock_exec.call_args_list
        commands = [call[0][4] for call in calls]
        assert any("docker rm" in cmd for cmd in commands)
        assert any("docker rmi" in cmd for cmd in commands)

    @pytest.mark.asyncio
    async def test_on_success_policy_task_failed_skips(self) -> None:
        """Policy 'on_success' with failure should skip cleanup."""
        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
            ) as mock_cred_fn,
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id="sha256:def456",
                policy="on_success",
                task_succeeded=False,
            )
            await docker_cleanup_container(inp)

        # Should NOT have fetched credentials (cleanup skipped early)
        mock_cred_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_never_policy_skips(self) -> None:
        """Policy 'never' should always skip cleanup."""
        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
            ) as mock_cred_fn,
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id="sha256:def456",
                policy="never",
                task_succeeded=True,
            )
            await docker_cleanup_container(inp)

        mock_cred_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_image_id_skips_rmi(self) -> None:
        """When image_id is None, docker rmi should not be called."""
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "", 0),
            ) as mock_exec,
            patch("temporalio.activity.logger"),
        ):
            inp = DockerCleanupInput(
                container_id="abc123",
                image_id=None,
                policy="always",
                task_succeeded=True,
            )
            await docker_cleanup_container(inp)

        # Should only have docker rm, not docker rmi
        calls = mock_exec.call_args_list
        commands = [call[0][4] for call in calls]
        assert any("docker rm" in cmd for cmd in commands)
        assert not any("docker rmi" in cmd for cmd in commands)


# ---------------------------------------------------------------------------
# Tests: docker_daemon_healthcheck activity
# ---------------------------------------------------------------------------


class TestDockerDaemonHealthcheck:
    """Tests for docker_daemon_healthcheck activity.

    Requirements: 1.8
    """

    @pytest.mark.asyncio
    async def test_healthy_daemon(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("Docker version 24.0.7", "", 0),
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await docker_daemon_healthcheck()

        assert result is True

    @pytest.mark.asyncio
    async def test_healthcheck_uses_resolved_vault_path(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"
        mock_get_credentials = AsyncMock(return_value=mock_cred)

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new=mock_get_credentials,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("Docker version 24.0.7", "", 0),
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await docker_daemon_healthcheck(
                DockerHealthcheckInput(
                    workflow_id="wf-hc",
                    runner_id="runner-1",
                    vault_path="vault:ssh/runners/runner-1/active",
                )
            )

        assert result is True
        mock_get_credentials.assert_awaited_once_with(
            "wf-hc",
            "vault:ssh/runners/runner-1/active",
        )

    @pytest.mark.asyncio
    async def test_unhealthy_daemon_exit_code(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                return_value=("", "Cannot connect to Docker daemon", 1),
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await docker_daemon_healthcheck()

        assert result is False

    @pytest.mark.asyncio
    async def test_unhealthy_daemon_ssh_error(self) -> None:
        mock_cred = MagicMock()
        mock_cred.host = "runner.internal"
        mock_cred.port = 22
        mock_cred.user = "ai-runner"
        mock_cred.private_key = "fake-key"

        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                return_value=mock_cred,
            ),
            patch(
                "src.activities.docker._ssh_exec",
                side_effect=RuntimeError("SSH connection failed"),
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await docker_daemon_healthcheck()

        assert result is False

    @pytest.mark.asyncio
    async def test_credential_failure_returns_false(self) -> None:
        with (
            patch(
                "src.activities.docker._get_ssh_credentials",
                new_callable=AsyncMock,
                side_effect=RuntimeError("Vault unreachable"),
            ),
            patch("temporalio.activity.logger"),
        ):
            result = await docker_daemon_healthcheck()

        assert result is False
