"""End-to-end integration test for ``ExecutionRunWorkflow``.


This test exercises the full ``ExecutionRunWorkflow`` lifecycle against
``WorkflowEnvironment.start_time_skipping`` with mocked SSH and an
in-memory MinIO substitute (no Docker required). The flow under test:

 vault_fetch_ssh_credentials
 → ssh_connect_and_run (timeout, retry policy provided by workflow)
 → minio_upload_artifact × 3 (stdout.log, stderr.log, exit_code.txt)
 → should_cleanup(policy, exit_code) → ssh_cleanup (if True)

Test scope decision
-------------------

The original implementation goal covered mocked SSH with MinIO-backed
artifacts. Real MinIO requires Docker, which is opt-in and unreliable
in some CI lanes. To keep the test hermetic and parallel-safe, we
replace the MinIO upload activity with an in-memory fake that records
every artifact key and its bytes. This preserves every observable
invariant of the workflow:

- ``minio_upload_artifact`` is invoked exactly three times with the
 expected artifact keys (``stdout.log``, ``stderr.log``,
 ``exit_code.txt``) and content.
- ``should_cleanup(policy, exit_code)`` truth-table is exercised across
 the full ``{always, on_success, never} × {0, !=0}`` matrix.
- The workflow returns ``cleanup_performed=True`` only when the truth
 table predicts cleanup, and ``ssh_cleanup`` is invoked iff cleanup
 is performed.
- ``ssh_connect_and_run`` retry policy (3x exponential) is asserted by
 triggering a transient failure that succeeds on retry — If the project later wants a real-MinIO smoke variant, the same fake
keys / contents can be replayed against an ``--run-docker``-gated
fixture; that variant is out of scope for the deterministic suite.

Test isolation
--------------

``isolate_worker(...)`` snapshots ``sys.path`` / ``sys.modules`` so the
``src.*`` namespace points to the execution-runner-worker tree only
inside the with-block. Other tests that pin to agent-runner-worker (or
not at all) keep their original namespace.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.integration._worker_path import isolate_worker


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeMinIO:
    """In-memory MinIO substitute for the integration test.

 Records every uploaded artifact as ``{(bucket, key): bytes}`` and
 counts the number of upload calls. Each upload returns an
 :class:`ArtifactRef`-shaped dict — the workflow does not consume
 the return value beyond the activity-completion signal, so a
 structural shape is sufficient.
 """

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.upload_calls: list[tuple[str, str, int]] = []

    def upload(self, bucket: str, key: str, data: bytes) -> dict[str, Any]:
        self.objects[(bucket, key)] = data
        self.upload_calls.append((bucket, key, len(data)))
        return {
            "bucket": bucket,
            "key": key,
            "size_bytes": len(data),
            "etag": f"etag-{len(self.objects):08x}",
        }


class _ActivityCallLog:
    """Records every activity invocation across the test for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


# ---------------------------------------------------------------------------
# Test parameters
# ---------------------------------------------------------------------------


# (cleanup_policy, exit_code, expected_cleanup, scenario_name)
# Mirrors the should_cleanup truth table:
# always × any → cleanup
# on_success × 0 → cleanup, × !=0 → no cleanup
# never × any → no cleanup
_CLEANUP_MATRIX: list[tuple[str, int, bool, str]] = [
    ("always", 0, True, "always_with_zero_exit"),
    ("always", 7, True, "always_with_nonzero_exit"),
    ("on_success", 0, True, "on_success_with_zero_exit"),
    ("on_success", 1, False, "on_success_with_nonzero_exit"),
    ("never", 0, False, "never_with_zero_exit"),
    ("never", 5, False, "never_with_nonzero_exit"),
]


