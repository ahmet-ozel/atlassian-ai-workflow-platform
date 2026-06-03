"""Unit tests for the workspace disk-quota gate in
:class:`ExecutionRunWorkflow` and :class:`LegacyExecutionRunWorkflow`.

Both workflow bodies invoke the ``check_disk_quota`` activity *before*
launching any SSH command so a department whose runner workspace is
already at or above its cap fails fast without touching the disk.  The
gate is opt-in:

* Canonical :class:`ExecutionRunWorkflow` reads
  :attr:`ExecutionRunWorkflowInput.workspace_quota_mb` and
  :attr:`~ExecutionRunWorkflowInput.department_id`.
* Legacy :class:`LegacyExecutionRunWorkflow` reads the matching
  :attr:`ExecutionRunInput.workspace_quota_mb` /
  :attr:`~ExecutionRunInput.dept_id` pair.

When either field is unset the gate is skipped entirely and the
existing observable behaviour is preserved verbatim — every existing
integration test stays green.

Scenarios covered
-----------------

1. ``workspace_quota_mb`` is ``None`` — gate is skipped, ``ssh_run_test``
   runs normally (canonical).
2. ``dept_id`` is empty — gate is skipped (canonical).
3. Quota cap = 1024 MB, runner reports 500 MB — gate allows the run,
   ``ssh_run_test`` runs normally (canonical).
4. Quota cap = 1024 MB, runner reports 1100 MB — gate rejects with
   ``ApplicationError(type="DiskQuotaExceeded")``, ``ssh_run_test``
   is **never** invoked (canonical).
5. SSH probe fails (activity returns ``allowed=True`` with an
   ``error`` field — best-effort allow) — gate allows the run
   (canonical).
6. Legacy :class:`LegacyExecutionRunWorkflow` mirrors the same
   behaviour:
   - quota=1024, usage=500 → gate passes, full legacy flow runs.
   - quota=1024, usage=1100 → gate rejects, ``ssh_connect_and_run``
     is **never** invoked.

Determinism
-----------

The workflow body uses ``workflow.execute_activity`` for the gate
(replay-safe) and the resolver helper :func:`_resolve_quota_base` is a
pure function (no I/O, no ``os.environ`` reads).
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
# ``sys.path`` bootstrapping — mirror the pattern used by
# ``test_execution_run_workflow_git_push.py`` so the in-tree ``src/``
# package import resolves without an editable install.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# Module-level imports of the symbols referenced by activity-stub type
# hints — see the comment in ``test_execution_run_workflow_git_push.py``
# for the rationale (Temporal introspects activity stubs at decorator
# application time, which evaluates forward references against the
# *defining module's* globals).
from src.activities.disk_quota import (  # noqa: E402
    DiskQuotaInput,
    DiskQuotaResult,
)
from src.workflows.execution_run_workflow import (  # noqa: E402
    ExecutionRunInput,
    ExecutionRunResult,
    ExecutionRunWorkflow,
    LegacyExecutionRunWorkflow,
    _resolve_quota_base,
)
from temporal_shared.messages import (  # noqa: E402
    ExecutionRunWorkflowInput,
    ExecutionRunWorkflowOutput,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Records every activity invocation across the test for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _make_ssh_cred() -> dict[str, Any]:
    """Return a fixed SSH credential dict for the legacy mocks."""
    return {
        "host": "runner.test.invalid",
        "port": 22,
        "user": "runner",
        "private_key": (
            "-----BEGIN TEST KEY-----\nfake\n-----END TEST KEY-----\n"
        ),
    }


def _canonical_activities(
    log: _ActivityCallLog,
    *,
    quota_result: DiskQuotaResult | None = None,
    quota_raises: BaseException | None = None,
) -> list[Any]:
    """Build the activity stub bundle for the canonical workflow.

    The canonical workflow body invokes:
      * ``ssh_healthcheck`` (always — pre-gate)
      * ``check_disk_quota`` (only when quota_mb + dept_id set)
      * ``ssh_run_test`` (only when the gate allows)
      * ``apply_cleanup_policy`` (best-effort, post-run)

    The healthcheck and cleanup stubs are no-ops; the quota stub
    optionally returns ``quota_result`` or raises ``quota_raises``.
    """

    @activity.defn(name="ssh_healthcheck")
    async def ssh_healthcheck(input: Any = None) -> dict[str, Any]:
        log.calls.append(("ssh_healthcheck", (input,)))
        return {"healthy": True, "host": "runner.test.invalid"}

    @activity.defn(name="check_disk_quota")
    async def check_disk_quota(input: DiskQuotaInput) -> DiskQuotaResult:  # noqa: A002
        log.calls.append(
            (
                "check_disk_quota",
                (input.dept_id, input.workspace_base, input.quota_mb),
            )
        )
        if quota_raises is not None:
            raise quota_raises
        if quota_result is not None:
            return quota_result
        # Fallback: allow with zero usage.
        return DiskQuotaResult(
            allowed=True,
            usage_mb=0.0,
            quota_mb=input.quota_mb,
        )

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

    return [
        ssh_healthcheck,
        check_disk_quota,
        ssh_run_test,
        apply_cleanup_policy,
    ]


# ---------------------------------------------------------------------------
# Pure helper: _resolve_quota_base
# ---------------------------------------------------------------------------


class TestResolveQuotaBase:
    """The resolver strips ``iter-N`` and the issue-key segment so the
    activity measures the department-level workspace base, not the
    per-task workdir.  The function is pure (no I/O) so the workflow
    body can call it without breaking determinism."""

    def test_none_returns_empty(self) -> None:
        assert _resolve_quota_base(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert _resolve_quota_base("") == ""

    def test_canonical_layout_strips_iter_and_issue_key(self) -> None:
        # ``{base}/{ISSUE_KEY}/iter-{N}`` → ``{base}``.
        assert (
            _resolve_quota_base("/var/ai-runner/PAY-4211/iter-3")
            == "/var/ai-runner"
        )

    def test_layout_without_iter_strips_only_issue_key(self) -> None:
        # ``{base}/{ISSUE_KEY}`` → ``{base}`` (no iter segment to drop).
        assert _resolve_quota_base("/srv/runner/PAY-1") == "/srv/runner"

    def test_trailing_slash_normalised(self) -> None:
        assert (
            _resolve_quota_base("/var/ai-runner/PAY-4211/iter-3/")
            == "/var/ai-runner"
        )

    def test_windows_separators_normalised(self) -> None:
        assert (
            _resolve_quota_base("C:\\srv\\runner\\PAY-1\\iter-2")
            == "C:/srv/runner"
        )


# ---------------------------------------------------------------------------
# Canonical: gate skipped when workspace_quota_mb is None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_gate_skipped_when_quota_none() -> None:
    """The quota gate is skipped when no quota is configured.

    Every existing call site builds an :class:`ExecutionRunWorkflowInput`
    without ``workspace_quota_mb``, so the default ``None`` must skip
    the gate entirely — no ``check_disk_quota`` activity invocation.
    """

    log = _ActivityCallLog()
    activities = _canonical_activities(log)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-quota-skip-{uuid.uuid4().hex[:8]}"
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
                    runner_id="runner-1",
                    command="pytest -q",
                    workdir="/var/ai-runner/PAY-1/iter-1",
                    department_id="payments",
                    # workspace_quota_mb intentionally omitted → None
                ),
                id=f"wf-quota-skip-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    assert "check_disk_quota" not in names, names
    assert "ssh_run_test" in names, names
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Canonical: gate skipped when department_id is empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_gate_skipped_when_dept_id_empty() -> None:
    """Gate requires dept_id for dedup.

    The activity uses ``dept_id`` for warning deduplication; an empty
    value would short-circuit the dedup table.  The workflow logs a
    warning and skips the gate rather than calling the activity with
    an empty dept slug.
    """

    log = _ActivityCallLog()
    activities = _canonical_activities(log)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-quota-no-dept-{uuid.uuid4().hex[:8]}"
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
                    runner_id="runner-1",
                    command="pytest -q",
                    workdir="/var/ai-runner/PAY-1/iter-1",
                    department_id="",  # empty → skip
                    workspace_quota_mb=1024.0,
                ),
                id=f"wf-quota-no-dept-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    assert "check_disk_quota" not in names, names
    assert "ssh_run_test" in names, names
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Canonical: gate passes when usage is below cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_gate_passes_when_usage_below_cap() -> None:
    """Happy path when usage is below cap.

    With quota=1024 MB and reported usage=500 MB the gate allows the
    run and the workflow proceeds to ``ssh_run_test`` as normal.  The
    activity is invoked exactly once and the gate fires *before* the
    SSH command (deterministic order).
    """

    log = _ActivityCallLog()
    quota_ok = DiskQuotaResult(
        allowed=True,
        usage_mb=500.0,
        quota_mb=1024.0,
    )
    activities = _canonical_activities(log, quota_result=quota_ok)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-quota-pass-{uuid.uuid4().hex[:8]}"
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
                    runner_id="runner-1",
                    command="pytest -q",
                    workdir="/var/ai-runner/PAY-1/iter-1",
                    department_id="payments",
                    workspace_quota_mb=1024.0,
                ),
                id=f"wf-quota-pass-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    # Order: healthcheck → check_disk_quota → ssh_run_test → cleanup
    assert names.count("check_disk_quota") == 1, names
    quota_idx = names.index("check_disk_quota")
    run_idx = names.index("ssh_run_test")
    assert quota_idx < run_idx, names

    # The activity received the resolved base path (parent of workdir,
    # with iter-N stripped) and the cap as float.
    quota_call_args = log.calls[quota_idx][1]
    dept_id_arg, base_arg, cap_arg = quota_call_args
    assert dept_id_arg == "payments"
    assert base_arg == "/var/ai-runner"
    assert cap_arg == 1024.0

    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Canonical: gate rejects when usage exceeds cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_gate_rejects_when_usage_exceeds_cap() -> None:
    """disk_quota_exceeded fail-fast.

    With quota=1024 MB and reported usage=1100 MB the gate rejects the
    run.  The workflow surfaces ``ApplicationError(type="DiskQuotaExceeded",
    non_retryable=True)`` and the (non-idempotent) ``ssh_run_test``
    activity is **never** invoked — preventing a storm of failing runs
    against an already-full disk.
    """

    log = _ActivityCallLog()
    quota_exceeded = DiskQuotaResult(
        allowed=False,
        usage_mb=1100.0,
        quota_mb=1024.0,
        error="disk_quota_exceeded",
    )
    activities = _canonical_activities(log, quota_result=quota_exceeded)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-quota-reject-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ExecutionRunWorkflow],
            activities=activities,
        ):
            with pytest.raises(Exception) as exc_info:
                await env.client.execute_workflow(
                    ExecutionRunWorkflow.run,
                    ExecutionRunWorkflowInput(
                        parent_workflow_id="parent-1",
                        runner_id="runner-1",
                        command="pytest -q",
                        workdir="/var/ai-runner/PAY-1/iter-1",
                        department_id="payments",
                        workspace_quota_mb=1024.0,
                    ),
                    id=f"wf-quota-reject-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    names = log.names()
    # Gate fired exactly once, ssh_run_test never ran.
    assert names.count("check_disk_quota") == 1, names
    assert "ssh_run_test" not in names, names

    # Temporal wraps the user error in a WorkflowFailureError → cause is
    # ApplicationError(type="DiskQuotaExceeded").  We accept either the
    # cause or the message containing "DiskQuotaExceeded" / "disk quota".
    cause = getattr(exc_info.value, "cause", None)
    assert (
        (isinstance(cause, ApplicationError) and cause.type == "DiskQuotaExceeded")
        or "DiskQuotaExceeded" in str(exc_info.value)
        or "disk quota" in str(exc_info.value).lower()
    ), exc_info.value


# ---------------------------------------------------------------------------
# Canonical: SSH probe failure → activity returns allowed=True (best-effort)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_gate_allows_when_probe_fails_best_effort() -> None:
    """Probe failures are handled as best-effort allows.

    When the SSH probe fails the activity returns ``allowed=True`` with
    a non-empty ``error`` field (matching the contract pinned in
    ``activities/disk_quota.py``).  The workflow logs a diagnostic and
    proceeds to ``ssh_run_test`` as normal — a transient network blip
    must not wedge every run on a flaky check.
    """

    log = _ActivityCallLog()
    probe_failed = DiskQuotaResult(
        allowed=True,
        usage_mb=0.0,
        quota_mb=1024.0,
        error="disk_check_failed: ssh execution failed",
    )
    activities = _canonical_activities(log, quota_result=probe_failed)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-quota-probe-fail-{uuid.uuid4().hex[:8]}"
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
                    runner_id="runner-1",
                    command="pytest -q",
                    workdir="/var/ai-runner/PAY-1/iter-1",
                    department_id="payments",
                    workspace_quota_mb=1024.0,
                ),
                id=f"wf-quota-probe-fail-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    assert names.count("check_disk_quota") == 1, names
    assert names.count("ssh_run_test") == 1, names
    assert result.status == "passed"


# ---------------------------------------------------------------------------
# Legacy: gate passes when usage is below cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_gate_passes_when_usage_below_cap() -> None:
    """Legacy workflow gate allows runs below the quota cap.

    The legacy workflow mirrors the canonical contract: when the cap +
    dept are both supplied the gate runs after Vault credential fetch
    and before ``ssh_connect_and_run``.  Usage 500 MB / cap 1024 MB
    allows the run.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-legacy-quota-pass-{uuid.uuid4().hex[:8]}"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="check_disk_quota")
    async def check_disk_quota(input: DiskQuotaInput) -> DiskQuotaResult:  # noqa: A002
        log.calls.append(
            (
                "check_disk_quota",
                (input.dept_id, input.workspace_base, input.quota_mb),
            )
        )
        return DiskQuotaResult(
            allowed=True,
            usage_mb=500.0,
            quota_mb=1024.0,
        )

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command, ws_path)))
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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-legacy-quota-pass-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                check_disk_quota,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
            ],
        ):
            result: ExecutionRunResult = await env.client.execute_workflow(
                LegacyExecutionRunWorkflow.run,
                ExecutionRunInput(
                    workflow_id=workflow_id,
                    test_command="pytest -q",
                    workspace_path="/srv/runner/PAY-1/iter-1",
                    cleanup_policy="never",
                    timeout_minutes=30,
                    dept_id="payments",
                    workspace_quota_mb=1024.0,
                ),
                id=f"wf-legacy-quota-pass-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    # Order: vault → check_disk_quota → ssh_connect_and_run → minio×3
    vault_idx = names.index("vault_fetch_ssh_credentials")
    quota_idx = names.index("check_disk_quota")
    ssh_idx = names.index("ssh_connect_and_run")
    assert vault_idx < quota_idx < ssh_idx, names
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Legacy: gate rejects when usage exceeds cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_gate_rejects_when_usage_exceeds_cap() -> None:
    """Legacy fail-fast when usage exceeds cap.

    Legacy workflow with cap=1024 MB and usage=1100 MB rejects with
    ``ApplicationError(type="DiskQuotaExceeded")``; the SSH command is
    never executed.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-legacy-quota-reject-{uuid.uuid4().hex[:8]}"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="check_disk_quota")
    async def check_disk_quota(input: DiskQuotaInput) -> DiskQuotaResult:  # noqa: A002
        log.calls.append(
            (
                "check_disk_quota",
                (input.dept_id, input.workspace_base, input.quota_mb),
            )
        )
        return DiskQuotaResult(
            allowed=False,
            usage_mb=1100.0,
            quota_mb=1024.0,
            error="disk_quota_exceeded",
        )

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command, ws_path)))
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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-legacy-quota-reject-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                check_disk_quota,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
            ],
        ):
            with pytest.raises(Exception) as exc_info:
                await env.client.execute_workflow(
                    LegacyExecutionRunWorkflow.run,
                    ExecutionRunInput(
                        workflow_id=workflow_id,
                        test_command="pytest -q",
                        workspace_path="/srv/runner/PAY-1/iter-1",
                        cleanup_policy="never",
                        timeout_minutes=30,
                        dept_id="payments",
                        workspace_quota_mb=1024.0,
                    ),
                    id=f"wf-legacy-quota-reject-{uuid.uuid4().hex[:8]}",
                    task_queue=task_queue,
                )

    names = log.names()
    assert "check_disk_quota" in names, names
    # The SSH command must not have run — fail-fast contract.
    assert "ssh_connect_and_run" not in names, names
    # And no artifacts were uploaded.
    assert "minio_upload_artifact" not in names, names

    cause = getattr(exc_info.value, "cause", None)
    assert (
        (isinstance(cause, ApplicationError) and cause.type == "DiskQuotaExceeded")
        or "DiskQuotaExceeded" in str(exc_info.value)
        or "disk quota" in str(exc_info.value).lower()
    ), exc_info.value


# ---------------------------------------------------------------------------
# Legacy: gate skipped when fields are unset (backwards compatibility)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_gate_skipped_when_quota_fields_unset() -> None:
    """The legacy quota gate is skipped when quota fields are unset.

    Every existing legacy call site builds an :class:`ExecutionRunInput`
    without the optional ``workspace_quota_mb`` / ``dept_id`` pair, so the
    defaults (``None`` / ``None``) must skip the gate entirely — no
    ``check_disk_quota`` invocation.  The full legacy flow runs exactly
    as before.
    """

    log = _ActivityCallLog()
    workflow_id = f"exec-legacy-quota-skip-{uuid.uuid4().hex[:8]}"

    @activity.defn(name="vault_fetch_ssh_credentials")
    async def vault_fetch_ssh_credentials(wf_id: str) -> dict[str, Any]:
        log.calls.append(("vault_fetch_ssh_credentials", (wf_id,)))
        return _make_ssh_cred()

    @activity.defn(name="check_disk_quota")
    async def check_disk_quota(input: DiskQuotaInput) -> DiskQuotaResult:  # noqa: A002
        log.calls.append(
            (
                "check_disk_quota",
                (input.dept_id, input.workspace_base, input.quota_mb),
            )
        )
        return DiskQuotaResult(
            allowed=True,
            usage_mb=0.0,
            quota_mb=None,
        )

    @activity.defn(name="ssh_connect_and_run")
    async def ssh_connect_and_run(
        cred: dict[str, Any],
        command: str,
        ws_path: str,
        timeout_minutes: int,
    ) -> dict[str, Any]:
        log.calls.append(("ssh_connect_and_run", (command, ws_path)))
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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"er-legacy-quota-skip-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[LegacyExecutionRunWorkflow],
            activities=[
                vault_fetch_ssh_credentials,
                check_disk_quota,
                ssh_connect_and_run,
                minio_upload_artifact,
                ssh_cleanup,
            ],
        ):
            result: ExecutionRunResult = await env.client.execute_workflow(
                LegacyExecutionRunWorkflow.run,
                ExecutionRunInput(
                    workflow_id=workflow_id,
                    test_command="pytest -q",
                    workspace_path="/srv/runner/PAY-1/iter-1",
                    cleanup_policy="never",
                    timeout_minutes=30,
                    # workspace_quota_mb / dept_id intentionally omitted
                ),
                id=f"wf-legacy-quota-skip-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    names = log.names()
    assert "check_disk_quota" not in names, names
    assert "ssh_connect_and_run" in names, names
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Input dataclass backwards compatibility
# ---------------------------------------------------------------------------


class TestExecutionRunInputDiskQuotaDefaults:
    """The ``workspace_quota_mb`` field defaults to ``None`` so
    every existing call site that constructs the dataclass without it
    keeps its current behaviour verbatim."""

    def test_legacy_input_default_is_none(self) -> None:
        inp = ExecutionRunInput(
            workflow_id="exec-1",
            test_command="pytest -q",
            workspace_path="/srv/ws",
        )
        assert inp.workspace_quota_mb is None

    def test_canonical_input_default_is_none(self) -> None:
        inp = ExecutionRunWorkflowInput(
            parent_workflow_id="parent-1",
            runner_id="runner-1",
            command="pytest -q",
        )
        assert inp.workspace_quota_mb is None

    def test_legacy_input_explicit_round_trip(self) -> None:
        inp = ExecutionRunInput(
            workflow_id="exec-1",
            test_command="pytest -q",
            workspace_path="/srv/ws",
            dept_id="payments",
            workspace_quota_mb=2048.0,
        )
        assert inp.workspace_quota_mb == 2048.0
        assert inp.dept_id == "payments"

    def test_canonical_input_explicit_round_trip(self) -> None:
        inp = ExecutionRunWorkflowInput(
            parent_workflow_id="parent-1",
            runner_id="runner-1",
            command="pytest -q",
            department_id="payments",
            workspace_quota_mb=2048.0,
        )
        assert inp.workspace_quota_mb == 2048.0
        assert inp.department_id == "payments"
