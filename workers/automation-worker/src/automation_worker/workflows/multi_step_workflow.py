"""``MultiStepWorkflow`` - sequential multi-step orchestrator.

Orchestrates complex tasks by splitting them into 2-20 sequential
child workflows. Each step runs as an independent child workflow;
the output of step N is passed as input to step N+1.

Responsibilities (design.md §5 "Multi-Step Orchestrator" and
-5.10):

1. Validate step count (2-20 inclusive).
2. Execute steps sequentially as child workflows.
3. Pass output from step N to step N+1.
4. Apply retry policy with exponential backoff (5s, 10s, 20s) on
 failure, max 3 retries step.
5. Track timing metadata (start_time, end_time, duration_seconds,
 output_summary capped at 500 chars) for each step.
6. Enforce per-step timeout (default 300s).
7. On all steps complete, execute output_actions via the
 ``execute_output_actions`` activity.
8. On failure after retries exhausted, post error report to Jira.

Additionally provides ``EpicSubtaskWorkflow`` - an Epic-aware
orchestrator that iterates an Epic's subtask list, starts a child
``AutomationWorkflow`` for each subtask, posts progress comments
to the parent Epic, and stops on first failure.

Determinism contract: The workflow body uses only Temporal-deterministic
primitives - ``workflow.now``, ``workflow.execute_activity``,
``workflow.start_child_workflow``. No ``random`` / ``uuid.uuid4`` /
``os.environ`` / direct I/O.

(2-20 step plan), **5.2** (independent
child workflow step), **5.3** (step state tracking), **5.4** (output
passing), **5.5** (retry with exponential backoff), **5.6** (error
report on exhausted retries), **5.7** (timing metadata), **5.8** (output
actions on completion), **5.9** (output passing failure → retry),
**5.10** (timeout enforcement), **12.3** (Epic subtask iteration with
progress comments), **12.4** (stop on subtask failure).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum number of steps allowed in a multi-step plan.
MIN_STEPS: Final[int] = 2

#: Maximum number of steps allowed in a multi-step plan.
MAX_STEPS: Final[int] = 20

#: Default timeout step in seconds.
DEFAULT_STEP_TIMEOUT_SECONDS: Final[int] = 300

#: Maximum retries step.
MAX_RETRIES_PER_STEP: Final[int] = 3

#: Exponential backoff intervals (seconds) for step retries.
RETRY_BACKOFF_INTERVALS: Final[tuple[int, ...]] = (5, 10, 20)

#: Maximum length for output_summary field.
MAX_OUTPUT_SUMMARY_LENGTH: Final[int] = 500

#: Activity name for posting Jira comments.
_ACT_JIRA_ADD_COMMENT: Final[str] = "jira_add_comment"

#: Activity name for executing output actions.
_ACT_EXECUTE_OUTPUT_ACTIONS: Final[str] = "execute_output_actions"

#: Default activity timeout for short operations.
_SHORT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: Default retry policy for activities.
_DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepDefinition:
    """Definition of a single step in a multi-step workflow.

 Attributes:
 name: Human-readable step name.
 workflow_type: The child workflow type to execute for this step.
 input_data: Input parameters for the child workflow.
 timeout_seconds: Maximum execution time for this step (default 300s).
 max_retries: Maximum retry attempts for this step (default 3).
 """

    name: str
    workflow_type: str
    input_data: dict[str, Any]
    timeout_seconds: int = DEFAULT_STEP_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES_PER_STEP


@dataclass
class StepResult:
    """Result of executing a single step.

 Attributes:
 step_name: Name of the step.
 status: Current state of the step.
 start_time: When the step started executing (ISO format string).
 end_time: When the step finished (ISO format string).
 duration_seconds: Total execution time in seconds.
 output_summary: Summary of step output (max 500 chars).
 error: Error message if the step failed.
 retry_count: Number of retry attempts made.
 """

    step_name: str
    status: Literal["pending", "running", "completed", "failed"]
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float | None = None
    output_summary: str = ""
    error: str | None = None
    retry_count: int = 0


@dataclass(frozen=True)
class MultiStepInput:
    """Input for the MultiStepWorkflow.

 Attributes:
 issue_key: The Jira issue key for context and error reporting.
 dept_id: Department identifier for credential resolution.
 steps: List of step definitions (2-20 steps).
 workflow_id: Parent workflow identifier for tracing.
 output_actions: Optional list of output actions to execute
 after all steps complete.
 """

    issue_key: str
    dept_id: str
    steps: list[StepDefinition]
    workflow_id: str
    output_actions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MultiStepResult:
    """Result of the MultiStepWorkflow.

 Attributes:
 success: Whether all steps completed successfully.
 step_results: Results for each step in execution order.
 failed_step: Name of the step that caused failure (if any).
 error: Overall error message (if workflow failed).
 total_duration_seconds: Total workflow execution time.
 """

    success: bool
    step_results: list[StepResult]
    failed_step: str | None = None
    error: str | None = None
    total_duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# MultiStepWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="MultiStepWorkflow")
class MultiStepWorkflow:
    """Sequential multi-step orchestrator workflow.

 Executes 2-20 steps sequentially as child workflows, passing
 output from each step to the next. Applies retry policy with
 exponential backoff on failure. Tracks timing metadata for each
 step. On completion, executes output_actions. On failure, posts
 error report to Jira..
 """

    @workflow.run
    async def run(self, inp: MultiStepInput) -> MultiStepResult:
        """Execute the multi-step workflow."""

        # 1. Validate step count 
        step_count = len(inp.steps)
        if step_count < MIN_STEPS or step_count > MAX_STEPS:
            error_msg = (
                f"Invalid step count: {step_count}. "
                f"Must be between {MIN_STEPS} and {MAX_STEPS}."
            )
            workflow.logger.error(
                "MultiStepWorkflow validation failed for %s: %s",
                inp.issue_key,
                error_msg,
            )
            await self._post_jira_comment(
                inp.issue_key,
                inp.dept_id,
                f"❌ Multi-step iş akışı başlatılamadı: {error_msg}",
            )
            return MultiStepResult(
                success=False,
                step_results=[],
                error=error_msg,
            )

        # Initialize step results (- pending state)
        step_results: list[StepResult] = [
            StepResult(step_name=step.name, status="pending")
            for step in inp.steps
        ]

        workflow_start = workflow.now()
        previous_output: dict[str, Any] = {}

        # 2. Execute steps sequentially 
        for idx, step_def in enumerate(inp.steps):
            step_result = await self._execute_step_with_retries(
                step_def=step_def,
                step_index=idx,
                inp=inp,
                previous_output=previous_output,
            )
            step_results[idx] = step_result

            # If step failed after all retries, stop workflow (Req 5.6)
            if step_result.status == "failed":
                workflow_end = workflow.now()
                total_duration = (
                    workflow_end - workflow_start
                ).total_seconds()

                # Post error report to Jira 
                await self._post_error_report(
                    inp=inp,
                    step_result=step_result,
                )

                return MultiStepResult(
                    success=False,
                    step_results=step_results,
                    failed_step=step_result.step_name,
                    error=step_result.error,
                    total_duration_seconds=total_duration,
                )

            # Pass output to next step 
            previous_output = self._extract_output(step_result)

        # 3. All steps completed - execute output_actions (Req 5.8)
        workflow_end = workflow.now()
        total_duration = (workflow_end - workflow_start).total_seconds()

        if inp.output_actions:
            await self._execute_output_actions(inp)

        return MultiStepResult(
            success=True,
            step_results=step_results,
            total_duration_seconds=total_duration,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_step_with_retries(
        self,
        *,
        step_def: StepDefinition,
        step_index: int,
        inp: MultiStepInput,
        previous_output: dict[str, Any],
    ) -> StepResult:
        """Execute a single step with retry policy.

 Applies exponential backoff (5s, 10s, 20s) on failure,
 up to max_retries attempts.
 Enforces per-step timeout.
 Records timing metadata.
 """

        retry_count = 0
        last_error: str | None = None

        while retry_count <= step_def.max_retries:
            # Mark step as running 
            start_time = workflow.now()

            try:
                # Build child workflow input by merging step input_data
                # with previous step output 
                child_input = {
                    **step_def.input_data,
                    "previous_step_output": previous_output,
                    "issue_key": inp.issue_key,
                    "dept_id": inp.dept_id,
                    "workflow_id": inp.workflow_id,
                    "step_name": step_def.name,
                    "step_index": step_index,
                }

                # Start child workflow with timeout (Req 5.2, 5.10)
                child_workflow_id = (
                    f"{inp.workflow_id}-step-{step_index}-"
                    f"attempt-{retry_count}"
                )

                result = await workflow.execute_child_workflow(
                    step_def.workflow_type,
                    args=[child_input],
                    id=child_workflow_id,
                    execution_timeout=timedelta(
                        seconds=step_def.timeout_seconds
                    ),
                )

                # Step completed successfully 
                end_time = workflow.now()
                duration = (end_time - start_time).total_seconds()

                output_summary = self._truncate_output_summary(
                    str(result) if result else ""
                )

                return StepResult(
                    step_name=step_def.name,
                    status="completed",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                    duration_seconds=duration,
                    output_summary=output_summary,
                    retry_count=retry_count,
                )

            except Exception as exc:  # noqa: BLE001
                end_time = workflow.now()
                duration = (end_time - start_time).total_seconds()
                last_error = str(exc)

                workflow.logger.warning(
                    "MultiStepWorkflow step '%s' (attempt %d/%d) "
                    "failed for %s: %s",
                    step_def.name,
                    retry_count + 1,
                    step_def.max_retries + 1,
                    inp.issue_key,
                    last_error,
                )

                # If we have retries left, apply exponential backoff
                # 
                if retry_count < step_def.max_retries:
                    backoff_idx = min(
                        retry_count, len(RETRY_BACKOFF_INTERVALS) - 1
                    )
                    backoff_seconds = RETRY_BACKOFF_INTERVALS[backoff_idx]
                    await workflow.sleep(timedelta(seconds=backoff_seconds))
                    retry_count += 1
                else:
                    # All retries exhausted 
                    return StepResult(
                        step_name=step_def.name,
                        status="failed",
                        start_time=start_time.isoformat(),
                        end_time=end_time.isoformat(),
                        duration_seconds=duration,
                        output_summary="",
                        error=last_error,
                        retry_count=retry_count,
                    )

        # Should not reach here, but defensive return
        return StepResult(
            step_name=step_def.name,
            status="failed",
            error=last_error or "Unknown error",
            retry_count=retry_count,
        )

    async def _post_error_report(
        self,
        *,
        inp: MultiStepInput,
        step_result: StepResult,
    ) -> None:
        """Post error report to Jira when a step fails after retries.

 Includes: failed step name, error reason, retry count, and
 last error output.
 """

        error_report = (
            f"❌ Multi-step iş akışı başarısız oldu.\n\n"
            f"• Başarısız adım: {step_result.step_name}\n"
            f"• Hata nedeni: {step_result.error or 'Bilinmeyen hata'}\n"
            f"• Deneme sayısı: {step_result.retry_count + 1}\n"
            f"• Son hata çıktısı: "
            f"{self._truncate_output_summary(step_result.error or '')}"
        )

        await self._post_jira_comment(
            inp.issue_key, inp.dept_id, error_report
        )

    async def _execute_output_actions(self, inp: MultiStepInput) -> None:
        """Execute output_actions via the Output_Action_Executor activity.

 Called when all steps complete successfully.
 """

        try:
            await workflow.execute_activity(
                _ACT_EXECUTE_OUTPUT_ACTIONS,
                args=[
                    {
                        "actions": inp.output_actions,
                        "issue_key": inp.issue_key,
                        "dept_id": inp.dept_id,
                        "workflow_id": inp.workflow_id,
                    }
                ],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "MultiStepWorkflow: execute_output_actions failed "
                "for %s: %s - continuing",
                inp.issue_key,
                exc,
            )

    async def _post_jira_comment(
        self, issue_key: str, dept_id: str, body: str
    ) -> None:
        """Post a Jira comment, swallowing failures (best-effort)."""

        try:
            await workflow.execute_activity(
                _ACT_JIRA_ADD_COMMENT,
                args=[issue_key, body, dept_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "MultiStepWorkflow: jira_add_comment failed for %s "
                "- continuing",
                issue_key,
            )

    @staticmethod
    def _truncate_output_summary(text: str) -> str:
        """Truncate output summary to MAX_OUTPUT_SUMMARY_LENGTH chars.: output_summary max 500 characters.
 """

        if len(text) <= MAX_OUTPUT_SUMMARY_LENGTH:
            return text
        return text[: MAX_OUTPUT_SUMMARY_LENGTH - 3] + "..."

    @staticmethod
    def _extract_output(step_result: StepResult) -> dict[str, Any]:
        """Extract output from a completed step for passing to next step.: pass output from step N to step N+1.
 """

        return {
            "step_name": step_result.step_name,
            "output_summary": step_result.output_summary,
            "duration_seconds": step_result.duration_seconds,
        }


# ---------------------------------------------------------------------------
# Epic Subtask Orchestration 
# ---------------------------------------------------------------------------

#: Default timeout for each subtask child workflow execution.
_EPIC_SUBTASK_TIMEOUT: Final[timedelta] = timedelta(minutes=30)

#: Activity name for writing audit events.
_ACT_WRITE_AUDIT: Final[str] = "write_audit_event"


@dataclass(frozen=True)
class EpicSubtaskDefinition:
    """A single subtask extracted from a Jira Epic.

 Attributes:
 issue_key: The Jira issue key of the subtask (e.g. ``PROJ-42``).
 summary: Human-readable summary/title of the subtask.
 """

    issue_key: str
    summary: str


@dataclass(frozen=True)
class EpicSubtaskInput:
    """Input for the ``EpicSubtaskWorkflow``.

 Attributes:
 epic_issue_key: The parent Epic's Jira issue key.
 dept_id: Department identifier for credential resolution.
 subtasks: List of subtask definitions to process sequentially.
 available_capabilities: Capabilities available for child workflows.
 available_repos: Repos available for child workflows.
 available_spaces: Confluence spaces available for child workflows.
 default_language: ISO-639-1 language code.
 trigger_event: Original trigger event type.
 trace_id: End-to-end correlation identifier.
 notify_on_success: Whether to notify on success.
 notify_channels: Notification channels.
 slack_webhook: Slack webhook URL (if configured).
 notify_email: Email address (if configured).
 """

    epic_issue_key: str
    dept_id: str
    subtasks: list[EpicSubtaskDefinition]
    available_capabilities: tuple[str, ...] = ()
    available_repos: tuple[str, ...] = ()
    available_spaces: tuple[str, ...] = ()
    default_language: str = "tr"
    trigger_event: str = "jira:issue_assigned"
    trace_id: str = ""
    notify_on_success: bool = False
    notify_channels: tuple[str, ...] = ()
    slack_webhook: str | None = None
    notify_email: str | None = None


@dataclass
class EpicSubtaskStepResult:
    """Result of processing a single Epic subtask.

 Attributes:
 issue_key: The subtask's Jira issue key.
 summary: The subtask's summary.
 status: Outcome of the subtask processing.
 error: Error message if the subtask failed.
 """

    issue_key: str
    summary: str
    status: Literal["completed", "failed", "skipped"]
    error: str | None = None


@dataclass(frozen=True)
class EpicSubtaskResult:
    """Result of the ``EpicSubtaskWorkflow``.

 Attributes:
 success: Whether all subtasks completed successfully.
 completed_count: Number of subtasks that completed.
 total_count: Total number of subtasks.
 step_results: Results for each subtask in execution order.
 failed_subtask_key: Issue key of the subtask that caused failure.
 error: Overall error message (if workflow failed).
 """

    success: bool
    completed_count: int
    total_count: int
    step_results: list[EpicSubtaskStepResult]
    failed_subtask_key: str | None = None
    error: str | None = None


@workflow.defn(name="EpicSubtaskWorkflow")
class EpicSubtaskWorkflow:
    """Epic subtask orchestrator - iterates subtasks sequentially.

 For each subtask in the Epic, starts a child ``AutomationWorkflow``
 and posts progress comments to the parent Epic. On first subtask
 failure, posts a failure comment and stops processing remaining
 subtasks.

 (Epic subtask iteration with progress
 comments), 12.4 (stop on subtask failure with fail comment).
 """

    @workflow.run
    async def run(self, inp: EpicSubtaskInput) -> EpicSubtaskResult:
        """Execute the Epic subtask orchestration workflow."""

        total = len(inp.subtasks)

        # Edge case: no subtasks (should not happen if task_analyzer
        # gates correctly, but defensive).
        if total == 0:
            await self._post_jira_comment(
                inp.epic_issue_key,
                inp.dept_id,
                "⚠️ Epic'te işlenecek subtask bulunamadı.",
            )
            return EpicSubtaskResult(
                success=False,
                completed_count=0,
                total_count=0,
                step_results=[],
                error="No subtasks to process.",
            )

        step_results: list[EpicSubtaskStepResult] = []
        completed_count = 0

        for idx, subtask in enumerate(inp.subtasks):
            # Start a child AutomationWorkflow for this subtask.
            child_workflow_id = (
                f"EpicSubtask-{inp.epic_issue_key}-"
                f"{subtask.issue_key}-{idx}"
            )

            try:
                await workflow.execute_child_workflow(
                    "AutomationWorkflow",
                    args=[
                        {
                            "issue_key": subtask.issue_key,
                            "department_id": inp.dept_id,
                            "available_capabilities": inp.available_capabilities,
                            "available_repos": inp.available_repos,
                            "available_spaces": inp.available_spaces,
                            "default_language": inp.default_language,
                            "trigger_event": inp.trigger_event,
                            "iteration": 1,
                            "trace_id": inp.trace_id,
                            "notify_on_success": inp.notify_on_success,
                            "notify_channels": inp.notify_channels,
                            "slack_webhook": inp.slack_webhook,
                            "notify_email": inp.notify_email,
                        }
                    ],
                    id=child_workflow_id,
                    execution_timeout=_EPIC_SUBTASK_TIMEOUT,
                )

                # Subtask completed successfully.
                completed_count += 1
                step_results.append(
                    EpicSubtaskStepResult(
                        issue_key=subtask.issue_key,
                        summary=subtask.summary,
                        status="completed",
                    )
                )

                # Post progress comment to parent Epic.
                progress_comment = (
                    f"🤖 {completed_count}/{total} subtask tamamlandı"
                )
                await self._post_jira_comment(
                    inp.epic_issue_key, inp.dept_id, progress_comment
                )

            except Exception as exc:  # noqa: BLE001
                # Subtask failed - post failure comment and stop
                #.
                error_msg = str(exc)
                step_results.append(
                    EpicSubtaskStepResult(
                        issue_key=subtask.issue_key,
                        summary=subtask.summary,
                        status="failed",
                        error=error_msg,
                    )
                )

                fail_comment = (
                    f"❌ Subtask {subtask.issue_key} fail oldu - "
                    f"Epic durduruldu"
                )
                await self._post_jira_comment(
                    inp.epic_issue_key, inp.dept_id, fail_comment
                )

                workflow.logger.warning(
                    "EpicSubtaskWorkflow: subtask %s failed for "
                    "Epic %s: %s - stopping remaining subtasks",
                    subtask.issue_key,
                    inp.epic_issue_key,
                    error_msg,
                )

                # Mark remaining subtasks as skipped.
                for remaining in inp.subtasks[idx + 1 :]:
                    step_results.append(
                        EpicSubtaskStepResult(
                            issue_key=remaining.issue_key,
                            summary=remaining.summary,
                            status="skipped",
                        )
                    )

                return EpicSubtaskResult(
                    success=False,
                    completed_count=completed_count,
                    total_count=total,
                    step_results=step_results,
                    failed_subtask_key=subtask.issue_key,
                    error=error_msg,
                )

        # All subtasks completed successfully.
        return EpicSubtaskResult(
            success=True,
            completed_count=completed_count,
            total_count=total,
            step_results=step_results,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post_jira_comment(
        self, issue_key: str, dept_id: str, body: str
    ) -> None:
        """Post a Jira comment to the Epic, swallowing failures (best-effort)."""

        try:
            await workflow.execute_activity(
                _ACT_JIRA_ADD_COMMENT,
                args=[issue_key, body, dept_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "EpicSubtaskWorkflow: jira_add_comment failed for %s "
                "- continuing",
                issue_key,
            )
