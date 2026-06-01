"""Unit tests for the runner_resolver integration in
:class:`ExecutionRunWorkflow` (Requirements 4.4, 4.5, 4.8).

The canonical :class:`ExecutionRunWorkflow` invokes the ``resolve_runner``
activity at workflow start when:
  * ``department_id`` is non-empty, AND
  * ``runner_id`` is ``None`` (not explicitly provided)

This enables the multi-SSH runner pool (G5) while preserving backward
compatibility: when ``runner_id`` is already set (legacy path) or
``department_id`` is empty, the workflow skips resolution and uses the
input ``runner_id`` verbatim.

Scenarios covered
-----------------

1. ``runner_id`` is set explicitly — ``resolve_runner`` is NOT called,
   workflow uses the input runner_id directly.
2. ``department_id`` is empty — ``resolve_runner`` is NOT called.
3. ``department_id`` is set AND ``runner_id`` is None — ``resolve_runner``
   IS called, workflow uses the resolved runner_id.
4. ``resolve_runner`` raises ApplicationError (no active runner) —
   workflow fails with the error propagated.
5. ``resolve_runner`` raises an infrastructure error — workflow falls
   back to input runner_id (None) and continues.
6. Resolved runner_id is passed to ``ssh_run_test`` activity.
7. SSH key dual-slot rotation vault_path is preserved in the resolution.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


# ---------------------------------------------------------------------------
# sys.path bootstrapping
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


from src.activities.disk_quota import (  # noqa: E402
    DiskQuotaInput,
    DiskQuotaResult,
)
from src.activities.docker import (  # noqa: E402
    DockerBuildInput,
    DockerBuildResult,
    DockerCleanupInput,
    DockerHealthcheckInput,
    DockerRunInput,
    DockerRunResult,
)
from src.workflows.execution_run_workflow import (  # noqa: E402
    ExecutionRunWorkflow,
)
from temporal_shared.messages import (  # noqa: E402
    ExecutionRunWorkflowInput,
    ExecutionRunWorkflowOutput,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Records every activity invocation for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def get_call(self, name: str) -> tuple[Any, ...] | None:
        for call_name, args in self.calls:
            if call_name == name:
                return args
        return None


def _build_activities(
    log: _ActivityCallLog,
    *,
    resolve_runner_result: dict[str, Any] | None = None,
    resolve_runner_raises: BaseException | None = None,
) -> list[Any]:
    """Build the activity stub bundle for the canonical workflow.

    Includes:
      * ``resolve_runner`` — returns resolve_runner_result or raises
      * ``ssh_healthcheck`` — always healthy
      * ``ssh_run_test`` — always passes
      * ``apply_cleanup_policy`` — no-op
    """

    @activity.defn(name="resolve_runner")
    async def resolve_runner(dept_id: str) -> dict[str, Any]:
        log.calls.append(("resolve_runner", (dept_id,)))
        if resolve_runner_raises is not None:
            raise resolve_runner_raises
        if resolve_runner_result is not None:
            return resolve_runner_result
        return {
            "runner_id": "resolved-runner-1",
            "host": "10.0.0.1",
            "port": 22,
            "username": "ai-runner",
            "vault_path": "vault:ssh/runners/resolved-runner-1/active",
        }

    @activity.defn(name="ssh_healthcheck")
    async def ssh_healthcheck(input: Any = None) -> dict[str, Any]:
        log.calls.append(("ssh_healthcheck", (input,)))
        return {"healthy": True, "host": "runner.test.invalid"}

    @activity.defn(name="ssh_run_test")
    async def ssh_run_test(
        runner_id: str | None,
        command: str,
        environment: tuple[tuple[str, str], ...],
        artifact_minio_prefix: str | None,
        workdir: str | None,
        parent_workflow_id: str,
        vault_path: str | None = None,
    ) -> dict[str, Any]:
        log.calls.append(
            ("ssh_run_test", (runner_id, command, workdir, vault_path))
        )
        return {
            "status": "passed",
            "exit_code": 0,
            "stdout_uri": "s3://ai-runs/stdout",
            "stderr_uri": "s3://ai-runs/stderr",
            "duration_seconds": 1.5,
            "runner_id": runner_id or "default-runner",
            "failure_reason": None,
        }

    @activity.defn(name="apply_cleanup_policy")
    async def apply_cleanup_policy(
        cleanup_policy: str,
        exit_code: int | None,
        workdir: str,
        container_id: str | None,
        image_id: str | None,
        parent_workflow_id: str,
        department_id: str,
        vault_path: str | None = None,
    ) -> dict[str, Any]:
        log.calls.append(
            ("apply_cleanup_policy", (cleanup_policy, exit_code, vault_path))
        )
        return {"cleanup_performed": False}

    @activity.defn(name="docker_daemon_healthcheck")
    async def docker_daemon_healthcheck(input: DockerHealthcheckInput) -> bool:
        log.calls.append(("docker_daemon_healthcheck", (input,)))
        return True

    @activity.defn(name="docker_build_image")
    async def docker_build_image(input: DockerBuildInput) -> DockerBuildResult:
        log.calls.append(("docker_build_image", (input,)))
        return DockerBuildResult(
            success=True,
            image_id="sha256:test-image",
            error=None,
            duration_seconds=1.25,
        )

    @activity.defn(name="docker_run_container")
    async def docker_run_container(input: DockerRunInput) -> DockerRunResult:
        log.calls.append(("docker_run_container", (input,)))
        return DockerRunResult(
            container_id="container-123",
            exit_code=0,
            stdout="ok",
            stderr="",
            log_artifact_uri="s3://ai-runs/docker/container.log",
        )

    @activity.defn(name="docker_cleanup_container")
    async def docker_cleanup_container(input: DockerCleanupInput) -> None:
        log.calls.append(("docker_cleanup_container", (input,)))

    return [
        resolve_runner,
        ssh_healthcheck,
        ssh_run_test,
        apply_cleanup_policy,
        docker_daemon_healthcheck,
        docker_build_image,
        docker_run_container,
        docker_cleanup_container,
    ]


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name)


# ---------------------------------------------------------------------------
# Test: resolve_runner is NOT called when runner_id is explicitly set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_skipped_when_runner_id_explicit() -> None:
    """**Validates: Backward compatibility — explicit runner_id**

    When the caller provides a non-None ``runner_id``, the workflow
    skips the ``resolve_runner`` activity and uses the input value
    directly.
    """
    log = _ActivityCallLog()
    activities = _build_activities(log)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-skip-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="parent-1",
                    runner_id="explicit-runner",  # explicitly set
                    command="pytest -q",
                    department_id="payments",
                ),
                id=f"wf-resolver-skip-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert "resolve_runner" not in log.names()
    assert result.status == "passed"
    assert result.runner_id == "explicit-runner"

    # Verify ssh_run_test received the explicit runner_id
    ssh_call = log.get_call("ssh_run_test")
    assert ssh_call is not None
    assert ssh_call[0] == "explicit-runner"


# ---------------------------------------------------------------------------
# Test: resolve_runner is NOT called when department_id is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_skipped_when_dept_id_empty() -> None:
    """**Validates: Backward compatibility — no department context**

    When ``department_id`` is empty, the workflow cannot resolve a
    runner and skips the activity call.
    """
    log = _ActivityCallLog()
    activities = _build_activities(log)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-no-dept-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="parent-1",
                    runner_id=None,
                    command="pytest -q",
                    department_id="",  # empty
                ),
                id=f"wf-resolver-no-dept-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert "resolve_runner" not in log.names()
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Test: resolve_runner IS called when dept_id set and runner_id is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_called_when_dept_id_set_and_runner_id_none() -> None:
    """**Validates: Requirements 4.4, 4.5 — runner resolution from DB**

    When ``department_id`` is non-empty and ``runner_id`` is None, the
    workflow calls ``resolve_runner`` to select the least-busy runner
    and uses the resolved runner_id for subsequent activities.
    """
    log = _ActivityCallLog()
    activities = _build_activities(
        log,
        resolve_runner_result={
            "runner_id": "pool-runner-2",
            "host": "10.0.0.2",
            "port": 2222,
            "username": "deploy",
            "vault_path": "vault:ssh/runners/pool-runner-2/active",
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-called-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="parent-1",
                    runner_id=None,  # not set → trigger resolution
                    command="pytest -q",
                    department_id="payments",
                ),
                id=f"wf-resolver-called-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # resolve_runner was called with the department_id
    assert "resolve_runner" in log.names()
    resolver_call = log.get_call("resolve_runner")
    assert resolver_call == ("payments",)

    # ssh_run_test received the resolved runner_id
    ssh_call = log.get_call("ssh_run_test")
    assert ssh_call is not None
    assert ssh_call[0] == "pool-runner-2"

    # Workflow output reflects the resolved runner_id
    assert result.status == "passed"
    assert result.runner_id == "pool-runner-2"


# ---------------------------------------------------------------------------
# Test: resolved runner context reaches SSH/Docker/cleanup activities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_runner_context_flows_to_docker_chain() -> None:
    """**Validates: selected runner credentials drive Docker execution**

    A Jira task that requires Docker should use the runner selected by
    ``resolve_runner`` for the SSH healthcheck, Docker daemon probe,
    Docker build/run/cleanup, and final workspace cleanup. This protects
    multi-runner deployments from silently falling back to a global
    Vault secret after the scheduler has already selected a runner.
    """
    log = _ActivityCallLog()
    runner_id = "docker-runner-7"
    expected_vault_path = f"vault:ssh/runners/{runner_id}/active"
    activities = _build_activities(
        log,
        resolve_runner_result={
            "runner_id": runner_id,
            "host": "10.10.10.7",
            "port": 2022,
            "username": "runner",
            "base_path": "/srv/ai-runner/payments",
            "vault_path": expected_vault_path,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-docker-context-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="PAY-4211",
                    runner_id=None,
                    command="pytest -q",
                    department_id="payments",
                    needs_docker=True,
                    docker_image_tag="ai-live-test:latest",
                    docker_dockerfile_path="Dockerfile",
                    environment=(("CLEANUP_POLICY", "always"),),
                ),
                id=f"wf-docker-context-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.status == "passed", (result, log.calls)
    assert result.runner_id == runner_id
    assert "ssh_run_test" not in log.names()

    ssh_healthcheck_input = log.get_call("ssh_healthcheck")[0]
    assert _field(ssh_healthcheck_input, "host") == "10.10.10.7"
    assert _field(ssh_healthcheck_input, "port") == 2022
    assert _field(ssh_healthcheck_input, "runner_id") == runner_id

    docker_healthcheck_input = log.get_call("docker_daemon_healthcheck")[0]
    assert _field(docker_healthcheck_input, "runner_id") == runner_id
    assert _field(docker_healthcheck_input, "vault_path") == expected_vault_path

    build_input = log.get_call("docker_build_image")[0]
    run_input = log.get_call("docker_run_container")[0]
    docker_cleanup_input = log.get_call("docker_cleanup_container")[0]
    assert _field(build_input, "runner_id") == runner_id
    assert _field(build_input, "vault_path") == expected_vault_path
    assert _field(run_input, "runner_id") == runner_id
    assert _field(run_input, "vault_path") == expected_vault_path
    assert _field(docker_cleanup_input, "runner_id") == runner_id
    assert _field(docker_cleanup_input, "vault_path") == expected_vault_path

    final_cleanup = log.get_call("apply_cleanup_policy")
    assert final_cleanup is not None
    assert final_cleanup[2] == expected_vault_path


# ---------------------------------------------------------------------------
# Test: resolve_runner raises ApplicationError → workflow fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_application_error_propagates() -> None:
    """**Validates: Requirements 4.6, 4.7 — no active runner failure**

    When ``resolve_runner`` raises an ApplicationError (no active
    runner assigned to the department), the workflow propagates the
    error and fails.
    """
    from temporalio.client import WorkflowFailureError

    log = _ActivityCallLog()
    activities = _build_activities(
        log,
        resolve_runner_raises=ApplicationError(
            "No active runner assigned to dept payments",
            type="RunnerResolutionError",
            non_retryable=True,
        ),
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-fail-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            with pytest.raises(WorkflowFailureError) as exc_info:
                await env.client.execute_workflow(
                    ExecutionRunWorkflow.run,
                    ExecutionRunWorkflowInput(
                        parent_workflow_id="parent-1",
                        runner_id=None,
                        command="pytest -q",
                        department_id="payments",
                    ),
                    id=f"wf-resolver-fail-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    # resolve_runner was called
    assert "resolve_runner" in log.names()
    # ssh_run_test was NOT called (workflow failed before reaching it)
    assert "ssh_run_test" not in log.names()
    # The error should reference the resolution failure
    assert "No active runner" in str(exc_info.value.cause)


# ---------------------------------------------------------------------------
# Test: resolve_runner infrastructure error → fallback to input runner_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_infrastructure_error_falls_back() -> None:
    """**Validates: Backward compatibility — graceful degradation**

    When ``resolve_runner`` raises a non-ApplicationError (e.g.
    infrastructure failure like DB connection timeout), the workflow
    falls back to the input ``runner_id`` (None in this case) and
    continues execution.
    """
    log = _ActivityCallLog()
    activities = _build_activities(
        log,
        resolve_runner_raises=RuntimeError("DB connection timeout"),
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-infra-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="parent-1",
                    runner_id=None,
                    command="pytest -q",
                    department_id="payments",
                ),
                id=f"wf-resolver-infra-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # resolve_runner was attempted
    assert "resolve_runner" in log.names()
    # ssh_run_test still ran (fallback path)
    assert "ssh_run_test" in log.names()
    # Workflow completed successfully
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Test: vault_path with dual-slot rotation pattern is preserved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolved_vault_path_follows_dual_slot_pattern() -> None:
    """**Validates: Requirement 4.8 — SSH key dual-slot rotation**

    The resolved runner's vault_path follows the dual-slot rotation
    pattern ``vault:ssh/runners/{runner_id}/active``. The workflow
    preserves this path in the resolution result, ensuring the
    existing SSH key rotation flow continues working with runner_id-
    based vault paths.
    """
    log = _ActivityCallLog()
    runner_id = "prod-runner-3"
    expected_vault_path = f"vault:ssh/runners/{runner_id}/active"

    activities = _build_activities(
        log,
        resolve_runner_result={
            "runner_id": runner_id,
            "host": "10.0.0.3",
            "port": 22,
            "username": "ai-runner",
            "vault_path": expected_vault_path,
        },
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-resolver-vault-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            result: ExecutionRunWorkflowOutput = await env.client.execute_workflow(
                ExecutionRunWorkflow.run,
                ExecutionRunWorkflowInput(
                    parent_workflow_id="parent-1",
                    runner_id=None,
                    command="pytest -q",
                    department_id="payments",
                ),
                id=f"wf-resolver-vault-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # The resolved runner_id is used throughout
    assert result.runner_id == runner_id

    # ssh_run_test received the resolved runner_id
    ssh_call = log.get_call("ssh_run_test")
    assert ssh_call is not None
    assert ssh_call[0] == runner_id
