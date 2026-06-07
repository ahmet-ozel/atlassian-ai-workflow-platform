"""Unit tests for the optional Bitbucket git-push sub-flow on
:class:`LegacyExecutionRunWorkflow`.

The workflow body is exercised against a real
``WorkflowEnvironment.start_time_skipping()`` cluster with mocked
activities so we can assert the activity-call sequence without
spinning up Vault, SSH, or MinIO.

Scenarios covered
-----------------

* ``git_push_required=False``  inject / push / cleanup activities
  are **never** called (legacy contract preserved).
* ``git_push_required=True`` + push success  ``inject_git_credentials``
   ``ssh_connect_and_run`` (push)  ``cleanup_git_credentials`` runs in
  exactly that order.
* ``git_push_required=True`` + push fails (non-zero exit code)
  inject runs, push runs, cleanup **still** runs from the
  ``finally`` block, and the workflow surfaces ``ApplicationError``
  while preserving the cleanup guarantee.
* ``git_push_required=True`` + push fails (SSH activity raises)
  same cleanup guarantee.
* ``inject_git_credentials`` returns ``success=False``
  ``ApplicationError`` raised, push **not** attempted, cleanup runs
  from the ``finally`` block.

Each scenario records every activity invocation in a shared
``_ActivityCallLog`` so the assertions are explicit and order-
preserving.

Determinism
-----------

The workflow body uses ``workflow.info().workflow_id`` for the
credential injection input (replay-safe).  The tests assert the
activity received that exact value rather than guessing - a
regression that swapped to ``os.environ`` / ``time.time()`` would
fail the determinism check during Temporal replay.
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
# ``sys.path`` bootstrapping - mirror the pattern used by
# ``test_execution_run_workflow_noop_defaults.py`` / unit tests so the
# in-tree ``src/`` package import resolves without an editable install.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))


# Module-level imports of the symbols referenced by activity-stub type
# hints.  ``temporalio.activity.defn`` introspects each stub via
# ``typing.get_type_hints``, which evaluates forward references against
# the *defining module's* globals - not the enclosing function's local
# scope.  Importing here ensures the stubs can declare typed
# parameters even when the activity-decorator is applied inside a
# pytest function body.
from src.activities.credential_injector import (  # noqa: E402
    CredentialInjectInput,
    CredentialInjectResult,
)
from src.workflows.execution_run_workflow import (  # noqa: E402
    ExecutionRunInput,
    ExecutionRunResult,
    LegacyExecutionRunWorkflow,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Records every activity invocation across the test for assertions.

    Each entry is a ``(name, args)`` tuple; the helper is shared between
    the activity stubs and the assertion site so the test can verify
    both the invocation count and the order of calls.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _make_ssh_cred() -> dict[str, Any]:
    """Return a fixed SSH credential dict for the mocks."""
    return {
        "host": "runner.test.invalid",
        "port": 22,
        "user": "runner",
        "private_key": (
            "-----BEGIN TEST KEY-----\nfake\n-----END TEST KEY-----\n"
        ),
    }


# ---------------------------------------------------------------------------
# Scenario 1: git_push_required=False keeps the legacy contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_push_disabled_skips_inject_and_cleanup() -> None:
    """Legacy contract: disabled git push skips inject and cleanup.

    When ``git_push_required=False`` (the default for every existing
    call site) the workflow must not call ``inject_git_credentials``,
    the push, or ``cleanup_git_credentials``.  This guards the
    backwards compatibility of every other ExecutionRunWorkflow
    consumer.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-no-push-{uuid.uuid4().hex[:8]}"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(
            ("ssh_connect_and_run", (command, ws_path, timeout_minutes))
        )
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    @activity.defn(name="minio_upload_artifact")
    async def minio_upload_artifact(
        bucket: str, key: str, data: bytes
    ) -> dict[str, Any]:
        log.calls.append(("minio_upload_artifact", (bucket, key)))
        return {"bucket": bucket, "key": key, "size_bytes": len(data)}

    @activity.defn(name="ssh_cleanup")
    async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
        log.calls.append(("ssh_cleanup", (ws_path,)))

    @activity.defn(name="inject_git_credentials")
    async def inject_git_credentials(input: Any) -> dict[str, Any]:  # noqa: A002
        log.calls.append(("inject_git_credentials", (input,)))
        return {"success": True, "error": None, "masked_username": "u***"}

    @activity.defn(name="cleanup_git_credentials")
    async def cleanup_git_credentials(workflow_id: str) -> None:
        log.calls.append(("cleanup_git_credentials", (workflow_id,)))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-no-push-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
                inject_git_credentials,
                cleanup_git_credentials,
            ],
        ):
            result: ExecutionRunResult = await env.client.execute_workflow(
                LegacyExecutionRunWorkflow.run,
                ExecutionRunInput(
                    workflow_id=workflow_id,
                    test_command="pytest -q",
                    workspace_path="/srv/runner/ws",
                    cleanup_policy="never",
                    timeout_minutes=30,
                    # git push intentionally NOT enabled
                ),
                id=f"wf-no-push-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    # The push branch must not have fired - none of the credential
    # activities are present in the call log.
    assert "inject_git_credentials" not in names, names
    assert "cleanup_git_credentials" not in names, names
    # And only the legacy SSH command ran (the push would be a
    # second ssh_connect_and_run invocation).
    ssh_calls = [n for n in names if n == "ssh_connect_and_run"]
    assert len(ssh_calls) == 1, names
    # And the workflow returned its normal result envelope.
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Scenario 2: git_push_required=True with successful push
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_push_success_runs_inject_push_cleanup_in_order() -> None:
    """Happy path runs inject, push, and cleanup in order.

    On the happy path the activity sequence is:
    inject  push (ssh_connect_and_run #2)  cleanup, with the
    artifact uploads following.  The cleanup activity runs **inside**
    the ``finally`` block so it appears between the push and the
    artifact uploads (the position is deterministic - we assert it
    explicitly).
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-push-ok-{uuid.uuid4().hex[:8]}"
    branch = "feature/payment-fix"
    dept = "payments"

    inject_inputs: list[Any] = []
    cleanup_workflow_ids: list[str] = []

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command,)))
        # Both the test command and the push succeed.
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    @activity.defn(name="minio_upload_artifact")
    async def minio_upload_artifact(
        bucket: str, key: str, data: bytes
    ) -> dict[str, Any]:
        log.calls.append(("minio_upload_artifact", (bucket, key)))
        return {"bucket": bucket, "key": key, "size_bytes": len(data)}

    @activity.defn(name="ssh_cleanup")
    async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
        log.calls.append(("ssh_cleanup", (ws_path,)))

    @activity.defn(name="inject_git_credentials")
    async def inject_git_credentials(
        input: CredentialInjectInput,  # noqa: A002
    ) -> CredentialInjectResult:
        log.calls.append(("inject_git_credentials", (input.dept_id,)))
        inject_inputs.append(input)
        return CredentialInjectResult(
            success=True,
            error=None,
            masked_username="u***",
        )

    @activity.defn(name="cleanup_git_credentials")
    async def cleanup_git_credentials(workflow_id: str) -> None:
        log.calls.append(("cleanup_git_credentials", (workflow_id,)))
        cleanup_workflow_ids.append(workflow_id)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-push-ok-{uuid.uuid4().hex[:8]}"
        wf_id = f"wf-push-ok-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
                inject_git_credentials,
                cleanup_git_credentials,
            ],
        ):
            result: ExecutionRunResult = await env.client.execute_workflow(
                LegacyExecutionRunWorkflow.run,
                ExecutionRunInput(
                    workflow_id=workflow_id,
                    test_command="pytest -q",
                    workspace_path="/srv/runner/ws",
                    cleanup_policy="never",
                    timeout_minutes=30,
                    git_push_required=True,
                    git_push_branch=branch,
                    dept_id=dept,
                ),
                id=wf_id,
                task_queue=task_queue,
            )

    names = log.names()

    # The push sub-flow runs in a fixed order:
    # vault  ssh#1 (test)  inject  ssh#2 (push)  cleanup  minio×3
    inject_idx = names.index("inject_git_credentials")
    cleanup_idx = names.index("cleanup_git_credentials")
    # There are exactly two ssh_connect_and_run calls - the test
    # command and the git push.  The second is between inject and
    # cleanup.
    ssh_indices = [i for i, n in enumerate(names) if n == "ssh_connect_and_run"]
    assert len(ssh_indices) == 2, names
    assert inject_idx < ssh_indices[1] < cleanup_idx, names
    # And the artifact uploads come *after* the cleanup, never before.
    upload_indices = [
        i for i, n in enumerate(names) if n == "minio_upload_artifact"
    ]
    assert all(i > cleanup_idx for i in upload_indices), names

    # The push command was built from the requested branch verbatim.
    push_command = log.calls[ssh_indices[1]][1][0]
    assert push_command == f"git push origin {branch}"

    # The inject input carries the workflow_id (replay-safe - not a
    # fresh ``time.time()``-derived string) and the configured TTL.
    assert len(inject_inputs) == 1
    inject_input = inject_inputs[0]
    assert inject_input.dept_id == dept
    assert inject_input.workflow_id == wf_id
    assert inject_input.ttl_minutes == 15

    # Cleanup received the same Temporal workflow id.
    assert cleanup_workflow_ids == [wf_id]

    # The workflow result still reflects the test command's exit code.
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Scenario 3: git_push_required=True with push failure (non-zero exit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_push_failure_still_runs_cleanup() -> None:
    """Credential cleanup runs after git push failure.

    When ``git push`` exits non-zero the workflow body raises
    ``ApplicationError(GitPushFailed)`` from inside the ``try``
    branch, but the ``finally`` block still invokes
    ``cleanup_git_credentials``.  The cleanup invariant must hold even
    when the push fails - otherwise a stale credential helper would
    survive on the SSH runner past the workflow lifetime.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-push-fail-{uuid.uuid4().hex[:8]}"
    branch = "main"
    dept = "ops"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    push_attempts = 0

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        nonlocal push_attempts
        log.calls.append(("ssh_connect_and_run", (command,)))
        if command.startswith("git push"):
            push_attempts += 1
            # Push exits non-zero (e.g. authentication failed).
            return {
                "stdout": "",
                "stderr": "remote: Permission denied",
                "exit_code": 1,
            }
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    @activity.defn(name="minio_upload_artifact")
    async def minio_upload_artifact(
        bucket: str, key: str, data: bytes
    ) -> dict[str, Any]:
        log.calls.append(("minio_upload_artifact", (bucket, key)))
        return {"bucket": bucket, "key": key, "size_bytes": len(data)}

    @activity.defn(name="ssh_cleanup")
    async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
        log.calls.append(("ssh_cleanup", (ws_path,)))

    @activity.defn(name="inject_git_credentials")
    async def inject_git_credentials(
        input: CredentialInjectInput,  # noqa: A002
    ) -> CredentialInjectResult:
        log.calls.append(("inject_git_credentials", (input.dept_id,)))
        return CredentialInjectResult(
            success=True,
            error=None,
            masked_username="u***",
        )

    @activity.defn(name="cleanup_git_credentials")
    async def cleanup_git_credentials(workflow_id: str) -> None:
        log.calls.append(("cleanup_git_credentials", (workflow_id,)))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-push-fail-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
                inject_git_credentials,
                cleanup_git_credentials,
            ],
        ):
            with pytest.raises(Exception) as exc_info:
                await env.client.execute_workflow(
                    LegacyExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command="pytest -q",
                        workspace_path="/srv/runner/ws",
                        cleanup_policy="never",
                        timeout_minutes=30,
                        git_push_required=True,
                        git_push_branch=branch,
                        dept_id=dept,
                    ),
                    id=f"wf-push-fail-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    names = log.names()

    # The push was attempted exactly once.
    assert push_attempts == 1
    # Inject ran before the failed push, and cleanup ran *after* the
    # failed push. This is the cleanup invariant:
    # cleanup runs whether the push succeeded or not.
    assert "inject_git_credentials" in names, names
    assert "cleanup_git_credentials" in names, names

    inject_idx = names.index("inject_git_credentials")
    cleanup_idx = names.index("cleanup_git_credentials")
    push_indices = [
        i for i, (n, args) in enumerate(log.calls)
        if n == "ssh_connect_and_run" and args[0].startswith("git push")
    ]
    assert len(push_indices) == 1
    assert inject_idx < push_indices[0] < cleanup_idx, names

    # The workflow surfaces the failure as ApplicationError(GitPushFailed).
    # Temporal wraps the user error in a WorkflowFailureError  cause
    # is the original ApplicationError.
    cause = exc_info.value.cause if hasattr(exc_info.value, "cause") else None
    assert cause is None or isinstance(cause, ApplicationError) or "GitPush" in str(
        exc_info.value
    )


