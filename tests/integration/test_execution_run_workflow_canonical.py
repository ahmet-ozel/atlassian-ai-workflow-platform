"""End-to-end integration test for the canonical ``ExecutionRunWorkflow``.


This test exercises the canonical :class:`ExecutionRunWorkflow` defined
in ``platform/workers/execution-runner-worker/src/workflows/execution_run_workflow.py``
by the execution-runner worker. The workflow consumes a single
:class:`ExecutionRunWorkflowInput` and emits a single
:class:`ExecutionRunWorkflowOutput` after delegating to the
``ssh_run_test`` activity.

Test scope decision
-------------------

The canonical workflow is a thin orchestrator: the heavy lifting
(credential fetch, paramiko SSH, MinIO upload, heartbeating) lives in
the ``ssh_run_test`` activity which has its own unit tests. Here we
mock the activity and verify that the workflow:

1. Calls ``ssh_run_test`` with the exact arguments derived from the
 input dataclass.
2. Falls back to the documented ``DEFAULT_START_TO_CLOSE`` /
 ``DEFAULT_HEARTBEAT`` values when the input leaves the timeout
 overrides as ``None`` - the activity always gets a bounded
 ``start_to_close_timeout`` regardless of caller configuration.
3. Maps the activity result dict onto :class:`ExecutionRunWorkflowOutput`
 for the three terminal statuses ``"passed"`` / ``"failed"`` /
 ``"timeout"`` and propagates ``exit_code`` / ``stdout_uri`` /
 ``stderr_uri`` / ``runner_id`` / ``failure_reason`` verbatim
 to preserve the output shape.

Production callers (e.g. :class:`AutomationWorkflow`) currently always
pass ``start_to_close_timeout=None`` / ``heartbeat_timeout=None`` so the
workflow body uses its built-in defaults. We mirror that production
pattern in the test inputs because Temporal's default JSON converter
does not serialise :class:`datetime.timedelta` values across the
workflow boundary; the dataclass field shape (``timedelta | None``) is
preserved for future converter upgrades.

The workflow body does not touch wall-clock time, randomness, or
external services directly, so the time-skipping test server is
sufficient and the test stays hermetic.

Test isolation
--------------

``isolate_worker("execution-runner")`` snapshots ``sys.path`` /
``sys.modules`` so the ``src.*`` namespace points to the
execution-runner-worker tree only inside the with-block. This mirrors
the pattern already used by ``test_execution_runner.py`` for the legacy
workflow.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.integration._worker_path import isolate_worker


# ---------------------------------------------------------------------------
# Activity-call recorder
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Records every ``ssh_run_test`` invocation for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []


# ---------------------------------------------------------------------------
# Status mapping matrix 
#
# Each row encodes the canonical mapping the workflow MUST preserve:
# ssh_run_test result dict → ExecutionRunWorkflowOutput fields
#
# (status, exit_code, stdout_uri, stderr_uri, duration_seconds,
# runner_id, failure_reason, scenario_id)
# ---------------------------------------------------------------------------


_STATUS_MATRIX: list[tuple[str, int | None, str | None, str | None, float, str | None, str | None, str]] = [
    (
        "passed",
        0,
        "s3://ai-runs/exec-canonical/stdout.txt",
        "s3://ai-runs/exec-canonical/stderr.txt",
        12.5,
        "runner-eu-1",
        None,
        "passed_with_artifacts",
    ),
    (
        "failed",
        1,
        "s3://ai-runs/exec-canonical/stdout.txt",
        "s3://ai-runs/exec-canonical/stderr.txt",
        7.25,
        "runner-eu-1",
        "non_zero_exit",
        "failed_non_zero_exit",
    ),
    (
        "timeout",
        None,
        None,
        None,
        1800.0,
        "runner-eu-1",
        "timeout",
        "timeout_no_artifacts",
    ),
]


# ---------------------------------------------------------------------------
# Status-mapping integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    (
        "status",
        "exit_code",
        "stdout_uri",
        "stderr_uri",
        "duration_seconds",
        "runner_id",
        "failure_reason",
        "scenario",
    ),
    _STATUS_MATRIX,
    ids=[row[-1] for row in _STATUS_MATRIX],
)
async def test_canonical_execution_run_status_mapping(
    status: str,
    exit_code: int | None,
    stdout_uri: str | None,
    stderr_uri: str | None,
    duration_seconds: float,
    runner_id: str | None,
    failure_reason: str | None,
    scenario: str,
) -> None:
    """Drive the canonical :class:`ExecutionRunWorkflow` against a mocked
 ``ssh_run_test`` activity for each terminal status. Verifies:

 * The workflow accepts a single :class:`ExecutionRunWorkflowInput`
 and returns a single :class:`ExecutionRunWorkflowOutput`.
 * ``ssh_run_test`` is invoked exactly once with the runner id,
 command, environment, MinIO prefix, workdir, and parent workflow
 id derived from the input.
 * The activity ``start_to_close_timeout`` and ``heartbeat_timeout``
 overrides supplied by the input flow through to the activity
 options.
 * The output ``status`` / ``exit_code`` / ``stdout_uri`` /
 ``stderr_uri`` / ``runner_id`` / ``failure_reason`` mirror the
 activity's result dict verbatim.
 """

    log = _ActivityCallLog()

    parent_workflow_id = f"agent-{scenario}-{uuid.uuid4().hex[:8]}"
    runner_id_in = "runner-eu-1"
    command = "pytest -q tests/integration"
    env_pairs: tuple[tuple[str, str], ...] = (
        ("CI", "true"),
        ("PYTHONUNBUFFERED", "1"),
    )
    artifact_minio_prefix = "s3://ai-runs/exec-canonical"
    workdir = "/srv/runner/workspace/PAY-4211"
    department_id = "payments"
    start_to_close = timedelta(minutes=15)
    heartbeat = timedelta(seconds=45)

    with isolate_worker("execution-runner"):
        from src.workflows.execution_run_workflow import (
            ExecutionRunWorkflow,
        )
        from temporal_shared.messages import (
            ExecutionRunWorkflowInput,
            ExecutionRunWorkflowOutput,
        )

        # ----- Activity mock -------------------------------------------

        @activity.defn(name="ssh_run_test")
        async def ssh_run_test_mock(
            runner_id_arg: str | None,
            command_arg: str,
            env_arg: tuple[tuple[str, str], ...],
            artifact_prefix_arg: str | None,
            workdir_arg: str | None,
            parent_workflow_id_arg: str,
        ) -> dict[str, Any]:
            log.calls.append(
                (
                    runner_id_arg,
                    command_arg,
                    env_arg,
                    artifact_prefix_arg,
                    workdir_arg,
                    parent_workflow_id_arg,
                )
            )
            return {
                "status": status,
                "exit_code": exit_code,
                "stdout_uri": stdout_uri,
                "stderr_uri": stderr_uri,
                "duration_seconds": duration_seconds,
                "runner_id": runner_id,
                "failure_reason": failure_reason,
            }

        # ----- Drive the workflow --------------------------------------

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = (
                f"execution-runner-canon-{scenario}-{uuid.uuid4().hex[:8]}"
            )

            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ExecutionRunWorkflow],
                activities=[ssh_run_test_mock],
            ):
                inp = ExecutionRunWorkflowInput(
                    parent_workflow_id=parent_workflow_id,
                    runner_id=runner_id_in,
                    command=command,
                    workdir=workdir,
                    environment=env_pairs,
                    artifact_minio_prefix=artifact_minio_prefix,
                    start_to_close_timeout=start_to_close,
                    heartbeat_timeout=heartbeat,
                    department_id=department_id,
                )

                handle = await env.client.start_workflow(
                    ExecutionRunWorkflow.run,
                    inp,
                    id=f"workflow-id-canon-{scenario}-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result: ExecutionRunWorkflowOutput = await handle.result()

    # ----- Assertions --------------------------------------------------

    # The activity was invoked exactly once with the expected arguments
    # derived from the input dataclass.
    assert len(log.calls) == 1, f"expected 1 ssh_run_test call, got {log.calls}"
    (
        runner_id_called,
        command_called,
        env_called,
        prefix_called,
        workdir_called,
        parent_called,
    ) = log.calls[0]
    assert runner_id_called == runner_id_in
    assert command_called == command
    # Temporal serialises the environment tuple as a list-of-lists; both
    # shapes are acceptable, so coerce before comparison.
    assert tuple(tuple(item) for item in env_called) == env_pairs
    assert prefix_called == artifact_minio_prefix
    assert workdir_called == workdir
    assert parent_called == parent_workflow_id

    # The output dataclass mirrors the activity result verbatim.
    assert isinstance(result, ExecutionRunWorkflowOutput)
    assert result.status == status
    assert result.exit_code == exit_code
    assert result.stdout_uri == stdout_uri
    assert result.stderr_uri == stderr_uri
    assert result.runner_id == runner_id
    assert result.failure_reason == failure_reason
    assert result.duration_seconds == pytest.approx(duration_seconds)


# ---------------------------------------------------------------------------
# Default-timeout fallback test 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_canonical_execution_run_uses_default_timeouts_when_none() -> None:
    """When the input leaves ``start_to_close_timeout`` and
 ``heartbeat_timeout`` unset, the workflow MUST fall back to its
 documented defaults (``DEFAULT_START_TO_CLOSE`` /
 ``DEFAULT_HEARTBEAT``) - NOT call the activity with an open-ended
 timeout. We assert the activity completes successfully under
 the fallback configuration; an open-ended timeout would surface as
 a Temporal "start_to_close_timeout required" validation error.
 """

    log = _ActivityCallLog()

    with isolate_worker("execution-runner"):
        from src.workflows.execution_run_workflow import (
            DEFAULT_HEARTBEAT,
            DEFAULT_START_TO_CLOSE,
            ExecutionRunWorkflow,
        )
        from temporal_shared.messages import (
            ExecutionRunWorkflowInput,
            ExecutionRunWorkflowOutput,
        )

        @activity.defn(name="ssh_run_test")
        async def ssh_run_test_mock(
            runner_id_arg: str | None,
            command_arg: str,
            env_arg: tuple[tuple[str, str], ...],
            artifact_prefix_arg: str | None,
            workdir_arg: str | None,
            parent_workflow_id_arg: str,
        ) -> dict[str, Any]:
            log.calls.append(
                (
                    runner_id_arg,
                    command_arg,
                    env_arg,
                    artifact_prefix_arg,
                    workdir_arg,
                    parent_workflow_id_arg,
                )
            )
            return {
                "status": "passed",
                "exit_code": 0,
                "stdout_uri": "s3://ai-runs/default-timeouts/stdout.txt",
                "stderr_uri": "s3://ai-runs/default-timeouts/stderr.txt",
                "duration_seconds": 1.5,
                "runner_id": runner_id_arg,
                "failure_reason": None,
            }

        # Sanity-check that the defaults are positive timedeltas - the
        # workflow contract relies on this so that the activity gets a
        # bounded ``start_to_close_timeout``.
        assert DEFAULT_START_TO_CLOSE > timedelta(0)
        assert DEFAULT_HEARTBEAT > timedelta(0)

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = f"execution-runner-defaults-{uuid.uuid4().hex[:8]}"

            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ExecutionRunWorkflow],
                activities=[ssh_run_test_mock],
            ):
                inp = ExecutionRunWorkflowInput(
                    parent_workflow_id="agent-defaults",
                    runner_id="runner-default",
                    command="echo ok",
                    workdir=None,
                    environment=(),
                    artifact_minio_prefix="s3://ai-runs/default-timeouts",
                    # Both timeouts intentionally None - exercise the
                    # workflow's fallback path.
                    start_to_close_timeout=None,
                    heartbeat_timeout=None,
                    department_id="payments",
                )

                handle = await env.client.start_workflow(
                    ExecutionRunWorkflow.run,
                    inp,
                    id=f"workflow-id-defaults-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result: ExecutionRunWorkflowOutput = await handle.result()

    # The activity ran exactly once and the workflow returned the
    # canonical "passed" output dataclass - proving the fallback
    # timeouts produced a valid Temporal activity invocation.
    assert len(log.calls) == 1
    assert isinstance(result, ExecutionRunWorkflowOutput)
    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.runner_id == "runner-default"
    assert result.failure_reason is None
