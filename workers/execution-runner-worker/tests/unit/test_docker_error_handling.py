"""Unit tests: Docker Activity error handling.

Tests build timeout, Dockerfile not found, Docker daemon unavailable,
and verifies resource limit parameters are correctly passed via the
:func:`build_docker_run_command` pure helper.

Validates Requirements: 1.2, 1.8
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Make ``src`` importable without installing the worker package.
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from src.activities.docker import (  # noqa: E402
    DockerBuildInput,
    DockerRunInput,
    build_docker_run_command,
    docker_build_image,
    docker_daemon_healthcheck,
)


# ---------------------------------------------------------------------------
# Resource limits & env injection (Requirement 1.9 — verified via 1.8 path)
# ---------------------------------------------------------------------------


class TestBuildDockerRunCommand:
    """Verify resource limits, mounts, and env vars are passed correctly.

    Validates Requirements: 1.2 (resource limits respected at command
    construction time), 1.8 (env injection survives daemon faults).
    """

    def test_cpu_limit_in_command(self) -> None:
        inp = DockerRunInput(
            image="test:latest",
            command="echo hi",
            workspace_path="/tmp/ws",
            cpu_limit=2.0,
            memory_limit_mb=2048,
        )
        cmd = build_docker_run_command(inp)
        assert "--cpus=2.0" in cmd

    def test_memory_limit_in_command(self) -> None:
        inp = DockerRunInput(
            image="test:latest",
            command="echo hi",
            workspace_path="/tmp/ws",
            cpu_limit=1.0,
            memory_limit_mb=1024,
        )
        cmd = build_docker_run_command(inp)
        assert "--memory=1024m" in cmd

    def test_workspace_volume_mount(self) -> None:
        inp = DockerRunInput(
            image="test:latest",
            command="echo hi",
            workspace_path="/var/runner/ws-1",
            cpu_limit=1.0,
            memory_limit_mb=512,
        )
        cmd = build_docker_run_command(inp)
        assert "/var/runner/ws-1" in cmd
        # Mount in :rw mode
        assert ":rw" in cmd

    def test_environment_variables_included(self) -> None:
        inp = DockerRunInput(
            image="test:latest",
            command="echo hi",
            workspace_path="/tmp/ws",
            environment={"FOO": "bar", "BAZ": "qux"},
            cpu_limit=1.0,
            memory_limit_mb=512,
        )
        cmd = build_docker_run_command(inp)
        assert "FOO" in cmd and "bar" in cmd
        assert "BAZ" in cmd and "qux" in cmd
        # Each variable injected via --env
        assert cmd.count("--env") == 2

    def test_image_and_command_present(self) -> None:
        inp = DockerRunInput(
            image="python:3.12-slim",
            command="pytest -q",
            workspace_path="/ws",
            cpu_limit=2.0,
            memory_limit_mb=1024,
        )
        cmd = build_docker_run_command(inp)
        assert "python:3.12-slim" in cmd
        assert "pytest -q" in cmd


# ---------------------------------------------------------------------------
# Build error scenarios (Requirement 1.2)
# ---------------------------------------------------------------------------


class _FakeCred:
    host = "runner.example.com"
    port = 22
    user = "runner"
    private_key = "fake-key"


@pytest.mark.asyncio
async def test_build_dockerfile_not_found_returns_error() -> None:
    """When ``docker build`` fails because the Dockerfile is missing,
    the activity returns ``success=False`` with an explanatory error.

    Validates Requirement: 1.2
    """
    inp = DockerBuildInput(
        dockerfile_path="/missing/Dockerfile",
        image_tag="test:latest",
        workspace_path="/workspace",
        build_timeout_seconds=300,
        workflow_id="wf-1",
    )

    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(return_value=_FakeCred()),
    ), patch(
        "src.activities.docker.asyncio.to_thread",
        new=AsyncMock(
            return_value=(
                "",
                "Cannot locate specified Dockerfile: no such file or directory",
                1,
            )
        ),
    ):
        result = await docker_build_image(inp)

    assert result.success is False
    assert result.image_id is None
    assert result.error is not None
    assert "Dockerfile not found" in result.error or "no such file" in result.error.lower()


@pytest.mark.asyncio
async def test_build_timeout_surfaces_as_error() -> None:
    """When the SSH build command raises a timeout, the activity reports
    a failure rather than crashing.

    Validates Requirement: 1.2
    """
    inp = DockerBuildInput(
        dockerfile_path="/app/Dockerfile",
        image_tag="test:latest",
        workspace_path="/workspace",
        build_timeout_seconds=1,
        workflow_id="wf-2",
    )

    async def _raise_runtime(*args, **kwargs):
        raise RuntimeError("Command timed out after 1s")

    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(return_value=_FakeCred()),
    ), patch(
        "src.activities.docker.asyncio.to_thread",
        new=AsyncMock(side_effect=RuntimeError("Command timed out after 1s")),
    ):
        result = await docker_build_image(inp)

    assert result.success is False
    assert result.error is not None
    assert "timed out" in result.error.lower() or "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_build_credential_fetch_failure_returns_error() -> None:
    """When SSH credentials cannot be fetched, the activity returns a
    structured failure result instead of raising.

    Validates Requirement: 1.2
    """
    inp = DockerBuildInput(
        dockerfile_path="/app/Dockerfile",
        image_tag="test:latest",
        workspace_path="/workspace",
        workflow_id="wf-3",
    )

    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(side_effect=RuntimeError("Vault unreachable")),
    ):
        result = await docker_build_image(inp)

    assert result.success is False
    assert result.image_id is None
    assert "credential" in (result.error or "").lower()


# ---------------------------------------------------------------------------
# Docker daemon healthcheck (Requirement 1.8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_healthcheck_returns_false_on_credential_failure() -> None:
    """If credentials fetch fails, the daemon healthcheck reports unhealthy.

    Validates Requirement: 1.8
    """
    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(side_effect=RuntimeError("Vault down")),
    ):
        healthy = await docker_daemon_healthcheck()
    assert healthy is False


@pytest.mark.asyncio
async def test_daemon_healthcheck_returns_false_on_ssh_error() -> None:
    """If ``docker info`` raises an SSH RuntimeError, daemon is unhealthy.

    Validates Requirement: 1.8
    """
    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(return_value=_FakeCred()),
    ), patch(
        "src.activities.docker.asyncio.to_thread",
        new=AsyncMock(side_effect=RuntimeError("ssh exec failed")),
    ):
        healthy = await docker_daemon_healthcheck()
    assert healthy is False


@pytest.mark.asyncio
async def test_daemon_healthcheck_returns_false_on_nonzero_exit() -> None:
    """If ``docker info`` exits non-zero, daemon is unhealthy.

    Validates Requirement: 1.8
    """
    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(return_value=_FakeCred()),
    ), patch(
        "src.activities.docker.asyncio.to_thread",
        new=AsyncMock(return_value=("", "Cannot connect to docker daemon", 1)),
    ):
        healthy = await docker_daemon_healthcheck()
    assert healthy is False


@pytest.mark.asyncio
async def test_daemon_healthcheck_returns_true_on_success() -> None:
    """When ``docker info`` succeeds, the daemon is reported healthy.

    Validates Requirement: 1.8
    """
    with patch(
        "src.activities.docker._get_ssh_credentials",
        new=AsyncMock(return_value=_FakeCred()),
    ), patch(
        "src.activities.docker.asyncio.to_thread",
        new=AsyncMock(return_value=("Server Version: 24.0.5", "", 0)),
    ):
        healthy = await docker_daemon_healthcheck()
    assert healthy is True