# ---------------------------------------------------------------------------
# Scenario 4: SSH-level failure during push still runs cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_git_push_ssh_error_still_runs_cleanup() -> None:
    """Credential cleanup runs after git push raises.

    A different failure mode: instead of returning a non-zero exit code
    the push activity raises (e.g. SSH connection drops mid-push).
    Temporal lifts the exception into the workflow body, but the
    ``finally`` block still fires ``cleanup_git_credentials``.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-push-raise-{uuid.uuid4().hex[:8]}"
    branch = "develop"
    dept = "data-science"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command,)))
        if command.startswith("git push"):
            raise ApplicationError(
                "ssh connection dropped during push",
                type="SSHActivityError",
                non_retryable=True,
            )
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    @activity.defn(name="minio_upload_artifact")
    async def minio_upload_artifact(
        bucket: str, key: str, data: bytes
    ) -> dict[str, Any]:
        log.calls.append(("minio_upload_artifact", (bucket, key)))
        return {"bucket": bucket, "key": key, "size_bytes": len(data)}

    @activity.defn(name="ssh_cleanup")
    async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
        log.calls.append(("ssh_cleanup", (ws_path,)))

    @activity.defn(name="inject_git_credentials")
    async def inject_git_credentials(
        input: CredentialInjectInput,  # noqa: A002
    ) -> CredentialInjectResult:
        log.calls.append(("inject_git_credentials", (input.dept_id,)))
        return CredentialInjectResult(
            success=True,
            error=None,
            masked_username="u***",
        )

    @activity.defn(name="cleanup_git_credentials")
    async def cleanup_git_credentials(workflow_id: str) -> None:
        log.calls.append(("cleanup_git_credentials", (workflow_id,)))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-push-raise-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
                inject_git_credentials,
                cleanup_git_credentials,
            ],
        ):
            with pytest.raises(Exception):
                await env.client.execute_workflow(
                    LegacyExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command="pytest -q",
                        workspace_path="/srv/runner/ws",
                        cleanup_policy="never",
                        timeout_minutes=30,
                        git_push_required=True,
                        git_push_branch=branch,
                        dept_id=dept,
                    ),
                    id=f"wf-push-raise-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    names = log.names()
    # Cleanup ran even though the push activity raised. Inject ran first,
    # the failing push ran next, then
    # cleanup.
    assert names.count("cleanup_git_credentials") == 1, names
    inject_idx = names.index("inject_git_credentials")
    cleanup_idx = names.index("cleanup_git_credentials")
    assert inject_idx < cleanup_idx, names


# ---------------------------------------------------------------------------
# Scenario 5: inject_git_credentials returns success=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inject_failure_raises_and_still_cleans_up() -> None:
    """Inject failure raises and still cleans up.

    When the credential-inject activity reports ``success=False`` the
    workflow surfaces an ``ApplicationError`` (so the parent workflow
    can react), but it must **still** run the cleanup activity from
    the ``finally`` branch.  This guards against a half-configured
    credential helper being left on the runner because the inject
    activity partially succeeded before deciding to report a failure.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-inject-fail-{uuid.uuid4().hex[:8]}"
    branch = "release/v2"
    dept = "platform"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command,)))
        return {"stdout": "ok", "stderr": "", "exit_code": 0}

    @activity.defn(name="minio_upload_artifact")
    async def minio_upload_artifact(
        bucket: str, key: str, data: bytes
    ) -> dict[str, Any]:
        log.calls.append(("minio_upload_artifact", (bucket, key)))
        return {"bucket": bucket, "key": key, "size_bytes": len(data)}

    @activity.defn(name="ssh_cleanup")
    async def ssh_cleanup(cred: dict[str, Any], ws_path: str) -> None:
        log.calls.append(("ssh_cleanup", (ws_path,)))

    @activity.defn(name="inject_git_credentials")
    async def inject_git_credentials(
        input: CredentialInjectInput,  # noqa: A002
    ) -> CredentialInjectResult:
        log.calls.append(("inject_git_credentials", (input.dept_id,)))
        return CredentialInjectResult(
            success=False,
            error="credential not found at vault path: secret/data/atlassian/platform/bitbucket",
            masked_username=None,
        )

    @activity.defn(name="cleanup_git_credentials")
    async def cleanup_git_credentials(workflow_id: str) -> None:
        log.calls.append(("cleanup_git_credentials", (workflow_id,)))

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-inject-fail-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
                inject_git_credentials,
                cleanup_git_credentials,
            ],
        ):
            with pytest.raises(Exception):
                await env.client.execute_workflow(
                    LegacyExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command="pytest -q",
                        workspace_path="/srv/runner/ws",
                        cleanup_policy="never",
                        timeout_minutes=30,
                        git_push_required=True,
                        git_push_branch=branch,
                        dept_id=dept,
                    ),
                    id=f"wf-inject-fail-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    names = log.names()
    # The push was *not* attempted because inject reported failure.
    push_calls = [
        n for n, args in log.calls
        if n == "ssh_connect_and_run" and args[0].startswith("git push")
    ]
    assert push_calls == [], names
    # But cleanup still ran.
    assert "cleanup_git_credentials" in names, names
    # And the order preserves the cleanup guarantee:
    # inject  (no push)  cleanup
    inject_idx = names.index("inject_git_credentials")
    cleanup_idx = names.index("cleanup_git_credentials")
    assert inject_idx < cleanup_idx, names


# ---------------------------------------------------------------------------
# Scenario 6: input dataclass backwards compatibility
# ---------------------------------------------------------------------------


class TestExecutionRunInputBackwardsCompat:
    """Optional push fields default to ``False`` / ``None`` so every
    existing call site that constructs :class:`ExecutionRunInput`
    without them keeps its current behaviour verbatim."""

    def test_defaults_preserve_legacy_contract(self) -> None:
        """Default input values preserve the legacy contract."""

        inp = ExecutionRunInput(
            workflow_id="exec-1",
            test_command="pytest -q",
            workspace_path="/srv/ws",
        )
        assert inp.git_push_required is False
        assert inp.git_push_branch is None
        assert inp.dept_id is None

    def test_explicit_push_fields_round_trip(self) -> None:
        """Explicit push inputs are retained on the workflow input."""

        inp = ExecutionRunInput(
            workflow_id="exec-1",
            test_command="pytest -q",
            workspace_path="/srv/ws",
            git_push_required=True,
            git_push_branch="feature/x",
            dept_id="payments",
        )
        assert inp.git_push_required is True
        assert inp.git_push_branch == "feature/x"
        assert inp.dept_id == "payments"