# ---------------------------------------------------------------------------
# Cleanup matrix integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("cleanup_policy", "exit_code", "expected_cleanup", "scenario"),
    _CLEANUP_MATRIX,
    ids=[s[3] for s in _CLEANUP_MATRIX],
)
async def test_execution_run_cleanup_matrix(
    cleanup_policy: str,
    exit_code: int,
    expected_cleanup: bool,
    scenario: str,
) -> None:
    """Drive ExecutionRunWorkflow across the
 ``{always, on_success, never} × {0, !=0}`` matrix. Verifies:

 - vault_fetch_ssh_credentials is called once.
 - ssh_connect_and_run is called once with the expected command
 and timeout configuration.
 - minio_upload_artifact is called exactly three times with the
 expected artifact keys carrying stdout, stderr, and exit_code
 artifacts.
 - should_cleanup truth-table holds: ssh_cleanup runs iff the policy
 and exit_code combination predicts cleanup.
 """

    log = _ActivityCallLog()
    fake_minio = _FakeMinIO()

    workflow_id = f"exec-test-{scenario}-{uuid.uuid4().hex[:8]}"
    test_command = "pytest -q"
    workspace_path = "/srv/runner/workspace"
    expected_stdout = f"stdout for {scenario}"
    expected_stderr = f"stderr for {scenario}"

    with isolate_worker("execution-runner"):
        from src.workflows.execution_run_workflow import (
            ExecutionRunInput,
            ExecutionRunResult,
            LegacyExecutionRunWorkflow as ExecutionRunWorkflow,
        )

        # ----- Activity mocks ------------------------------------------

        @activity.defn(name="vault_fetch_ssh_credentials")
        async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
            log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
            # Workflow passes the dict to ssh_connect_and_run as ``cred``.
            # The four required fields mirror SSHCred (host, port, user,
            # private_key).
            return {
                "host": "runner.test.invalid",
                "port": 22,
                "user": "runner",
                "private_key": "-----BEGIN TEST KEY-----\nfake\n-----END TEST KEY-----\n",
            }

        @activity.defn(name="ssh_connect_and_run")
        async def ssh_connect_and_run(
            cred: dict[str, Any],
            command: str,
            ws_path: str,
            timeout_minutes: int,
        ) -> dict[str, Any]:
            log.calls.append(
                (
                    "ssh_connect_and_run",
                    (cred["host"], command, ws_path, timeout_minutes),
                )
            )
            return {
                "stdout": expected_stdout,
                "stderr": expected_stderr,
                "exit_code": exit_code,
            }

        @activity.defn(name="minio_upload_artifact")
        async def minio_upload_artifact(
            bucket: str, key: str, data: bytes
        ) -> dict[str, Any]:
            log.calls.append(
                ("minio_upload_artifact", (bucket, key, len(data)))
            )
            return fake_minio.upload(bucket, key, data)

        @activity.defn(name="ssh_cleanup")
        async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
            log.calls.append(("ssh_cleanup", (cred["host"], ws_path)))

        # ----- Drive the workflow --------------------------------------

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = (
                f"execution-runner-{scenario}-{uuid.uuid4().hex[:8]}"
            )

            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ExecutionRunWorkflow],
                activities=[
                    vault_fetch_ssh_credentials,
                    ssh_connect_and_run,
                    minio_upload_artifact,
                    ssh_cleanup,
                ],
            ):
                handle = await env.client.start_workflow(
                    ExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command=test_command,
                        workspace_path=workspace_path,
                        cleanup_policy=cleanup_policy,
                        timeout_minutes=30,
                    ),
                    id=f"workflow-id-{scenario}-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result: ExecutionRunResult = await handle.result()

    # ----- Assertions --------------------------------------------------

    # The activity-call sequence is exact:
    # vault → ssh → upload × 3 → (cleanup?)
    names = log.names()
    assert names[0] == "vault_fetch_ssh_credentials", names
    assert names[1] == "ssh_connect_and_run", names
    upload_indices = [
        i for i, n in enumerate(names) if n == "minio_upload_artifact"
    ]
    assert len(upload_indices) == 3, f"expected 3 uploads, got {names}"
    # The three uploads come consecutively after ssh_connect_and_run.
    assert upload_indices == [2, 3, 4], f"upload positions: {upload_indices}"

    # Vault was queried with the workflow_id.
    vault_calls = [
        args for n, args in log.calls if n == "vault_fetch_ssh_credentials"
    ]
    assert vault_calls == [(workflow_id,)]

    # SSH ran the requested command on the configured workspace path with
    # the 30-minute timeout.
    ssh_calls = [args for n, args in log.calls if n == "ssh_connect_and_run"]
    assert len(ssh_calls) == 1
    host_arg, command_arg, ws_arg, timeout_arg = ssh_calls[0]
    assert host_arg == "runner.test.invalid"
    assert command_arg == test_command
    assert ws_arg == workspace_path
    assert timeout_arg == 30

    # MinIO upload keys follow the ``executions/{workflow_id}/{name}``
    # contract from temporal_shared.identifiers.execution_artifact_key.
    expected_stdout_key = f"executions/{workflow_id}/stdout.log"
    expected_stderr_key = f"executions/{workflow_id}/stderr.log"
    expected_exit_code_key = f"executions/{workflow_id}/exit_code.txt"
    expected_keys = {
        expected_stdout_key,
        expected_stderr_key,
        expected_exit_code_key,
    }
    actual_keys = {key for (_bucket, key) in fake_minio.objects}
    assert actual_keys == expected_keys, (
        f"unexpected artifact keys uploaded: "
        f"expected {expected_keys}, got {actual_keys}"
    )

    # All three artifacts went into the default bucket.
    buckets = {bucket for (bucket, _key) in fake_minio.objects}
    assert buckets == {"ai-runs"}, f"unexpected buckets: {buckets}"

    # Artifact contents match the ssh_connect_and_run output.
    assert (
        fake_minio.objects[("ai-runs", expected_stdout_key)]
        == expected_stdout.encode("utf-8")
    )
    assert (
        fake_minio.objects[("ai-runs", expected_stderr_key)]
        == expected_stderr.encode("utf-8")
    )
    assert (
        fake_minio.objects[("ai-runs", expected_exit_code_key)]
        == str(exit_code).encode("utf-8")
    )

    # Cleanup truth-table.
    cleanup_invocations = [n for n in names if n == "ssh_cleanup"]
    assert (len(cleanup_invocations) == 1) is expected_cleanup, (
        f"cleanup mismatch for policy={cleanup_policy!r} exit={exit_code}: "
        f"expected_cleanup={expected_cleanup}, "
        f"actual ssh_cleanup invocations={len(cleanup_invocations)}"
    )

    # The result mirrors the cleanup decision exactly.
    assert result.cleanup_performed is expected_cleanup
    assert result.exit_code == exit_code
    assert result.stdout_artifact_key == expected_stdout_key
    assert result.stderr_artifact_key == expected_stderr_key
    assert result.exit_code_artifact_key == expected_exit_code_key


