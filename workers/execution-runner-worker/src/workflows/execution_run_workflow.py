"""ExecutionRunWorkflow — Temporal workflow for remote test execution.

This module hosts **two** Temporal workflow definitions:

* :class:`ExecutionRunWorkflow` — the **canonical** workflow as defined
  by ``platform-mimari-workflows`` task 2.3 / Requirements 1.1 and 1.6.
  Accepts the single-payload :class:`ExecutionRunWorkflowInput` from
  :mod:`temporal_shared.messages` and returns the matching
  :class:`ExecutionRunWorkflowOutput`.  Internally orchestrates a single
  ``ssh_run_test`` activity that performs the credential fetch, command
  execution, stdout/stderr upload to MinIO, and 30-second heartbeating
  in one place.  The activity ``start_to_close_timeout`` is sourced from
  workflow input (which the gateway populates from configuration), not
  hard-coded in workflow code.

  **Enhancements (post-foundation):**

  * **SSH healthcheck** — Before invoking ``ssh_run_test``, the workflow
    runs a lightweight ``ssh_healthcheck`` activity (TCP connect probe).
    If the runner is unreachable the workflow enters a "queued" retry
    loop with exponential backoff (60s → 120s → 240s, max 30 min total).
    An ``ssh_host_unhealthy`` audit event is emitted and a Jira bot
    comment is posted. If the runner recovers within the retry window
    the workflow proceeds normally; otherwise it fails with
    ``failure_reason="runner_unreachable"``.

  * **Cleanup policy enforcement** — After ``ssh_run_test`` completes,
    the workflow invokes ``apply_cleanup_policy`` to honour the user's
    cleanup preference (``delete_on_success`` | ``always`` | ``never``).
    This ensures Docker containers, images, and workspace directories
    are cleaned up (or preserved) according to the task's Forge custom
    field setting.

* :class:`LegacyExecutionRunWorkflow` — the foundation-spec scaffold
  that the integration tests under ``tests/integration/test_execution_runner.py``
  exercise.  Preserved here unchanged in behaviour so the existing
  tested orchestration of ``vault_fetch_ssh_credentials`` →
  ``ssh_connect_and_run`` → ``minio_upload_artifact`` × 3 → optional
  ``ssh_cleanup`` keeps green.  The class and its Temporal-registered
  name end with ``Legacy`` so the canonical workflow can claim the
  ``"ExecutionRunWorkflow"`` Temporal name without colliding.

Both workflow bodies are **replay-safe** (no direct I/O, no
``datetime.now()``, no ``random`` / ``uuid``).  All side effects happen
inside activities.

Requirements: 1.1, 1.6 (canonical) and 8.1-8.8 (legacy).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_shared.helpers import CleanupPolicy, should_cleanup
    from temporal_shared.identifiers import execution_artifact_key
    from temporal_shared.messages import (
        ExecutionRunWorkflowInput,
        ExecutionRunWorkflowOutput,
    )

    from src.activities.credential_injector import (
        CredentialInjectInput,
        CredentialInjectResult,
    )
    from src.activities.disk_quota import (
        DiskQuotaInput,
        DiskQuotaResult,
    )
    from src.activities.minio import DEFAULT_BUCKET
    from src.activities.runner_resolver import (
        RunnerResolution,
        RunnerResolutionError,
    )
    from runners.workspace_path import build_workspace_path
    # EK2 — dataclass shapes for the docker chain. The activities
    # themselves are referenced by string name through
    # ``workflow.execute_activity`` so the workflow module never
    # imports the activity callables (those carry paramiko/io).
    from src.activities.docker import (
        DockerBuildInput,
        DockerCleanupInput,
        DockerHealthcheckInput,
        DockerRunInput,
    )
    from src.activities.ssh_healthcheck import SSHHealthcheckInput


# ---------------------------------------------------------------------------
# Default timeouts (canonical workflow)
# ---------------------------------------------------------------------------

#: Default ``start_to_close_timeout`` applied to ``ssh_run_test`` when the
#: workflow input leaves it unset.  Mirrors ``SSH_COMMAND_TIMEOUT_S=1800``
#: in ``execution-runner-worker/.env.example`` — 30 minutes.  The gateway
#: populates :attr:`ExecutionRunWorkflowInput.start_to_close_timeout` from
#: configuration; this constant is the fallback so the workflow body never
#: forces an open-ended activity attempt.
DEFAULT_START_TO_CLOSE: timedelta = timedelta(minutes=30)

#: Default heartbeat timeout.  The activity beats every 30 s while it
#: streams the SSH command, so allowing 90 s here gives a comfortable 3×
#: safety margin before Temporal declares the activity stalled
#: (Requirement 1.6 — heartbeat 30 s contract).
DEFAULT_HEARTBEAT: timedelta = timedelta(seconds=90)

# ---------------------------------------------------------------------------
# noop_test smoke-flow defaults  (Requirement 6.8, task 10.4)
# ---------------------------------------------------------------------------

#: Workflow-type discriminator that opts into the smoke-flow defaults
#: below.  Mirrors the literal in
#: :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`;
#: kept as a module-level constant here so a typo in the gating check
#: surfaces immediately rather than silently disabling the safety net.
_NOOP_TEST_WORKFLOW_TYPE: Final[str] = "noop_test"

#: Default command synthesised for a ``noop_test`` execution when the
#: caller leaves :attr:`ExecutionRunWorkflowInput.command` empty.
#: ``echo "ok"`` is intentionally trivial — the smoke-test path
#: validates the full pipeline (webhook → AutomationWorkflow →
#: ExecutionRunWorkflow → SSH runner → activity → Jira comment)
#: without exercising any business logic on the runner.  Matches the
#: user-pinned wording in tasks.md §10.4.
_NOOP_TEST_DEFAULT_COMMAND: Final[str] = 'echo "ok"'

#: Tighter ``start_to_close_timeout`` applied to ``noop_test`` runs.
#: A smoke test that takes longer than 30 seconds is almost certainly
#: a runner / network problem rather than a slow command, so we bound
#: the wait aggressively and let Temporal surface the timeout to the
#: parent :class:`AutomationWorkflow` quickly.  Pinned by tasks.md
#: §10.4 ("e.g. 30 seconds — noop should never run long").
_NOOP_TEST_START_TO_CLOSE: Final[timedelta] = timedelta(seconds=30)

#: Retry policy for ``ssh_run_test``.  Limited to 3 attempts to honour the
#: ``maximumAttempts ≤ 3`` rule for non-idempotent activities (Requirement
#: 1.6).  An SSH test run is **not** idempotent: re-running the command
#: re-executes side effects on the runner.
_RUN_TEST_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)

# ---------------------------------------------------------------------------
# SSH healthcheck retry parameters (post-foundation enhancement)
# ---------------------------------------------------------------------------

#: Timeout for the ``ssh_healthcheck`` activity.  The probe is a simple
#: TCP connect — 30 seconds is generous.
_HEALTHCHECK_TIMEOUT: timedelta = timedelta(seconds=30)

#: Initial backoff between healthcheck retries when the runner is down.
_HEALTHCHECK_INITIAL_BACKOFF: timedelta = timedelta(seconds=60)

#: Maximum total time the workflow will wait for the runner to recover
#: before failing with ``runner_unreachable``.  30 minutes matches the
#: default ``start_to_close_timeout`` for the main activity.
_HEALTHCHECK_MAX_WAIT: timedelta = timedelta(minutes=30)

#: Maximum number of healthcheck retry attempts.  With 60s initial
#: backoff and 2× coefficient: 60 + 120 + 240 + 480 = 900s (15 min)
#: for 4 retries.  We allow up to 6 retries to cover the 30-min window.
_HEALTHCHECK_MAX_RETRIES: int = 6

#: Backoff coefficient for healthcheck retries.
_HEALTHCHECK_BACKOFF_COEFFICIENT: float = 2.0

# ---------------------------------------------------------------------------
# Cleanup policy activity parameters
# ---------------------------------------------------------------------------

#: Timeout for the ``apply_cleanup_policy`` activity.  Cleanup involves
#: SSH + docker commands — 2 minutes is generous for best-effort work.
_CLEANUP_TIMEOUT: timedelta = timedelta(minutes=2)

#: Retry policy for cleanup — single attempt since it's best-effort.
_CLEANUP_RETRY_POLICY: RetryPolicy = RetryPolicy(maximum_attempts=1)


# ---------------------------------------------------------------------------
# Runner resolver activity parameters (Requirements 4.4, 4.5, 4.8)
# ---------------------------------------------------------------------------

#: Timeout for the ``resolve_runner`` activity.  The activity performs a
#: DB query against ``infrastructure.ssh_runners`` + a best-effort audit
#: write — 30 seconds is generous for a single SQL round-trip.
_RUNNER_RESOLVER_TIMEOUT: timedelta = timedelta(seconds=30)

#: Retry policy for ``resolve_runner``.  Two attempts cover transient
#: DB connection flakes without amplifying load when the database is
#: genuinely down.  A ``RunnerResolutionError`` (no active runner) is
#: non-retryable by nature — the retry only helps with infrastructure
#: glitches.
_RUNNER_RESOLVER_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=2,
)

# ---------------------------------------------------------------------------
# Disk quota gate parameters (Requirement 16.1, 16.2)
# ---------------------------------------------------------------------------

#: Timeout for the ``check_disk_quota`` activity.  The activity itself
#: bounds the SSH ``du -sm`` window at 30s (R16.1); a 60s start-to-close
#: gives a 2× safety margin for credential fetch + connect overhead so
#: a transiently slow runner does not falsely trip the gate.
_DISK_QUOTA_TIMEOUT: timedelta = timedelta(seconds=60)

#: Retry policy for the disk-quota gate.  A second attempt covers
#: transient SSH flakes (lost packets, brief auth glitches) without
#: amplifying load when the runner is genuinely down — the activity
#: itself returns ``allowed=True`` on SSH failure (best-effort allow,
#: see ``check_disk_quota`` docstring) so two attempts is plenty.
_DISK_QUOTA_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_attempts=2,
)


def _resolve_quota_base(workdir: str | None) -> str:
    """Derive the disk-quota measurement base path from ``workdir``.

    The canonical workspace layout pinned by
    :func:`runners.workspace_path.build_workspace_path` is::

        {RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}

    Quota is enforced at the **department-runner-base** level, not the
    per-iteration workdir, so this helper strips the trailing
    ``/iter-N`` (when present) and the ``ISSUE_KEY`` segment to land on
    the runner base path.  Any string that does not match that layout
    falls through verbatim — the activity will measure whatever it
    receives.

    The helper is deterministic and pure (no I/O, no environment
    reads), which is important because it executes inside the workflow
    body.

    Parameters
    ----------
    workdir:
        The per-task working directory passed via
        :attr:`ExecutionRunWorkflowInput.workdir`.  ``None`` or an
        empty string returns the empty string, which the activity
        translates into the env-derived ``RUNNER_BASE_PATH`` on the
        runner side.

    Returns
    -------
    str
        Forward-slash-normalised base path suitable for ``du -sm``.
    """

    if not workdir:
        return ""
    # Normalise to forward slashes (workflow inputs may carry POSIX or
    # mixed separators depending on the dispatcher).
    normalised = workdir.replace("\\", "/").rstrip("/")
    if not normalised:
        return ""
    parts = normalised.split("/")
    # Walk back from the tail: drop a trailing ``iter-N`` segment, then
    # drop the issue-key segment.  Anything that does not match the
    # canonical layout is returned verbatim.
    if parts and parts[-1].startswith("iter-"):
        parts.pop()
    if len(parts) >= 2:
        # The remaining tail segment is the issue key — strip it so the
        # measurement covers the whole department workspace base.
        parts.pop()
    base = "/".join(parts)
    # Preserve a leading absolute slash if the caller supplied one and
    # the strip-down did not consume it (e.g. ``/var/ai-runner``).
    if normalised.startswith("/") and not base.startswith("/"):
        base = "/" + base
    return base or normalised


def _iteration_from_workdir(workdir: str | None) -> int:
    """Extract trailing ``iter-N`` from a workdir, defaulting to 0."""
    if not workdir:
        return 0
    tail = workdir.replace("\\", "/").rstrip("/").split("/")[-1]
    if not tail.startswith("iter-"):
        return 0
    try:
        value = int(tail[5:])
    except ValueError:
        return 0
    return value if value >= 0 else 0


def _coerce_timeout(
    value: timedelta | int | float | None,
) -> timedelta | None:
    """Normalise workflow timeout overrides for Temporal activity calls."""

    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return timedelta(seconds=float(value))
    return None


def _activity_result_field(
    result: Any,
    name: str,
    default: Any = None,
) -> Any:
    """Read an activity result field from either dicts or dataclasses."""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _derive_workdir(
    inp: ExecutionRunWorkflowInput,
    resolved_base_path: str | None,
) -> str | None:
    """Build a per-task workspace from the resolved runner base path."""
    if not resolved_base_path or not inp.parent_workflow_id:
        return inp.workdir
    try:
        return build_workspace_path(
            resolved_base_path,
            inp.parent_workflow_id,
            _iteration_from_workdir(inp.workdir),
        )
    except Exception:  # noqa: BLE001 - keep legacy workdir if validation fails
        return inp.workdir


# ---------------------------------------------------------------------------
# Canonical ExecutionRunWorkflow (task 2.3 — Requirements 1.1, 1.6)
# ---------------------------------------------------------------------------


@workflow.defn(name="ExecutionRunWorkflow")
class ExecutionRunWorkflow:
    """Remote SSH/Docker test runner workflow.

    Reads a single :class:`ExecutionRunWorkflowInput` and returns a
    single :class:`ExecutionRunWorkflowOutput`.  The body is a thin
    orchestrator around the ``ssh_run_test`` activity, which handles
    credential resolution, command execution, MinIO artifact upload,
    and heartbeating internally.

    Status mapping (Requirement 1.6):

    * ``exit_code == 0``                       → ``"passed"``
    * ``exit_code != 0`` (other than timeout)  → ``"failed"``
    * SSH/runner command timeout (signalled by the activity returning
      ``status="timeout"``)                    → ``"timeout"``

    ``noop_test`` smoke-flow safety net (R6.8, task 10.4)
    -----------------------------------------------------

    When :attr:`ExecutionRunWorkflowInput.workflow_type` is
    ``"noop_test"`` the workflow applies two **opt-in defaults** so a
    smoke run dispatched against a freshly-onboarded department always
    has something safe to execute:

    * An empty :attr:`~ExecutionRunWorkflowInput.command` is replaced
      with ``echo "ok"``.
    * An unset
      :attr:`~ExecutionRunWorkflowInput.start_to_close_timeout` is
      tightened to 30 seconds — a smoke run that takes longer is
      almost certainly a runner / network problem rather than a slow
      command.

    Both defaults are no-ops when the parent
    :class:`AutomationWorkflow` populates the fields explicitly (the
    common production path).  They exist so an ad-hoc dispatch (CLI,
    integration test) cannot accidentally launch an open-ended
    smoke run on the SSH runner.  Every other workflow type sees the
    legacy contract — :attr:`command` is consumed verbatim and an
    empty command surfaces as a runner-side error.
    """

    @workflow.run
    async def run(
        self, inp: ExecutionRunWorkflowInput
    ) -> ExecutionRunWorkflowOutput:
        """Execute the remote test and report a structured result.

        Parameters
        ----------
        inp:
            Frozen input dataclass.  Carries the runner identifier,
            command to execute, environment, optional working directory,
            MinIO artifact prefix, and the ``start_to_close_timeout`` /
            ``heartbeat_timeout`` overrides supplied by the parent
            workflow.

        Returns
        -------
        ExecutionRunWorkflowOutput
            ``status``, ``exit_code``, ``stdout_uri`` / ``stderr_uri``,
            ``duration_seconds``, ``runner_id``, and ``failure_reason``.
        """
        workflow.logger.info(
            "ExecutionRunWorkflow.run: parent=%s runner_id=%s "
            "department=%s artifact_prefix=%s",
            inp.parent_workflow_id,
            inp.runner_id,
            inp.department_id,
            inp.artifact_minio_prefix,
        )

        # -----------------------------------------------------------------
        # Step 0a: Resolve runner from DB (Requirements 4.4, 4.5, 4.8)
        # -----------------------------------------------------------------
        # When a department_id is available and no explicit runner_id is
        # provided, call the resolve_runner activity to select the
        # least-busy runner assigned to the department.  The resolved
        # runner_id is then used for all subsequent steps (healthcheck,
        # SSH execution, vault credential fetch).  This enables the
        # multi-SSH runner pool (G5) while preserving backward
        # compatibility: when runner_id is already set (legacy path) or
        # department_id is empty, the workflow skips resolution and uses
        # the input runner_id verbatim.
        resolved_runner_id = inp.runner_id
        resolved_vault_path: str | None = None
        resolved_base_path: str | None = None
        resolved_host: str | None = None
        resolved_port: int | None = None
        if inp.department_id and not inp.runner_id:
            resolution = await self._resolve_runner(inp.department_id)
            if resolution is not None:
                resolved_runner_id = resolution["runner_id"]
                resolved_vault_path = resolution.get("vault_path")
                resolved_base_path = resolution.get("base_path")
                resolved_host = resolution.get("host")
                raw_port = resolution.get("port")
                resolved_port = int(raw_port) if raw_port is not None else None
                workflow.logger.info(
                    "ExecutionRunWorkflow.run: resolved runner_id=%s "
                    "host=%s port=%s vault_path=%s base_path=%s for dept=%s",
                    resolved_runner_id,
                    resolution.get("host"),
                    resolution.get("port"),
                    resolved_vault_path,
                    resolved_base_path,
                    inp.department_id,
                )

        # -----------------------------------------------------------------
        # Step 0b: SSH runner healthcheck (post-foundation enhancement)
        # -----------------------------------------------------------------
        # Perform a lightweight TCP probe before committing to the full
        # ssh_run_test activity.  If the runner is unreachable, retry
        # with exponential backoff up to _HEALTHCHECK_MAX_WAIT.
        runner_healthy = await self._wait_for_healthy_runner(
            inp,
            runner_id=resolved_runner_id,
            host=resolved_host,
            port=resolved_port,
        )
        if not runner_healthy:
            workflow.logger.error(
                "ExecutionRunWorkflow.run: runner unreachable after "
                "max retries — failing workflow"
            )
            return ExecutionRunWorkflowOutput(
                status="failed",
                exit_code=None,
                stdout_uri=None,
                stderr_uri=None,
                duration_seconds=0.0,
                runner_id=resolved_runner_id,
                failure_reason="runner_unreachable",
            )

        # ``noop_test`` smoke-flow safety net (R6.8, task 10.4).
        command = inp.command
        start_to_close_override = _coerce_timeout(inp.start_to_close_timeout)
        if inp.workflow_type == _NOOP_TEST_WORKFLOW_TYPE:
            if not command:
                command = _NOOP_TEST_DEFAULT_COMMAND
                workflow.logger.info(
                    "ExecutionRunWorkflow.run: noop_test smoke flow — "
                    "command was empty, defaulting to %r",
                    command,
                )
            if start_to_close_override is None:
                start_to_close_override = _NOOP_TEST_START_TO_CLOSE
                workflow.logger.info(
                    "ExecutionRunWorkflow.run: noop_test smoke flow — "
                    "start_to_close_timeout was unset, tightening to %s",
                    start_to_close_override,
                )

        effective_workdir = _derive_workdir(inp, resolved_base_path)
        run_input = (
            replace(inp, workdir=effective_workdir)
            if effective_workdir != inp.workdir
            else inp
        )

        # -----------------------------------------------------------------
        # Step 0.5: Workspace disk-quota gate (Requirements 16.1, 16.2)
        # -----------------------------------------------------------------
        # Before committing to the (non-idempotent) ``ssh_run_test``
        # activity, ask the runner whether the department's workspace
        # base path is already at or above its quota.  This prevents a
        # storm of failing runs against a full disk — the activity
        # itself returns ``allowed=True`` on SSH failure (best-effort
        # allow) so a transient network blip cannot wedge the workflow.
        #
        # Gate is opt-in: when ``workspace_quota_mb`` is ``None`` (the
        # legacy path, which every existing call site exercises) the
        # workflow skips the activity invocation entirely so the
        # registered worker bundle stays optional.
        await self._enforce_disk_quota(run_input)

        # Activity options sourced from input (R1.6 — config-driven).
        start_to_close = start_to_close_override or DEFAULT_START_TO_CLOSE
        heartbeat = _coerce_timeout(inp.heartbeat_timeout) or DEFAULT_HEARTBEAT

        # EK2 fix (GEREKSINIM_ANALIZI.md): when ``needs_docker`` is True
        # the workflow runs the structured Docker chain
        # (build → run → collect_logs → cleanup) instead of the single
        # ``ssh_run_test`` invocation. Previously the ``docker_*``
        # activities were registered with the worker but no workflow
        # called them — Docker only ran if the user happened to embed
        # ``docker ...`` in the raw SSH command. With ``needs_docker``
        # now propagated from the analyser through ``_child_args``, the
        # gateway's classification finally drives a real build/run/cleanup.
        if inp.needs_docker:
            result = await self._run_docker_chain(
                run_input,
                command=command,
                resolved_runner_id=resolved_runner_id,
                resolved_vault_path=resolved_vault_path,
            )
        else:
            # K2 fix: forward the resolved ``vault_path`` so
            # ``ssh_run_test`` reads the admin-panel-configured runner's
            # secret instead of the global ``ssh/runner/current`` Vault
            # path. Previously this value was computed by
            # ``_resolve_runner`` and silently discarded — the whole
            # multi-runner pool was decorative until now.
            result = await workflow.execute_activity(
                "ssh_run_test",
                args=[
                    resolved_runner_id,
                    command,
                    run_input.environment,
                    run_input.artifact_minio_prefix,
                    run_input.workdir,
                    run_input.parent_workflow_id,
                    resolved_vault_path,
                ],
                start_to_close_timeout=start_to_close,
                heartbeat_timeout=heartbeat,
                retry_policy=_RUN_TEST_RETRY_POLICY,
            )

        status = result.get("status", "failed")
        exit_code = result.get("exit_code")
        stdout_uri = result.get("stdout_uri")
        stderr_uri = result.get("stderr_uri")
        duration_seconds = float(result.get("duration_seconds", 0.0))
        runner_id_out = result.get("runner_id", resolved_runner_id)
        failure_reason = result.get("failure_reason")

        workflow.logger.info(
            "ExecutionRunWorkflow.run finished: status=%s exit_code=%s "
            "stdout_uri=%s stderr_uri=%s duration_seconds=%s "
            "failure_reason=%s",
            status,
            exit_code,
            stdout_uri,
            stderr_uri,
            duration_seconds,
            failure_reason,
        )

        # -----------------------------------------------------------------
        # Step final: Apply cleanup policy (post-foundation enhancement)
        # -----------------------------------------------------------------
        # Enforce the user-specified cleanup policy. The activity is
        # best-effort — cleanup failures do not alter the workflow result.
        await self._apply_cleanup_policy(
            run_input,
            exit_code,
            resolved_vault_path=resolved_vault_path,
        )

        return ExecutionRunWorkflowOutput(
            status=status,
            exit_code=exit_code,
            stdout_uri=stdout_uri,
            stderr_uri=stderr_uri,
            duration_seconds=duration_seconds,
            runner_id=runner_id_out,
            failure_reason=failure_reason,
        )

    # -----------------------------------------------------------------
    # Private helpers (replay-safe — only call activities)
    # -----------------------------------------------------------------

    async def _run_docker_chain(
        self,
        inp: ExecutionRunWorkflowInput,
        *,
        command: str,
        resolved_runner_id: str | None,
        resolved_vault_path: str | None,
    ) -> dict[str, Any]:
        """Run the structured Docker pipeline (EK2 fix).

        Sequence: ``docker_daemon_healthcheck`` → ``docker_build_image``
        → ``docker_run_container`` → ``docker_cleanup_container``. Each
        step is a registered Temporal activity (see
        ``workers/execution-runner-worker/src/activities/docker.py``).

        The result is shaped to match :func:`ssh_run_test`'s return so
        the rest of the workflow body (status mapping, cleanup policy
        application, output_actions hook) stays unchanged.
        """
        # Dataclass shapes already imported at module level inside
        # ``workflow.unsafe.imports_passed_through()`` — no per-call
        # import needed.
        workflow.logger.info(
            "ExecutionRunWorkflow._run_docker_chain: needs_docker=True "
            "image_tag=%s dockerfile=%s workdir=%s",
            inp.docker_image_tag,
            inp.docker_dockerfile_path,
            inp.workdir,
        )

        parent_id = inp.parent_workflow_id or ""
        image_tag = inp.docker_image_tag or (
            f"ai-bot-{parent_id.lower()}:latest"
        )
        dockerfile_path = inp.docker_dockerfile_path or "Dockerfile"
        workspace_path = inp.workdir or ""

        # Step 1 — docker daemon healthcheck. If the runner has no docker
        # available we short-circuit with a structured failure so the
        # caller surfaces a clear error instead of a build crash.
        try:
            daemon_ok: bool = await workflow.execute_activity(
                "docker_daemon_healthcheck",
                args=[
                    DockerHealthcheckInput(
                        workflow_id=parent_id,
                        runner_id=resolved_runner_id,
                        vault_path=resolved_vault_path,
                    )
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except Exception:  # noqa: BLE001
            daemon_ok = False
        if not daemon_ok:
            return {
                "status": "failed",
                "exit_code": None,
                "stdout_uri": None,
                "stderr_uri": None,
                "duration_seconds": 0.0,
                "runner_id": resolved_runner_id,
                "failure_reason": "docker_daemon_unavailable",
            }

        # Step 2 — build the image.
        build_result = await workflow.execute_activity(
            "docker_build_image",
            args=[
                DockerBuildInput(
                    dockerfile_path=dockerfile_path,
                    image_tag=image_tag,
                    workspace_path=workspace_path,
                    workflow_id=parent_id,
                    runner_id=resolved_runner_id,
                    vault_path=resolved_vault_path,
                )
            ],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        if not _activity_result_field(build_result, "success", False):
            return {
                "status": "failed",
                "exit_code": None,
                "stdout_uri": None,
                "stderr_uri": None,
                "duration_seconds": float(
                    _activity_result_field(
                        build_result, "duration_seconds", 0.0
                    )
                ),
                "runner_id": resolved_runner_id,
                "failure_reason": "docker_build_failed",
            }

        image_id = _activity_result_field(build_result, "image_id")
        container_id: str | None = None
        try:
            # Step 3 — run the container with the supplied command. The
            # ``environment`` tuple is normalised to a dict for the
            # docker activity input shape (which uses dict[str, str]).
            env_dict: dict[str, str] = (
                dict(inp.environment) if inp.environment else {}
            )
            run_result = await workflow.execute_activity(
                "docker_run_container",
                args=[
                    DockerRunInput(
                        image=image_tag,
                        command=command or 'echo "no command supplied"',
                        workspace_path=workspace_path,
                        environment=env_dict,
                        workflow_id=parent_id,
                        runner_id=resolved_runner_id,
                        vault_path=resolved_vault_path,
                    )
                ],
                start_to_close_timeout=timedelta(minutes=35),
                retry_policy=_RUN_TEST_RETRY_POLICY,
            )
            container_id = (
                _activity_result_field(run_result, "container_id") or None
            )
            exit_code: int | None = _activity_result_field(
                run_result, "exit_code"
            )
            log_uri: str | None = _activity_result_field(
                run_result, "log_artifact_uri"
            )
            status: str = "passed" if exit_code == 0 else "failed"
            failure_reason = None if status == "passed" else "non_zero_exit"
            result: dict[str, Any] = {
                "status": status,
                "exit_code": exit_code,
                "stdout_uri": log_uri,
                "stderr_uri": log_uri,
                "duration_seconds": float(
                    _activity_result_field(
                        build_result, "duration_seconds", 0.0
                    )
                ),
                "runner_id": resolved_runner_id,
                "failure_reason": failure_reason,
            }
        finally:
            # Step 4 — cleanup runs unconditionally (always reach here
            # because ``finally``) — honours the cleanup policy carried
            # in the environment tuple (defaults to ``on_success``).
            cleanup_policy_value: str = "on_success"
            if inp.environment:
                cleanup_policy_value = dict(inp.environment).get(
                    "CLEANUP_POLICY", "on_success"
                )
            try:
                await workflow.execute_activity(
                    "docker_cleanup_container",
                    args=[
                        DockerCleanupInput(
                            container_id=container_id or "",
                            image_id=image_id,
                            policy=cleanup_policy_value,  # type: ignore[arg-type]
                            task_succeeded=(result.get("status") == "passed"
                                            if "result" in locals() else False),
                            workflow_id=parent_id,
                            runner_id=resolved_runner_id,
                            vault_path=resolved_vault_path,
                        )
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup
                workflow.logger.warning(
                    "docker_cleanup_container failed (best-effort)",
                    exc_info=True,
                )

        return result

    async def _wait_for_healthy_runner(
        self,
        inp: ExecutionRunWorkflowInput,
        *,
        runner_id: str | None,
        host: str | None,
        port: int | None,
    ) -> bool:
        """Probe the SSH runner with exponential backoff.

        Returns ``True`` when the runner is reachable, ``False`` when
        all retries are exhausted.
        """
        backoff = _HEALTHCHECK_INITIAL_BACKOFF
        for attempt in range(_HEALTHCHECK_MAX_RETRIES + 1):
            try:
                hc_result: dict[str, Any] = await workflow.execute_activity(
                    "ssh_healthcheck",
                    args=[
                        SSHHealthcheckInput(
                            host=host,
                            port=port,
                            runner_id=runner_id,
                        )
                    ],
                    start_to_close_timeout=_HEALTHCHECK_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except Exception:  # noqa: BLE001
                # Activity itself failed (e.g. worker crashed) — treat
                # as unhealthy and retry.
                hc_result = {"healthy": False, "error": "activity_failed"}

            if hc_result.get("healthy", False):
                if attempt > 0:
                    workflow.logger.info(
                        "ExecutionRunWorkflow: runner recovered after "
                        "%d retries",
                        attempt,
                    )
                return True

            # Runner is unhealthy.
            host = hc_result.get("host", "unknown")
            error = hc_result.get("error", "unknown")
            workflow.logger.warning(
                "ExecutionRunWorkflow: ssh_healthcheck FAILED "
                "(attempt %d/%d) host=%s error=%s — "
                "retrying in %s",
                attempt + 1,
                _HEALTHCHECK_MAX_RETRIES + 1,
                host,
                error,
                backoff,
            )

            if attempt >= _HEALTHCHECK_MAX_RETRIES:
                break

            # Wait with exponential backoff before next probe.
            await workflow.sleep(backoff)
            backoff = min(
                timedelta(
                    seconds=backoff.total_seconds()
                    * _HEALTHCHECK_BACKOFF_COEFFICIENT
                ),
                _HEALTHCHECK_MAX_WAIT,
            )

        return False

    async def _enforce_disk_quota(
        self, inp: ExecutionRunWorkflowInput
    ) -> None:
        """Invoke the ``check_disk_quota`` activity gate (R16.1, R16.2).

        The gate is fully opt-in: when
        :attr:`ExecutionRunWorkflowInput.workspace_quota_mb` is ``None``
        the method short-circuits without invoking the activity, so the
        canonical workflow keeps its existing observable behaviour for
        every legacy call site.

        When a cap is supplied:

        * The base path passed to the activity is derived from
          :attr:`~ExecutionRunWorkflowInput.workdir` — its parent
          directory is used as the quota measurement root because the
          ``workdir`` already includes the per-task subdirectory and we
          want to measure the department-level usage.  When ``workdir``
          is ``None`` the workflow falls back to the empty string, which
          the activity translates into ``du -sm`` against the env-derived
          ``RUNNER_BASE_PATH`` on the runner side.
        * The :attr:`~ExecutionRunWorkflowInput.department_id` field
          must be non-empty; otherwise the call is skipped because the
          activity uses it for warning deduplication and an empty value
          would short-circuit the dedup table.
        * The activity returns ``allowed=False`` only when the runner
          reports usage strictly above the cap.  In that case we raise
          :class:`ApplicationError` with ``type="DiskQuotaExceeded"``
          and ``non_retryable=True`` so the parent
          :class:`AutomationWorkflow` can surface a Jira comment without
          retrying the workflow against an already-full disk.
        * The activity is best-effort on SSH failure (returns
          ``allowed=True`` with an ``error`` field) — we log the
          diagnostic but do **not** block the run, matching the
          contract pinned in ``activities/disk_quota.py``.
        """

        if inp.workspace_quota_mb is None:
            return
        if not inp.department_id:
            workflow.logger.warning(
                "ExecutionRunWorkflow: workspace_quota_mb=%s set but "
                "department_id is empty — skipping disk-quota gate "
                "(activity needs dept_id for warning dedup)",
                inp.workspace_quota_mb,
            )
            return

        # Resolve the quota measurement base.  The runner-side activity
        # uses ``du -sm <workspace_base>`` so we want the parent of the
        # per-task workdir, not the workdir itself.  Empty / None falls
        # through to the activity, which will measure the env-derived
        # ``RUNNER_BASE_PATH`` (best-effort default).
        workspace_base = _resolve_quota_base(inp.workdir)

        try:
            quota_result: DiskQuotaResult = await workflow.execute_activity(
                "check_disk_quota",
                args=[
                    DiskQuotaInput(
                        dept_id=inp.department_id,
                        workspace_base=workspace_base,
                        quota_mb=float(inp.workspace_quota_mb),
                    )
                ],
                result_type=DiskQuotaResult,
                start_to_close_timeout=_DISK_QUOTA_TIMEOUT,
                retry_policy=_DISK_QUOTA_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001 — best-effort gate
            # Activity infrastructure failure (e.g. worker crash before
            # the activity body runs).  Log and allow — we'd rather
            # surface a real downstream SSH failure than wedge every
            # run on a flaky check.
            workflow.logger.warning(
                "ExecutionRunWorkflow: check_disk_quota activity raised "
                "(best-effort, allowing run to proceed)",
                exc_info=True,
            )
            return

        if not quota_result.allowed:
            workflow.logger.error(
                "ExecutionRunWorkflow: disk quota exceeded for dept=%s "
                "usage=%.1fMB cap=%.1fMB — failing run before SSH",
                inp.department_id,
                quota_result.usage_mb,
                quota_result.quota_mb or 0.0,
            )
            raise ApplicationError(
                (
                    "workspace disk quota exceeded: "
                    f"usage={quota_result.usage_mb}MB "
                    f"cap={quota_result.quota_mb}MB "
                    f"dept={inp.department_id}"
                ),
                type="DiskQuotaExceeded",
                non_retryable=True,
            )

        if quota_result.error:
            # Activity reported an error but allowed the run (best-effort
            # path — SSH probe failed).  Log so an operator can correlate
            # later disk-full incidents with the gate's blind spot.
            workflow.logger.warning(
                "ExecutionRunWorkflow: check_disk_quota allowed run "
                "despite probe error (dept=%s error=%s)",
                inp.department_id,
                quota_result.error,
            )

    async def _apply_cleanup_policy(
        self,
        inp: ExecutionRunWorkflowInput,
        exit_code: int | None,
        *,
        resolved_vault_path: str | None,
    ) -> None:
        """Invoke the cleanup policy activity (best-effort).

        The cleanup policy is read from the workflow input's
        environment tuple (key ``CLEANUP_POLICY``). When absent,
        defaults to ``"on_success"`` which matches the legacy
        behaviour.
        """
        # Extract cleanup_policy from environment if present.
        cleanup_policy = "on_success"
        if inp.environment:
            env_dict = dict(inp.environment)
            cleanup_policy = env_dict.get(
                "CLEANUP_POLICY",
                env_dict.get("cleanup_policy", "on_success"),
            )

        # Extract optional container/image IDs from environment.
        env_dict = dict(inp.environment) if inp.environment else {}
        container_id = env_dict.get("CONTAINER_ID")
        image_id = env_dict.get("IMAGE_ID")

        try:
            await workflow.execute_activity(
                "apply_cleanup_policy",
                args=[
                    cleanup_policy,
                    exit_code,
                    inp.workdir or "",
                    container_id,
                    image_id,
                    inp.parent_workflow_id,
                    inp.department_id,
                    resolved_vault_path,
                ],
                start_to_close_timeout=_CLEANUP_TIMEOUT,
                retry_policy=_CLEANUP_RETRY_POLICY,
            )
        except Exception:  # noqa: BLE001
            # Cleanup is best-effort — log and continue.
            workflow.logger.warning(
                "ExecutionRunWorkflow: apply_cleanup_policy activity "
                "failed (best-effort, continuing)",
                exc_info=True,
            )

    async def _resolve_runner(
        self, dept_id: str
    ) -> dict[str, Any] | None:
        """Invoke the ``resolve_runner`` activity (Requirements 4.4, 4.5, 4.8).

        Selects the least-busy SSH runner assigned to the given
        department from the ``infrastructure.ssh_runners`` table.  The
        resolved runner's ``vault_path`` follows the dual-slot rotation
        pattern (``vault:ssh/runners/{runner_id}/active``) so the
        existing SSH key rotation flow continues working unchanged.

        When the activity raises :class:`RunnerResolutionError` (no
        active runner assigned), the workflow fails with
        ``failure_reason="no_runner_assigned"`` — the activity itself
        writes the ``no_runner_assigned_to_dept`` audit event.

        Returns ``None`` only on unexpected infrastructure failures
        (e.g. worker crash before the activity body runs) — in that
        case the workflow falls back to the input ``runner_id`` so
        existing deployments with a single runner keep working.

        Parameters
        ----------
        dept_id:
            Department identifier to resolve a runner for.

        Returns
        -------
        dict[str, Any] | None
            Serialized :class:`RunnerResolution` dict with keys
            ``runner_id``, ``host``, ``port``, ``username``,
            ``vault_path``.  ``None`` on infrastructure failure
            (fallback to input runner_id).
        """
        from temporalio.exceptions import ActivityError

        try:
            resolution: dict[str, Any] = await workflow.execute_activity(
                "resolve_runner",
                args=[dept_id],
                start_to_close_timeout=_RUNNER_RESOLVER_TIMEOUT,
                retry_policy=_RUNNER_RESOLVER_RETRY_POLICY,
            )
            return resolution
        except ActivityError as exc:
            # Temporal wraps activity exceptions in ActivityError.
            # If the cause is an ApplicationError with a known type
            # (e.g. "RunnerResolutionError" — no active runner assigned),
            # re-raise so the workflow fails with a clear reason.
            # Other causes (generic RuntimeError wrapped by Temporal,
            # timeout, cancellation) are treated as infrastructure
            # failures — fall back to input runner_id.
            if exc.cause and isinstance(exc.cause, ApplicationError):
                cause_type = getattr(exc.cause, "type", None)
                if cause_type == "RunnerResolutionError":
                    raise exc.cause from exc
            # Infrastructure failure — fall back gracefully.
            workflow.logger.warning(
                "ExecutionRunWorkflow: resolve_runner activity failed "
                "with ActivityError (falling back to input runner_id)",
                exc_info=True,
            )
            return None
        except Exception:  # noqa: BLE001
            # Infrastructure failure (worker crash, network partition).
            # Log and return None so the workflow can fall back to the
            # input runner_id (backward compatibility with single-runner
            # deployments).
            workflow.logger.warning(
                "ExecutionRunWorkflow: resolve_runner activity failed "
                "(falling back to input runner_id)",
                exc_info=True,
            )
            return None


# ---------------------------------------------------------------------------
# Legacy I/O dataclasses (unchanged — kept for backwards compatibility)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionRunInput:
    """Input parameters for the legacy ExecutionRunWorkflow scaffold.

    Attributes
    ----------
    workflow_id : str
        The unique workflow ID (e.g. ``exec-agent-...-1700000000``).
    test_command : str
        Shell command to execute on the remote host.
    workspace_path : str
        Remote directory to use as working directory for the command.
    cleanup_policy : str
        One of ``"always"``, ``"on_success"``, ``"never"``.
    timeout_minutes : int
        Maximum execution time for the SSH command (default 30).
    git_push_required : bool
        When ``True`` (and ``dept_id`` / ``git_push_branch`` are set) the
        workflow injects Bitbucket credentials via Vault, runs
        ``git push origin <branch>`` over SSH, and guarantees credential
        cleanup in a ``try/finally`` block.  Defaults to ``False`` so
        existing call sites keep their pre-credential-injection
        behaviour verbatim.
    git_push_branch : str | None
        Branch name to push to ``origin``.  Required when
        ``git_push_required=True``; ignored otherwise.
    dept_id : str | None
        Department slug used to construct the Vault path
        (``atlassian/{dept_id}/bitbucket``).  Required when
        ``git_push_required=True``; ignored otherwise.
    """

    workflow_id: str
    test_command: str
    workspace_path: str
    cleanup_policy: str = "on_success"
    timeout_minutes: int = 30
    # Optional Bitbucket push fields (platform-completion task 26.x —
    # Requirements 2.1-2.7).  Together they enable the credential-
    # injection / push / cleanup sub-flow inside ``run``.  All three
    # default to "no push" so the legacy call sites in
    # ``tests/integration/test_execution_runner.py`` keep green.
    git_push_required: bool = False
    git_push_branch: str | None = None
    dept_id: str | None = None
    # Optional disk-quota gate (Requirements 16.1, 16.2).  When both
    # ``dept_id`` and ``workspace_quota_mb`` are populated the workflow
    # invokes ``check_disk_quota`` after Vault credentials are fetched
    # but before any SSH command runs.  ``None`` (the default) skips
    # the gate entirely so existing call sites keep their pre-quota
    # observable behaviour verbatim.  Sourced from
    # ``departments.json::ssh_workspace_quota_mb`` by the dispatch
    # layer.
    workspace_quota_mb: float | None = None


@dataclass(frozen=True)
class ExecutionRunResult:
    """Result of the legacy ExecutionRunWorkflow scaffold.

    Attributes
    ----------
    exit_code : int
        The process exit code from the remote command.
    stdout_artifact_key : str
        MinIO key for the uploaded stdout log.
    stderr_artifact_key : str
        MinIO key for the uploaded stderr log.
    exit_code_artifact_key : str
        MinIO key for the uploaded exit_code file.
    cleanup_performed : bool
        Whether workspace cleanup was executed.
    """

    exit_code: int
    stdout_artifact_key: str
    stderr_artifact_key: str
    exit_code_artifact_key: str
    cleanup_performed: bool


# ---------------------------------------------------------------------------
# Legacy retry policies
# ---------------------------------------------------------------------------

# SSH connect retry: 3 attempts with exponential backoff (Requirement 8.6)
_SSH_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)

# MinIO upload retry: 3 attempts with exponential backoff
_MINIO_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)

# Vault fetch retry: 3 attempts
_VAULT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Git push credential-injection retry policies (R2.1-R2.7)
# ---------------------------------------------------------------------------

#: Retry policy for ``inject_git_credentials``.  Two attempts mirror the
#: Vault fetch retry budget already enforced inside the activity body
#: (Requirement 2.4 — max 2 retries with 5s backoff).  Adding a wrapping
#: Temporal retry on top of the in-activity retry would multiply
#: attempts unnecessarily, so we cap workflow-level retries at 2.
_INJECT_CREDENTIAL_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=2,
)

#: Retry policy for ``cleanup_git_credentials``.  Single attempt —
#: cleanup is best-effort (Property 4: cleanup guarantee).  The
#: credential cache has a TTL that will eventually purge anything left
#: behind, so a flaky retry on shutdown does not justify the extra
#: latency.
_CLEANUP_CREDENTIAL_RETRY_POLICY: RetryPolicy = RetryPolicy(
    maximum_attempts=1,
)

#: ``start_to_close_timeout`` for ``inject_git_credentials``.  The
#: activity does Vault fetch (30s) + SSH config (~5-10s); 2 minutes is
#: a generous upper bound that still surfaces a stuck activity quickly.
_INJECT_CREDENTIAL_TIMEOUT: timedelta = timedelta(minutes=2)

#: ``start_to_close_timeout`` for ``cleanup_git_credentials``.  The
#: cleanup activity itself enforces a 5s SSH window — 10s here gives
#: headroom for transient connection setup without leaving the runner
#: blocked on a stuck cleanup.
_CLEANUP_CREDENTIAL_TIMEOUT: timedelta = timedelta(seconds=10)

#: ``start_to_close_timeout`` for the ``git push`` SSH activity.  The
#: push itself is bounded by the ssh_connect_and_run activity timeout
#: (5 min) — most pushes complete in seconds, but a slow LAN or large
#: pack file can stretch into the minute range.  We allow up to 5
#: minutes before declaring the push stalled.
_GIT_PUSH_TIMEOUT: timedelta = timedelta(minutes=5)

#: Per-attempt timeout (in *minutes*, the unit accepted by
#: ``ssh_connect_and_run``) for the ``git push`` command.  Mirrors
#: :data:`_GIT_PUSH_TIMEOUT` — 5 minutes.
_GIT_PUSH_TIMEOUT_MINUTES: int = 5


# ---------------------------------------------------------------------------
# Legacy workflow definition (foundation scaffold — unchanged behaviour)
# ---------------------------------------------------------------------------


@workflow.defn(name="LegacyExecutionRunWorkflow")
class LegacyExecutionRunWorkflow:
    """Legacy scaffold workflow that runs tests on a remote SSH host.

    Flow:
        vault_fetch_ssh_credentials
        → ssh_connect_and_run (30min timeout, 3x retry)
        → minio_upload_artifact (stdout.log, stderr.log, exit_code.txt)
        → should_cleanup(policy, exit_code) → ssh_cleanup (if True)

    The workflow is deterministic: all I/O is encapsulated in activities.
    Time is obtained via ``workflow.now()``.

    .. note::

        This class predates the :class:`ExecutionRunWorkflow` canonical
        contract introduced by ``platform-mimari-workflows`` task 2.3.
        It is kept under a Temporal-distinct name
        (``LegacyExecutionRunWorkflow``) so the integration test
        ``tests/integration/test_execution_runner.py`` (foundation
        spec) keeps green while the canonical workflow uses the
        ``"ExecutionRunWorkflow"`` Temporal name.
    """

    @workflow.run
    async def run(self, inp: ExecutionRunInput) -> ExecutionRunResult:
        """Execute the full test-run lifecycle.

        Parameters
        ----------
        inp : ExecutionRunInput
            Workflow input containing command, workspace path, cleanup
            policy, and timeout configuration.

        Returns
        -------
        ExecutionRunResult
            Result containing exit code, artifact keys, and cleanup status.
        """
        workflow.logger.info(
            "LegacyExecutionRunWorkflow started: workflow_id=%s, command=%s",
            inp.workflow_id,
            inp.test_command,
        )

        # Step 1: Fetch SSH credentials from Vault (Requirement 8.1)
        ssh_cred: dict[str, Any] = await workflow.execute_activity(
            "vault_fetch_ssh_credentials",
            args=[inp.workflow_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_VAULT_RETRY_POLICY,
        )

        workflow.logger.info(
            "SSH credentials fetched for host=%s", ssh_cred["host"]
        )

        # Step 1.5: Workspace disk-quota gate (Requirements 16.1, 16.2)
        # ---------------------------------------------------------------
        # Before launching the (non-idempotent) test command, ask the
        # runner whether the department workspace base is already at or
        # above its quota.  Opt-in: when either ``workspace_quota_mb``
        # or ``dept_id`` is unset the gate is skipped entirely so the
        # legacy contract (and every existing integration test) is
        # preserved verbatim.
        if (
            inp.workspace_quota_mb is not None
            and inp.dept_id
        ):
            quota_base = _resolve_quota_base(inp.workspace_path)
            try:
                quota_result: DiskQuotaResult = (
                    await workflow.execute_activity(
                        "check_disk_quota",
                        args=[
                            DiskQuotaInput(
                                dept_id=inp.dept_id,
                                workspace_base=quota_base,
                                quota_mb=float(inp.workspace_quota_mb),
                            )
                        ],
                        result_type=DiskQuotaResult,
                        start_to_close_timeout=_DISK_QUOTA_TIMEOUT,
                        retry_policy=_DISK_QUOTA_RETRY_POLICY,
                    )
                )
            except Exception:  # noqa: BLE001 — best-effort gate
                workflow.logger.warning(
                    "LegacyExecutionRunWorkflow: check_disk_quota "
                    "activity raised (best-effort, allowing run)",
                    exc_info=True,
                )
                quota_result = None  # type: ignore[assignment]

            if quota_result is not None and not quota_result.allowed:
                workflow.logger.error(
                    "LegacyExecutionRunWorkflow: disk quota exceeded "
                    "for dept=%s usage=%.1fMB cap=%.1fMB — failing "
                    "before SSH",
                    inp.dept_id,
                    quota_result.usage_mb,
                    quota_result.quota_mb or 0.0,
                )
                raise ApplicationError(
                    (
                        "workspace disk quota exceeded: "
                        f"usage={quota_result.usage_mb}MB "
                        f"cap={quota_result.quota_mb}MB "
                        f"dept={inp.dept_id}"
                    ),
                    type="DiskQuotaExceeded",
                    non_retryable=True,
                )

        # Step 2: Connect via SSH and run the test command (Requirement 8.2, 8.6, 8.8)
        # Retry policy handles transient SSH connection failures (3x exponential).
        # start_to_close_timeout=30min enforces the maximum execution time.
        run_result: dict[str, Any] = await workflow.execute_activity(
            "ssh_connect_and_run",
            args=[ssh_cred, inp.test_command, inp.workspace_path, inp.timeout_minutes],
            start_to_close_timeout=timedelta(minutes=30),
            retry_policy=_SSH_RETRY_POLICY,
        )

        stdout: str = run_result["stdout"]
        stderr: str = run_result["stderr"]
        exit_code: int = run_result["exit_code"]

        workflow.logger.info(
            "SSH command completed: exit_code=%d, stdout_len=%d, stderr_len=%d",
            exit_code,
            len(stdout),
            len(stderr),
        )

        # Step 2.5: Optional Bitbucket git push with credential injection
        # ---------------------------------------------------------------
        # When the caller marks the run as ``git_push_required=True`` we
        # fetch a TTL-scoped Bitbucket credential from Vault, configure
        # ``git credential.helper cache`` on the SSH runner, run the
        # push, then **always** clean the credential up (Property 4 —
        # credential cleanup guarantee).  The cleanup runs from a
        # ``finally`` block so it fires whether the push succeeded,
        # failed, or the inject step itself raised.
        #
        # All three of ``git_push_required`` / ``dept_id`` /
        # ``git_push_branch`` must be set; otherwise the block is a
        # no-op and the legacy artifact-upload flow continues
        # untouched.  Determinism: we use ``workflow.info().workflow_id``
        # (replay-safe) — never ``os.environ`` or ``time.time()``.
        if (
            inp.git_push_required
            and inp.dept_id
            and inp.git_push_branch
        ):
            await self._inject_push_and_cleanup(
                ssh_cred=ssh_cred,
                dept_id=inp.dept_id,
                branch=inp.git_push_branch,
                workspace_path=inp.workspace_path,
            )

        # Step 3: Upload artifacts to MinIO (Requirement 8.3)
        # Artifact keys follow the format: executions/{workflow_id}/{name}
        stdout_key = execution_artifact_key(inp.workflow_id, "stdout.log")
        stderr_key = execution_artifact_key(inp.workflow_id, "stderr.log")
        exit_code_key = execution_artifact_key(inp.workflow_id, "exit_code.txt")

        await workflow.execute_activity(
            "minio_upload_artifact",
            args=[DEFAULT_BUCKET, stdout_key, stdout.encode("utf-8")],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_MINIO_RETRY_POLICY,
        )

        await workflow.execute_activity(
            "minio_upload_artifact",
            args=[DEFAULT_BUCKET, stderr_key, stderr.encode("utf-8")],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_MINIO_RETRY_POLICY,
        )

        await workflow.execute_activity(
            "minio_upload_artifact",
            args=[DEFAULT_BUCKET, exit_code_key, str(exit_code).encode("utf-8")],
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=_MINIO_RETRY_POLICY,
        )

        workflow.logger.info(
            "Artifacts uploaded: stdout=%s, stderr=%s, exit_code=%s",
            stdout_key,
            stderr_key,
            exit_code_key,
        )

        # Step 4: Conditional cleanup based on policy and exit code (Requirement 8.4, 8.5)
        # Uses the pure should_cleanup helper (deterministic, no I/O).
        cleanup_performed = False
        policy: CleanupPolicy = inp.cleanup_policy  # type: ignore[assignment]

        if should_cleanup(policy, exit_code):
            workflow.logger.info(
                "Cleanup triggered: policy=%s, exit_code=%d",
                inp.cleanup_policy,
                exit_code,
            )
            # ssh_cleanup is best-effort — the activity itself swallows
            # exceptions internally. We also catch here to prevent workflow
            # failure from a cleanup issue.
            try:
                await workflow.execute_activity(
                    "ssh_cleanup",
                    args=[ssh_cred, inp.workspace_path],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                cleanup_performed = True
            except Exception:  # noqa: BLE001
                workflow.logger.warning(
                    "ssh_cleanup failed (best-effort), continuing",
                    exc_info=True,
                )
        else:
            workflow.logger.info(
                "Cleanup skipped: policy=%s, exit_code=%d",
                inp.cleanup_policy,
                exit_code,
            )

        workflow.logger.info(
            "LegacyExecutionRunWorkflow completed: exit_code=%d, cleanup=%s",
            exit_code,
            cleanup_performed,
        )

        return ExecutionRunResult(
            exit_code=exit_code,
            stdout_artifact_key=stdout_key,
            stderr_artifact_key=stderr_key,
            exit_code_artifact_key=exit_code_key,
            cleanup_performed=cleanup_performed,
        )

    # -----------------------------------------------------------------
    # Bitbucket git-push helper (Requirements 2.1-2.7, Property 4)
    # -----------------------------------------------------------------

    async def _inject_push_and_cleanup(
        self,
        *,
        ssh_cred: dict[str, Any],
        dept_id: str,
        branch: str,
        workspace_path: str,
    ) -> None:
        """Inject a Bitbucket credential, run ``git push``, then clean up.

        Implements the credential-injection sub-flow:

        1. ``inject_git_credentials`` — fetch from Vault and configure
           ``git credential.helper cache --timeout=900`` on the SSH
           runner (15 minute TTL).
        2. ``ssh_connect_and_run`` — execute
           ``git push origin <branch>`` from the workspace directory.
        3. ``cleanup_git_credentials`` — **always** runs from the
           ``finally`` block (Property 4: cleanup guarantee).  Cleanup
           failures are swallowed because the credential cache has a
           TTL that will purge anything left behind.

        Determinism contract:

        * Uses :func:`workflow.info().workflow_id` (replay-safe).
        * No direct ``os.environ`` / ``time.time()`` access — all I/O
          happens inside the registered activities.
        * The cleanup activity is invoked unconditionally (even when
          inject raises) to honour Property 4.

        Parameters
        ----------
        ssh_cred:
            Output of :func:`vault_fetch_ssh_credentials` — passed to
            ``ssh_connect_and_run`` so the push runs on the same host
            that ran the original test command.
        dept_id:
            Department slug used to build the Vault path
            (``atlassian/{dept_id}/bitbucket``).
        branch:
            Branch name passed verbatim to ``git push origin``.  The
            workflow does **not** quote this value — the integration
            layer that builds :class:`ExecutionRunInput` is
            responsible for ensuring it matches the
            ``[A-Za-z0-9._/-]+`` pattern accepted by git.
        workspace_path:
            Working directory on the runner where ``git push`` runs.
        """

        push_workflow_id = workflow.info().workflow_id
        push_command = f"git push origin {branch}"

        workflow.logger.info(
            "git push requested: dept=%s branch=%s workspace=%s "
            "workflow_id=%s",
            dept_id,
            branch,
            workspace_path,
            push_workflow_id,
        )

        try:
            # Step 1 — inject credential.  Failures here surface as
            # ApplicationError so the workflow fails before the push
            # is attempted.  The ``finally`` block still runs the
            # cleanup activity, mirroring Property 4.
            inject_result: CredentialInjectResult = (
                await workflow.execute_activity(
                    "inject_git_credentials",
                    args=[
                        CredentialInjectInput(
                            dept_id=dept_id,
                            workflow_id=push_workflow_id,
                            ttl_minutes=15,
                        )
                    ],
                    result_type=CredentialInjectResult,
                    start_to_close_timeout=_INJECT_CREDENTIAL_TIMEOUT,
                    retry_policy=_INJECT_CREDENTIAL_RETRY_POLICY,
                )
            )

            if not inject_result.success:
                workflow.logger.error(
                    "inject_git_credentials returned success=False: %s",
                    inject_result.error,
                )
                raise ApplicationError(
                    (
                        "git credential injection failed for "
                        f"dept={dept_id} workflow={push_workflow_id}: "
                        f"{inject_result.error}"
                    ),
                    type="GitCredentialInjectionFailed",
                    non_retryable=True,
                )

            workflow.logger.info(
                "git credential injected (user=%s) — running push",
                inject_result.masked_username,
            )

            # Step 2 — run the push over SSH.  We reuse
            # ``ssh_connect_and_run`` so the existing retry/heartbeat
            # plumbing applies; a single attempt is enough because
            # ``git push`` is not idempotent and re-running it after
            # a partial failure could double-publish refs.
            push_result: dict[str, Any] = await workflow.execute_activity(
                "ssh_connect_and_run",
                args=[
                    ssh_cred,
                    push_command,
                    workspace_path,
                    _GIT_PUSH_TIMEOUT_MINUTES,
                ],
                start_to_close_timeout=_GIT_PUSH_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

            push_exit = int(push_result.get("exit_code", -1))
            if push_exit != 0:
                workflow.logger.error(
                    "git push exited non-zero: exit_code=%d branch=%s",
                    push_exit,
                    branch,
                )
                raise ApplicationError(
                    (
                        f"git push origin {branch} failed with "
                        f"exit_code={push_exit}"
                    ),
                    type="GitPushFailed",
                    non_retryable=True,
                )

            workflow.logger.info(
                "git push succeeded: branch=%s exit_code=0", branch
            )
        finally:
            # Step 3 — cleanup runs unconditionally (Property 4).  We
            # swallow every exception class because the credential
            # cache has a TTL and best-effort cleanup is preferable
            # to surfacing a cleanup error that masks the real
            # push/inject failure above.
            try:
                await workflow.execute_activity(
                    "cleanup_git_credentials",
                    args=[push_workflow_id],
                    start_to_close_timeout=_CLEANUP_CREDENTIAL_TIMEOUT,
                    retry_policy=_CLEANUP_CREDENTIAL_RETRY_POLICY,
                )
                workflow.logger.info(
                    "git credentials cleaned up (workflow_id=%s)",
                    push_workflow_id,
                )
            except Exception:  # noqa: BLE001 — best-effort cleanup
                workflow.logger.warning(
                    "cleanup_git_credentials failed for workflow_id=%s "
                    "(best-effort, credentials will expire via TTL)",
                    push_workflow_id,
                    exc_info=True,
                )
