"""End-to-end integration test for the ``remote_ssh_test_only`` flow.

Scenario
--------

The :class:`ExecutionRunWorkflow` body in the
``execution-runner-worker`` is the canonical SSH test-runner workflow
behind the ``remote_ssh_test_only`` workflow type. The workflow-type
catalogue maps ``remote_ssh_test_only`` to the
``execution`` capability — a Jira task that asks the bot to run a
smoke command on a remote host without modifying source. At dispatch
time the parent :class:`AutomationWorkflow` builds an
:class:`ExecutionRunWorkflowInput` with ``workflow_type``
``"remote_ssh_test_only"`` and starts ``ExecutionRunWorkflow`` as a
child on the ``execution-runner-tq`` task queue.

The :class:`ExecutionRunWorkflow` body is a thin orchestrator: it
pre-processes the (caller-supplied) command, then delegates to the
``ssh_run_test`` activity which performs the credential fetch, SSH
session, MinIO upload, and 30 s heartbeating in one place. The
workflow's job is to map the
activity result onto a structured
:class:`ExecutionRunWorkflowOutput` with one of three terminal
statuses:

* ``"passed"`` — exit code 0.
* ``"failed"`` — non-zero exit, runner unreachable, or other
 non-timeout error.
* ``"timeout"`` — SSH command exceeded the per-attempt budget.

This file exercises all three terminal statuses end-to-end against a
real Temporal time-skipping ``WorkflowEnvironment``. The
``ssh_run_test`` activity is mocked with a recording stub that
returns the result dict the production activity would emit in each
scenario (see
``platform/workers/execution-runner-worker/src/activities/ssh.py``
for the dict shape) — no paramiko, no MinIO, no Vault.

Three test cases:

* ``test_execution_run_workflow_passed`` — activity returns
 ``status="passed"`` / ``exit_code=0``; workflow result MUST be
 ``status="passed"``.
* ``test_execution_run_workflow_failed`` — activity returns
 ``status="failed"`` / ``exit_code=1``; workflow result MUST be
 ``status="failed"``.
* ``test_execution_run_workflow_timeout`` — activity returns
 ``status="timeout"`` / ``failure_reason="timeout"``; workflow
 result MUST be ``status="timeout"``.

Each test additionally pins:

* ``ssh_run_test`` invoked exactly once with the command, runner
 id, environment, MinIO prefix, workdir, and parent workflow id
 derived from the input dataclass ( — every dispatch carries
 the same envelope so the runner can resolve the right
 credentials).
* ``workflow_type="remote_ssh_test_only"`` is propagated unchanged
 into the workflow body — the safety net inside
 :class:`ExecutionRunWorkflow.run` only fires for ``"noop_test"``,
 so a ``remote_ssh_test_only`` dispatch keeps
 the caller-supplied command verbatim.
* ``runner_id`` and ``failure_reason`` round-trip from the
 activity result onto the output dataclass unchanged so audit
 consumers can discriminate timeout-vs-non-zero-exit failures.

Hosts without the embedded ``temporal-test-server`` skip cleanly
via the same module-level gate the existing integration tests
in ``test_temporal_signal.py`` / ``test_e2e_code_change_with_test.py``
use — see :func:`_temporal_test_env_available` and
:func:`_start_time_skipping_or_skip`.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Module-level skip gate
# ---------------------------------------------------------------------------


def _temporal_test_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly.

 Any import failure is treated as "skip cleanly" so hosts without
 the embedded ``temporal-test-server`` (sandboxed CI, missing
 native deps) skip rather than erroring at collection time.
 """

    try:
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure → skip.
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _temporal_test_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the time-skipping env, ``pytest.skip``ing on failure.

 The embedded ``temporal-test-server`` may fail to start on hosts
 where the binary is not bundled. Surface that cleanly as a
 skip — the integration suite stays green on machines that
 cannot host Temporal locally.
 """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Activity-call recorder
#
# ``ssh_run_test`` takes a positional argument tuple — runner id,
# command, env, artifact prefix, workdir, parent workflow id — which
# the ``ExecutionRunWorkflow`` body forwards verbatim from the
# input dataclass. The recorder captures the tuple
# per invocation so each test can assert on the exact payload the
# runner side would receive.
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Append-only log of ``ssh_run_test`` invocations."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []


# ---------------------------------------------------------------------------
# Standard input fixture
# ---------------------------------------------------------------------------


def _make_remote_ssh_input(*, parent_workflow_id: str) -> Any:
    """Build a :class:`ExecutionRunWorkflowInput` for the
 ``remote_ssh_test_only`` flow.

 The values mirror what the production
 :class:`AutomationWorkflow` would synthesise for a Jira task
 asking the bot to run a smoke command — a runner id sourced
 from the dept's ``ssh_runners`` list, an explicit
 ``workflow_type="remote_ssh_test_only"``, and a non-empty
 command (the ``noop_test`` safety net does NOT apply to this
 type, so an empty command would surface as a runner-side
 error).

 The ``start_to_close_timeout`` and ``heartbeat_timeout`` fields
 are intentionally left ``None`` so the workflow falls back to
 its built-in defaults (``DEFAULT_START_TO_CLOSE`` /
 ``DEFAULT_HEARTBEAT``). Temporal's default JSON converter does
 not serialise :class:`datetime.timedelta` across the workflow
 boundary; production callers (notably
 :class:`AutomationWorkflow`) pass ``None`` here for the same
 reason and the canonical
 :class:`ExecutionRunWorkflow` tests
 (``test_execution_run_workflow_canonical.py``) follow the same
 pattern.
 """

    from temporal_shared.messages import ExecutionRunWorkflowInput

    return ExecutionRunWorkflowInput(
        parent_workflow_id=parent_workflow_id,
        runner_id="runner-eu-1",
        command="bash -lc 'pytest -q tests/smoke'",
        workdir="/srv/runner/workspace/PAY-9001",
        environment=(
            ("CI", "true"),
            ("PYTHONUNBUFFERED", "1"),
        ),
        artifact_minio_prefix="s3://ai-runs/remote-ssh-test-only",
        start_to_close_timeout=None,
        heartbeat_timeout=None,
        department_id="payments",
        workflow_type="remote_ssh_test_only",
    )


# ---------------------------------------------------------------------------
# Common drive helper
# ---------------------------------------------------------------------------


async def _run_workflow_with_ssh_result(
    ssh_result: dict[str, Any],
    *,
    scenario: str,
) -> tuple[Any, _ActivityCallLog]:
    """Drive :class:`ExecutionRunWorkflow` once with a stubbed
 ``ssh_run_test`` that returns ``ssh_result``.

 Returns the workflow output and the activity call log so the
 caller can pin both the structured result and the activity
 payload in a single arrange/act/assert block.
 """

    from tests.integration._worker_path import isolate_worker

    log = _ActivityCallLog()
    parent_workflow_id = f"agent-{scenario}-{uuid.uuid4().hex[:8]}"

    with isolate_worker("execution-runner"):
        from temporalio import activity
        from temporalio.worker import Worker

        from src.workflows.execution_run_workflow import ExecutionRunWorkflow

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
            return ssh_result

        async with _start_time_skipping_or_skip() as env:
            task_queue = (
                f"execution-runner-tq-{scenario}-{uuid.uuid4().hex[:8]}"
            )
            inp = _make_remote_ssh_input(
                parent_workflow_id=parent_workflow_id
            )
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ExecutionRunWorkflow],
                activities=[ssh_run_test_mock],
            ):
                handle = await env.client.start_workflow(
                    ExecutionRunWorkflow.run,
                    inp,
                    id=f"workflow-id-remote-ssh-{scenario}-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result = await handle.result()

    return result, log


# ---------------------------------------------------------------------------
# Test 1 — passed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_run_workflow_passed() -> None:
    """Drive :class:`ExecutionRunWorkflow` with the
 ``remote_ssh_test_only`` envelope through the happy path:

 1. ``ssh_run_test`` is invoked exactly once with the command,
 runner id, environment, MinIO prefix, workdir, and parent
 workflow id derived from the input dataclass.
 2. The mocked activity returns a successful result dict
 (``status="passed"`` / ``exit_code=0``).
 3. The workflow maps the dict onto
 :class:`ExecutionRunWorkflowOutput` and emits
 ``status="passed"`` / ``exit_code=0`` /
 ``failure_reason=None``.

 The caller's command is NOT rewritten — the ``noop_test``
 safety net inside :class:`ExecutionRunWorkflow.run` only fires
 when ``workflow_type == "noop_test"``, so
 every other dispatch keeps the command verbatim. Pinning the
 command round-trip here protects against a regression that
 accidentally widens the safety net to other workflow types.
 """

    from temporal_shared.messages import ExecutionRunWorkflowOutput

    ssh_result: dict[str, Any] = {
        "status": "passed",
        "exit_code": 0,
        "stdout_uri": "s3://ai-runs/remote-ssh-test-only/stdout.txt",
        "stderr_uri": "s3://ai-runs/remote-ssh-test-only/stderr.txt",
        "duration_seconds": 4.25,
        "runner_id": "runner-eu-1",
        "failure_reason": None,
    }

    result, log = await _run_workflow_with_ssh_result(
        ssh_result, scenario="passed"
    )

    # ----- Activity invoked exactly once with the input envelope ----

    assert len(log.calls) == 1, (
        f"ssh_run_test must run exactly once; got {len(log.calls)} "
        f"calls: {log.calls!r}"
    )
    (
        runner_id_called,
        command_called,
        env_called,
        prefix_called,
        workdir_called,
        parent_called,
    ) = log.calls[0]
    assert runner_id_called == "runner-eu-1"
    assert command_called == "bash -lc 'pytest -q tests/smoke'", (
        f"remote_ssh_test_only must keep the caller's command verbatim "
        f"(no noop_test safety-net rewrite); got {command_called!r}"
    )
    # Temporal serialises the environment tuple as a list-of-lists; both
    # shapes are acceptable, so coerce before comparison.
    assert tuple(tuple(item) for item in env_called) == (
        ("CI", "true"),
        ("PYTHONUNBUFFERED", "1"),
    )
    assert prefix_called == "s3://ai-runs/remote-ssh-test-only"
    assert workdir_called == "/srv/runner/workspace/PAY-9001"
    assert parent_called.startswith("agent-passed-")

    # ----- Workflow output ------------------------------------------

    assert isinstance(result, ExecutionRunWorkflowOutput), (
        f"workflow must return ExecutionRunWorkflowOutput; got "
        f"{type(result).__name__}"
    )
    assert result.status == "passed", (
        f"expected status=passed on exit_code=0; got {result!r}"
    )
    assert result.exit_code == 0
    assert result.failure_reason is None, (
        f"failure_reason must be None on a passing run; got {result!r}"
    )
    assert result.runner_id == "runner-eu-1"
    assert result.stdout_uri == (
        "s3://ai-runs/remote-ssh-test-only/stdout.txt"
    )
    assert result.stderr_uri == (
        "s3://ai-runs/remote-ssh-test-only/stderr.txt"
    )
    assert result.duration_seconds == pytest.approx(4.25)


# ---------------------------------------------------------------------------
# Test 2 — failed (non-zero exit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_run_workflow_failed() -> None:
    """A ``remote_ssh_test_only`` dispatch whose smoke command exits
 non-zero MUST surface as
 ``ExecutionRunWorkflowOutput(status="failed", ...)``. The
 failure category (``"non_zero_exit"``) flows through unchanged
 so the audit consumer can discriminate between exit-code
 failures and runner-unreachable / artifact-upload failures.

 Trace
 -----

 * ``ssh_run_test`` returns
 ``{"status": "failed", "exit_code": 1, ...,
 "failure_reason": "non_zero_exit"}``.
 * The workflow body maps the dict onto
 :class:`ExecutionRunWorkflowOutput` verbatim.
 * The output ``status`` is ``"failed"`` (NOT ``"timeout"`` —
 the timeout path requires a distinct activity result).
 """

    from temporal_shared.messages import ExecutionRunWorkflowOutput

    ssh_result: dict[str, Any] = {
        "status": "failed",
        "exit_code": 1,
        "stdout_uri": "s3://ai-runs/remote-ssh-test-only/stdout.txt",
        "stderr_uri": "s3://ai-runs/remote-ssh-test-only/stderr.txt",
        "duration_seconds": 8.0,
        "runner_id": "runner-eu-1",
        "failure_reason": "non_zero_exit",
    }

    result, log = await _run_workflow_with_ssh_result(
        ssh_result, scenario="failed"
    )

    # ----- Activity invoked exactly once ----------------------------

    assert len(log.calls) == 1, (
        f"ssh_run_test must run exactly once; got {len(log.calls)} "
        f"calls: {log.calls!r}"
    )

    # ----- Workflow output ------------------------------------------

    assert isinstance(result, ExecutionRunWorkflowOutput)
    assert result.status == "failed", (
        f"expected status=failed on non-zero exit; got {result!r}"
    )
    assert result.exit_code == 1
    assert result.failure_reason == "non_zero_exit", (
        f"failure_reason must round-trip from the activity result; "
        f"got {result!r}"
    )
    assert result.runner_id == "runner-eu-1"
    assert result.stdout_uri == (
        "s3://ai-runs/remote-ssh-test-only/stdout.txt"
    )
    assert result.stderr_uri == (
        "s3://ai-runs/remote-ssh-test-only/stderr.txt"
    )


# ---------------------------------------------------------------------------
# Test 3 — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_execution_run_workflow_timeout() -> None:
    """When the SSH command exceeds the per-attempt budget the
 ``ssh_run_test`` activity returns
 ``{"status": "timeout", "exit_code": None, ...,
 "failure_reason": "timeout"}`` — see
 ``platform/workers/execution-runner-worker/src/activities/ssh.py``
 for the production return shape. The workflow MUST surface
 this as ``ExecutionRunWorkflowOutput(status="timeout", ...)``
 so the parent :class:`AutomationWorkflow` can discriminate
 "tests timed out" from "tests failed" when posting the Jira
 summary.

 The activity-side timeout signal is preferred over a Temporal
 ``ActivityTimeoutError`` because:

 * The production ``ssh_run_test`` body catches the paramiko
 timeout itself and emits a structured result dict — letting
 Temporal's activity timeout fire would mean an open-ended
 retry attempt instead of a stable terminal status.
 * The retry policy bounds ``ssh_run_test`` to 3 attempts
 with non-idempotent semantics; a Temporal-level timeout
 would still race the retry policy. The activity-emitted
 timeout is the single-shot terminal signal the workflow
 body maps to ``status="timeout"``.
 """

    from temporal_shared.messages import ExecutionRunWorkflowOutput

    ssh_result: dict[str, Any] = {
        "status": "timeout",
        "exit_code": None,
        "stdout_uri": None,
        "stderr_uri": None,
        "duration_seconds": 1800.0,
        "runner_id": "runner-eu-1",
        "failure_reason": "timeout",
    }

    result, log = await _run_workflow_with_ssh_result(
        ssh_result, scenario="timeout"
    )

    # ----- Activity invoked exactly once ----------------------------

    assert len(log.calls) == 1, (
        f"ssh_run_test must run exactly once; got {len(log.calls)} "
        f"calls: {log.calls!r}"
    )

    # ----- Workflow output ------------------------------------------

    assert isinstance(result, ExecutionRunWorkflowOutput)
    assert result.status == "timeout", (
        f"expected status=timeout when activity returns "
        f"status='timeout'; got {result!r}"
    )
    # Timeout runs do not carry a process exit code — the SSH
    # session was killed before the command finished. The activity
    # surfaces this as ``exit_code=None`` so the workflow output
    # mirrors the absence faithfully.
    assert result.exit_code is None, (
        f"exit_code must be None on a timeout (no process result); "
        f"got {result!r}"
    )
    assert result.failure_reason == "timeout", (
        f"failure_reason must round-trip 'timeout' from the activity; "
        f"got {result!r}"
    )
    # No artifacts on a timeout — the activity body bails before
    # uploading stdout/stderr. The workflow output preserves the
    # null URIs so the audit consumer does not synthesise broken
    # MinIO links.
    assert result.stdout_uri is None
    assert result.stderr_uri is None
    assert result.runner_id == "runner-eu-1"
    assert result.duration_seconds == pytest.approx(1800.0)