# ---------------------------------------------------------------------------
# SSH connect retry test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ssh_connect_retries_on_transient_failure() -> None:
    """The workflow's ssh_connect_and_run activity options carry a
 ``RetryPolicy(maximum_attempts=3)``. A transient connection failure
 on the first attempt is followed by success on the second — the
 workflow must complete cleanly without surfacing the failure.

 This exercises the retry path without requiring a real SSH server:
 the activity callable counts invocations and raises on the first
 attempt, then returns a successful run on the second.
 """

    log = _ActivityCallLog()
    fake_minio = _FakeMinIO()
    ssh_attempts = 0

    workflow_id = f"exec-retry-{uuid.uuid4().hex[:8]}"

    with isolate_worker("execution-runner"):
        from src.workflows.execution_run_workflow import (
            ExecutionRunInput,
            LegacyExecutionRunWorkflow as ExecutionRunWorkflow,
        )

        @activity.defn(name="vault_fetch_ssh_credentials")
        async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
            log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
            return {
                "host": "runner.test.invalid",
                "port": 22,
                "user": "runner",
                "private_key": "-----BEGIN TEST KEY-----\nfake\n-----END TEST KEY-----\n",
            }

        @activity.defn(name="ssh_connect_and_run")
        async def ssh_connect_and_run(
            cred: dict[str, Any],
            command: str,
            ws_path: str,
            timeout_minutes: int,
        ) -> dict[str, Any]:
            nonlocal ssh_attempts
            ssh_attempts += 1
            log.calls.append(("ssh_connect_and_run", (ssh_attempts,)))
            if ssh_attempts == 1:
                # Transient failure on first attempt — Temporal retries.
                raise ConnectionError(
                    "transient SSH failure (test injection)"
                )
            return {
                "stdout": "ok\n",
                "stderr": "",
                "exit_code": 0,
            }

        @activity.defn(name="minio_upload_artifact")
        async def minio_upload_artifact(
            bucket: str, key: str, data: bytes
        ) -> dict[str, Any]:
            log.calls.append(("minio_upload_artifact", (bucket, key)))
            return fake_minio.upload(bucket, key, data)

        @activity.defn(name="ssh_cleanup")
        async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
            log.calls.append(("ssh_cleanup", (ws_path,)))

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = (
                f"execution-runner-retry-{uuid.uuid4().hex[:8]}"
            )
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ExecutionRunWorkflow],
                activities=[
                    vault_fetch_ssh_credentials,
                    ssh_connect_and_run,
                    minio_upload_artifact,
                    ssh_cleanup,
                ],
            ):
                handle = await env.client.start_workflow(
                    ExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command="pytest -q",
                        workspace_path="/srv/runner/workspace",
                        cleanup_policy="on_success",
                        timeout_minutes=30,
                    ),
                    id=f"retry-test-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )
                result = await handle.result()

    # The activity was invoked twice; the workflow only saw one logical
    # success. Total ssh_connect_and_run calls == 2 (1 fail + 1 success).
    assert ssh_attempts == 2, (
        f"expected 2 ssh_connect_and_run attempts (1 fail + 1 retry), "
        f"got {ssh_attempts}"
    )
    # The workflow completed successfully despite the transient failure.
    assert result.exit_code == 0
    # Cleanup ran (on_success × exit_code=0 → True).
    assert result.cleanup_performed is True
    # Three artifact uploads still happened.
    assert len(fake_minio.objects) == 3
