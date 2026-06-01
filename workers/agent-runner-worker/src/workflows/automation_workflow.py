"""Top-level :class:`AutomationWorkflow` Temporal workflow.

This workflow is the orchestrator for a single Jira- or Bitbucket-
triggered automation run. It is started by ``automation-service`` after
the webhook handler has accepted the payload (HMAC, replay, loop-guard
and capability Phase 1 already passed).

Responsibilities (design.md §"AutomationWorkflow İç Akışı")::

    1. Best-effort acknowledgement comment on the Jira issue.
    2. Fetch the issue via ``jira_get_issue``.
    3. Run ``llm_analyze_task`` to derive the workflow_type / output_actions.
    4. **Capability Phase 2** — verify the department has *all* services
       required by the chosen workflow_type. The check uses pure helpers
       from :mod:`temporal_shared.capabilities` against ``inp.available_capabilities``
       so it stays deterministic without an extra activity round-trip.
    5. If ``confidence == "low"`` and ``needs_info_question`` is non-empty,
       post the question, then ``workflow.wait_condition(...)`` for either
       a ``new_comment`` signal, a 7-day timeout, or a 3-iteration loop cap.
       On signal, increment the loop counter and re-run ``llm_analyze_task``
       with the appended comment. On timeout / loop cap exhaustion, post a
       failure comment and mark the work item ``failed``.
    6. Dispatch a child :class:`AgentRunnerWorkflow` (started by name to
       avoid an import-time coupling to the child class) with workflow_id
       ``agent_workflow_id(parent_id, 1)``. ``workflow.execute_child_workflow``
       is awaited so child failures propagate as exceptions.
    7. On child success, post a completion comment, transition the issue
       to ``Done`` and mark the work item ``completed``. On child failure,
       post a failure comment and mark the work item ``failed``.

Determinism contract (Property 11, design.md §"Determinism Sözleşmesi"):

* ``workflow.now()`` is the only time source.
* ``workflow.sleep(...)`` and ``workflow.wait_condition(...)`` are the only
  scheduling primitives.
* No ``random`` / ``uuid`` / ``os.environ`` / direct I/O — every side effect
  goes through an ``@activity.defn`` activity.
* Activity modules are imported under the
  ``workflow.unsafe.imports_passed_through()`` sandbox escape hatch.

Validates Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10,
5.11, 11.1, 11.2, 11.3, 11.4, 11.5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Activity / shared-helper imports inside the Temporal sandbox escape hatch.
#
# ``workflow.unsafe.imports_passed_through()`` is the documented way to
# import modules that touch I/O (httpx, asyncpg, ...) without breaking the
# Temporal workflow sandbox. The static AST determinism test
# (``platform/tests/property/test_workflow_determinism_static.py``)
# explicitly tolerates this with-block.
# ---------------------------------------------------------------------------

with workflow.unsafe.imports_passed_through():
    from temporal_shared.capabilities import (
        missing_capabilities,
        required_capabilities,
    )
    from temporal_shared.identifiers import agent_workflow_id

    # Activity name strings only — calling activities by name keeps the
    # workflow body decoupled from the activity modules' httpx imports.
    # We still import the data-class types we *send* to / *receive* from
    # activities so the workflow can construct/destructure them.
    from src.activities.jira import IssueData  # noqa: F401  (used in type hints)
    from src.activities.llm import DeptContext as _LlmDeptContext
    from src.activities.llm import IssueData as _LlmIssueData
    from src.prompts.parser import TaskAnalysis, TaskAnalysisError  # noqa: F401


# ---------------------------------------------------------------------------
# Default activity options
# ---------------------------------------------------------------------------

#: Default timeout for short-lived Atlassian / DB activities (Jira comment,
#: transition, work-item update). Generous enough for transient retries.
_SHORT_TIMEOUT: timedelta = timedelta(minutes=2)

#: Timeout for LLM activities (task analysis can take longer on large issues).
_LLM_TIMEOUT: timedelta = timedelta(minutes=5)

#: Retry policy for short side-effecting activities. The activity itself is
#: idempotent (Jira comments are append-only, transitions are no-op when
#: already in target status, work_items.status updates are state-machine
#: validated) so a few quick retries on transient failures is safe.
_DEFAULT_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Maximum number of LLM analysis iterations triggered by ``new_comment``
#: signals. The first analysis is iteration 1; every signal that re-runs
#: ``llm_analyze_task`` increments ``_loop_count``. Once it reaches
#: ``MAX_LOOP_COUNT`` the workflow gives up and marks the work item failed.
MAX_LOOP_COUNT: int = 3

#: Wall-clock budget for waiting on a ``new_comment`` reply when confidence
#: is ``"low"``. Implemented via ``workflow.wait_condition(timeout=...)``
#: which uses the deterministic Temporal timer.
NEEDS_INFO_TIMEOUT: timedelta = timedelta(days=7)


# ---------------------------------------------------------------------------
# Public dataclasses (workflow input / output / signal payload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutomationInput:
    """Input payload passed by ``automation-service`` when starting the workflow.

    Mirrors the JSON schema in design.md §"AutomationInput JSON şeması".

    Attributes
    ----------
    issue_key:
        Jira issue key (e.g. ``"PAY-4211"``). For Bitbucket-PR-triggered
        runs this is the Jira key linked to the PR (or ``"BB-{pr_id}"``
        when no link exists — the webhook handler decides).
    department_id:
        Department slug for credential resolution and routing.
    available_capabilities:
        Tuple of capability strings the department has
        (``"jira"``, ``"bitbucket"``, ``"execution"``, ``"confluence"``,
        ``"web_search"``). Tuple (not frozenset) so the dataclass is
        Temporal-serialisable; converted to a ``frozenset`` inside the
        workflow body.
    available_repos:
        Tuple of repository slugs accessible to the department; passed
        through to ``llm_analyze_task`` as part of ``DeptContext``.
    available_spaces:
        Tuple of Confluence space keys accessible to the department.
    default_language:
        ISO-639-1 language code for LLM output / Jira comments
        (``"tr"`` per default).
    trigger_event:
        Webhook event type that started the workflow
        (e.g. ``"jira:issue_assigned"``).
    iteration:
        Iteration number; ``1`` for the initial run. Reserved for future
        iter-N support; the AgentRunner child also gets this value.
    """

    issue_key: str
    department_id: str
    available_capabilities: tuple[str, ...] = ()
    available_repos: tuple[str, ...] = ()
    available_spaces: tuple[str, ...] = ()
    default_language: str = "tr"
    trigger_event: str = "jira:issue_assigned"
    iteration: int = 1


@dataclass(frozen=True)
class AutomationResult:
    """Final result of an :class:`AutomationWorkflow` run.

    Mirrors the JSON schema in design.md §"AutomationResult".

    Attributes
    ----------
    status:
        One of ``"completed"`` or ``"failed"``.
    workflow_type:
        The LLM-selected workflow type (``"code_change_with_test"`` etc.)
        if the analysis succeeded, otherwise ``None``.
    child_workflow_id:
        The Temporal workflow ID of the dispatched ``AgentRunnerWorkflow``
        child, or ``None`` if dispatch never happened (capability gate
        failure, loop cap reached, 7d timeout, ...).
    summary:
        Short human-readable summary; mirrored into the final Jira comment.
    failure_reason:
        Stable category for failed runs:
        ``"missing_capability"``, ``"loop_cap_reached"``,
        ``"needs_info_timeout"``, ``"child_failed"``,
        ``"task_analysis_failed"``, or ``None`` for completed runs.
    """

    status: str
    workflow_type: str | None = None
    child_workflow_id: str | None = None
    summary: str = ""
    failure_reason: str | None = None


@dataclass(frozen=True)
class NewCommentSignal:
    """Payload for the ``new_comment`` signal.

    The Jira webhook handler forwards each ``jira:comment_created`` event
    as a signal to the running ``AutomationWorkflow`` (when one exists for
    the issue). The workflow uses the comment to feed back into
    ``llm_analyze_task`` for needs_info clarification.

    Attributes
    ----------
    comment_text:
        Plain-text body of the new comment.
    actor_account_id:
        Atlassian account ID of the commenter — useful for the loop-guard
        when the workflow itself comments via the bot account. (The webhook
        handler usually drops bot comments before forwarding, but defending
        in depth keeps the workflow robust.)
    """

    comment_text: str
    actor_account_id: str | None = None


@dataclass(frozen=True)
class _AgentRunnerInputShape:
    """Shape of the input passed to the ``AgentRunnerWorkflow`` child.

    Defined here (rather than imported from
    :mod:`src.workflows.agent_runner_workflow`) because the child workflow
    module is a stub at this point in the spec; the concrete dataclass
    lives at the call site so the orchestrator can be implemented and
    tested independently. The child workflow can later import or mirror
    this shape when its body is filled in.
    """

    parent_workflow_id: str
    issue_key: str
    department_id: str
    workflow_type: str
    target_repo: str | None
    target_branch: str
    output_actions: tuple[dict[str, Any], ...]
    iteration: int = 1


# ---------------------------------------------------------------------------
# Helper: format the "missing capability" Jira comment
# ---------------------------------------------------------------------------


def _format_missing_caps_comment(workflow_type: str, missing: set[str]) -> str:
    """Return the Turkish missing-capability blocked-comment body."""

    listed = ", ".join(sorted(missing))
    return (
        f"⛔ Eksik capability — '{workflow_type}' iş akışı için "
        f"şu servis(ler) tanımlı değil: {listed}. "
        f"Lütfen departman bot kimlik bilgilerini tamamladıktan sonra "
        f"görevi tekrar atayın."
    )


def _format_completion_comment(
    workflow_type: str, child_summary: str
) -> str:
    """Return the Turkish completion summary comment body."""

    suffix = f" — {child_summary}" if child_summary else ""
    return f"✅ Tamamlandı ({workflow_type}){suffix}."


def _format_failure_comment(workflow_type: str, reason: str) -> str:
    """Return the Turkish child-failure summary comment body."""

    return (
        f"❌ İş akışı başarısız ({workflow_type}). "
        f"Sebep: {reason}. Detaylar Temporal UI'da."
    )


# ---------------------------------------------------------------------------
# AutomationWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="AutomationWorkflow")
class AutomationWorkflow:
    """Top-level orchestrator workflow — see module docstring."""

    def __init__(self) -> None:
        # Pending question posted to Jira when confidence == "low";
        # surfaced via the ``get_pending_question`` query so external
        # observers (admin UI, debugging) can see what the workflow is
        # waiting for without scraping comments.
        self._pending_question: str | None = None

        # Number of LLM re-analyses triggered by ``new_comment`` signals.
        # The first analysis is *not* counted (it is iteration 1 by default
        # — counter increments only on signal-driven reruns).
        self._loop_count: int = 0

        # Append-only conversation buffer of comments received via signals,
        # fed back into ``llm_analyze_task`` on the next iteration.
        self._comments_received: list[str] = []

        # One-shot edge flag flipped by ``new_comment`` signals; consumed
        # and reset by the wait-condition predicate.
        self._new_comment_received: bool = False

    # -- Signals -----------------------------------------------------------

    @workflow.signal
    def new_comment(self, comment: NewCommentSignal) -> None:
        """Receive a forwarded ``jira:comment_created`` event.

        Stores the comment text in the conversation buffer and flips the
        edge flag the wait-condition predicate observes. The signal handler
        intentionally does *no* I/O — all it does is mutate workflow state.
        """

        # Accept either a NewCommentSignal dataclass or the raw dict shape
        # Temporal converts JSON payloads into. Defensive coercion lets us
        # interop with handlers that pass plain dicts.
        if isinstance(comment, NewCommentSignal):
            text = comment.comment_text
        elif isinstance(comment, dict):
            text = str(comment.get("comment_text", ""))
        else:
            text = str(comment)

        if text:
            self._comments_received.append(text)
            self._new_comment_received = True

    # -- Queries -----------------------------------------------------------

    @workflow.query
    def get_pending_question(self) -> str | None:
        """Return the question the workflow is currently waiting on, if any.

        Returns ``None`` when the workflow is not in a needs_info wait state
        (analysis succeeded with high/medium confidence, or a previous
        iteration cleared the question).
        """

        return self._pending_question

    # -- Run ---------------------------------------------------------------

    @workflow.run
    async def run(self, inp: AutomationInput) -> AutomationResult:
        # The Temporal workflow info object is deterministic — its
        # ``workflow_id`` is derived from the start request, not the clock.
        parent_workflow_id = workflow.info().workflow_id

        # 1. Best-effort acknowledgement comment. Wrapped in a try/except so
        #    a failed ack never aborts the run — the issue may have been
        #    deleted between webhook receipt and workflow start.
        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[
                    inp.issue_key,
                    "🤖 Task alındı, analiz ediliyor...",
                    inp.department_id,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "jira_add_comment ack failed for %s — continuing anyway",
                inp.issue_key,
            )

        # Mark work_item as running. This is the pending → running edge
        # of the state machine (Property 9) and must succeed for the
        # workflow to progress.
        await self._update_work_item_status(parent_workflow_id, "running")

        # 2. Fetch the Jira issue. Failure here is fatal — without issue
        #    data the LLM cannot analyse the task.
        try:
            issue_data = await workflow.execute_activity(
                "jira_get_issue",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                parent_workflow_id=parent_workflow_id,
                inp=inp,
                workflow_type=None,
                reason="task_analysis_failed",
                jira_message=(
                    "❌ Jira issue okunamadı, görev başlatılamıyor. "
                    f"Hata: {exc}"
                ),
            )

        # 3. Initial LLM analysis. The result drives capability check,
        #    needs-info loop, and child dispatch.
        dept_context = _LlmDeptContext(
            available_repos=list(inp.available_repos),
            available_spaces=list(inp.available_spaces),
            available_capabilities=list(inp.available_capabilities),
            default_language=inp.default_language,
        )
        llm_issue = self._to_llm_issue(issue_data, inp.issue_key)

        try:
            analysis = await workflow.execute_activity(
                "llm_analyze_task",
                args=[llm_issue, dept_context],
                result_type=TaskAnalysis,
                start_to_close_timeout=_LLM_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                parent_workflow_id=parent_workflow_id,
                inp=inp,
                workflow_type=None,
                reason="task_analysis_failed",
                jira_message=(
                    f"❌ Görev analizi başarısız: {exc}. "
                    "Lütfen issue açıklamasını netleştirip yeniden atayın."
                ),
            )

        # 4-5. Capability Phase 2 + needs_info loop.
        analysis = await self._resolve_analysis(
            parent_workflow_id=parent_workflow_id,
            inp=inp,
            llm_issue=llm_issue,
            dept_context=dept_context,
            analysis=analysis,
        )
        if isinstance(analysis, AutomationResult):
            # Helper short-circuited (capability missing / loop cap /
            # 7d timeout) — return its terminal result directly.
            return analysis

        # 6. Dispatch child AgentRunnerWorkflow.
        child_id = agent_workflow_id(parent_workflow_id, 1)
        child_input = _AgentRunnerInputShape(
            parent_workflow_id=parent_workflow_id,
            issue_key=inp.issue_key,
            department_id=inp.department_id,
            workflow_type=analysis.workflow_type,
            target_repo=analysis.target_repo,
            target_branch=analysis.target_branch or "develop",
            output_actions=tuple(
                {"type": a.type, "payload": a.payload}
                for a in analysis.output_actions
            ),
            iteration=inp.iteration,
        )

        try:
            child_summary = await workflow.execute_child_workflow(
                "AgentRunnerWorkflow",
                args=[child_input],
                id=child_id,
                task_queue=workflow.info().task_queue,
            )
        except Exception as exc:  # noqa: BLE001
            return await self._fail(
                parent_workflow_id=parent_workflow_id,
                inp=inp,
                workflow_type=analysis.workflow_type,
                reason="child_failed",
                jira_message=_format_failure_comment(
                    analysis.workflow_type, str(exc)
                ),
                child_workflow_id=child_id,
            )

        # 7. Success path: completion comment + Done transition + completed.
        summary_text = self._stringify_child_result(child_summary)
        completion_body = _format_completion_comment(
            analysis.workflow_type, summary_text
        )
        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[inp.issue_key, completion_body, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — comment best-effort
            workflow.logger.warning(
                "jira_add_comment (completion) failed for %s — continuing",
                inp.issue_key,
            )

        try:
            await workflow.execute_activity(
                "jira_transition_issue",
                args=[inp.issue_key, "Done", inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — transition best-effort
            workflow.logger.warning(
                "jira_transition_issue (Done) failed for %s — continuing",
                inp.issue_key,
            )

        await self._update_work_item_status(
            parent_workflow_id, "completed"
        )

        return AutomationResult(
            status="completed",
            workflow_type=analysis.workflow_type,
            child_workflow_id=child_id,
            summary=summary_text,
            failure_reason=None,
        )

    # -- Internal helpers --------------------------------------------------

    async def _resolve_analysis(
        self,
        *,
        parent_workflow_id: str,
        inp: AutomationInput,
        llm_issue: Any,
        dept_context: Any,
        analysis: TaskAnalysis,
    ) -> TaskAnalysis | AutomationResult:
        """Apply Phase 2 capability check + needs_info loop.

        Returns either the resolved (high/medium confidence + capability
        OK) ``TaskAnalysis``, or a terminal :class:`AutomationResult`
        when the workflow should stop here (missing capability, loop cap,
        timeout).
        """

        while True:
            # --- Capability Phase 2 -----------------------------------
            try:
                required = required_capabilities(analysis.workflow_type)
            except KeyError:
                # ``multi_step`` or unknown workflow type — parser should
                # have rejected this, but guard anyway.
                return await self._fail(
                    parent_workflow_id=parent_workflow_id,
                    inp=inp,
                    workflow_type=analysis.workflow_type,
                    reason="missing_capability",
                    jira_message=(
                        f"⛔ Bilinmeyen workflow_type "
                        f"'{analysis.workflow_type}'. Görev iptal edildi."
                    ),
                    transition_to_blocked=True,
                )

            available = frozenset(inp.available_capabilities)
            missing = missing_capabilities(required, available)
            if missing:
                return await self._fail(
                    parent_workflow_id=parent_workflow_id,
                    inp=inp,
                    workflow_type=analysis.workflow_type,
                    reason="missing_capability",
                    jira_message=_format_missing_caps_comment(
                        analysis.workflow_type, missing
                    ),
                    transition_to_blocked=True,
                )

            # --- Confidence gate --------------------------------------
            if analysis.confidence != "low" or not analysis.needs_info_question:
                # Clear any pending question (a previous iteration may
                # have set one) and exit the loop with the resolved
                # analysis.
                self._pending_question = None
                return analysis

            # --- Needs-info: post the question ------------------------
            self._pending_question = analysis.needs_info_question
            try:
                await workflow.execute_activity(
                    "jira_add_comment",
                    args=[
                        inp.issue_key,
                        analysis.needs_info_question,
                        inp.department_id,
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception:  # noqa: BLE001 — comment best-effort
                workflow.logger.warning(
                    "jira_add_comment (needs_info) failed for %s",
                    inp.issue_key,
                )

            # --- Wait for signal / timeout / loop cap -----------------
            # Reset the edge flag before each wait so spurious wake-ups
            # from previous signals do not leak across iterations.
            self._new_comment_received = False

            try:
                await workflow.wait_condition(
                    lambda: (
                        self._new_comment_received
                        or self._loop_count >= MAX_LOOP_COUNT
                    ),
                    timeout=NEEDS_INFO_TIMEOUT,
                )
            except TimeoutError:
                return await self._fail(
                    parent_workflow_id=parent_workflow_id,
                    inp=inp,
                    workflow_type=analysis.workflow_type,
                    reason="needs_info_timeout",
                    jira_message=(
                        "⌛ 7 gün içinde yanıt alınmadı, görev "
                        "kapatıldı. Lütfen issue'yu yeniden atayın."
                    ),
                )

            # --- Decide why we woke up --------------------------------
            if self._loop_count >= MAX_LOOP_COUNT:
                return await self._fail(
                    parent_workflow_id=parent_workflow_id,
                    inp=inp,
                    workflow_type=analysis.workflow_type,
                    reason="loop_cap_reached",
                    jira_message=(
                        "🛑 Görev analizi 3 iterasyon sonra hâlâ "
                        "düşük güven üretti, otomatik işleme kapatıldı."
                    ),
                )

            # Signal woke us up — increment, re-analyse with the new
            # comment context, then loop back to the capability check.
            self._loop_count += 1
            self._new_comment_received = False
            self._pending_question = None

            try:
                analysis = await workflow.execute_activity(
                    "llm_analyze_task",
                    args=[
                        self._llm_issue_with_history(llm_issue),
                        dept_context,
                    ],
                    result_type=TaskAnalysis,
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001
                return await self._fail(
                    parent_workflow_id=parent_workflow_id,
                    inp=inp,
                    workflow_type=None,
                    reason="task_analysis_failed",
                    jira_message=(
                        f"❌ Tekrar analiz başarısız: {exc}."
                    ),
                )

    async def _fail(
        self,
        *,
        parent_workflow_id: str,
        inp: AutomationInput,
        workflow_type: str | None,
        reason: str,
        jira_message: str,
        transition_to_blocked: bool = False,
        child_workflow_id: str | None = None,
    ) -> AutomationResult:
        """Common failure path: post comment, optionally transition, mark failed."""

        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[inp.issue_key, jira_message, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — comment best-effort
            workflow.logger.warning(
                "jira_add_comment (failure) failed for %s", inp.issue_key
            )

        if transition_to_blocked:
            try:
                await workflow.execute_activity(
                    "jira_transition_issue",
                    args=[inp.issue_key, "Blocked", inp.department_id],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception:  # noqa: BLE001 — transition best-effort
                workflow.logger.warning(
                    "jira_transition_issue (Blocked) failed for %s",
                    inp.issue_key,
                )

        await self._update_work_item_status(parent_workflow_id, "failed")

        return AutomationResult(
            status="failed",
            workflow_type=workflow_type,
            child_workflow_id=child_workflow_id,
            summary=jira_message,
            failure_reason=reason,
        )

    async def _update_work_item_status(
        self, workflow_id: str, new_status: str
    ) -> None:
        """Best-effort wrapper around ``update_work_item_status`` activity.

        The activity itself enforces the state-machine invariant
        (Property 9). If the update fails (DB unavailable, transition
        rejected because of an unexpected concurrent write) we log and
        continue — the workflow's terminal status is the source of truth
        for downstream observers; the DB row eventually reconciles.
        """

        try:
            await workflow.execute_activity(
                "update_work_item_status",
                args=[workflow_id, new_status],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — DB update best-effort
            workflow.logger.warning(
                "update_work_item_status(%s -> %s) failed",
                workflow_id,
                new_status,
            )

    # ---- Pure converters / formatters ------------------------------------

    @staticmethod
    def _to_llm_issue(issue_data: Any, issue_key: str) -> Any:
        """Adapt the ``jira_get_issue`` result to the LLM activity's shape.

        The two activities use *different* ``IssueData`` dataclasses
        (jira.IssueData has more fields than llm.IssueData). This method
        is pure — no I/O, no time. Accepts duck-typed inputs so tests
        can pass plain objects/dicts.
        """

        # Support both dataclass and dict shapes (Temporal serialises to
        # dict and reconstructs on the activity boundary; in unit tests
        # the workflow may receive either form).
        def _attr(obj: Any, name: str, default: Any = "") -> Any:
            if isinstance(obj, dict):
                return obj.get(name, default)
            return getattr(obj, name, default)

        return _LlmIssueData(
            issue_key=_attr(issue_data, "key", issue_key) or issue_key,
            summary=_attr(issue_data, "summary", "") or "",
            description=_attr(issue_data, "description", "") or "",
            issue_type=_attr(issue_data, "issue_type", "") or "",
            project_key=_attr(issue_data, "project_key", "") or "",
        )

    def _llm_issue_with_history(self, base: Any) -> Any:
        """Append the conversation history to the issue description.

        Concatenates received comments (newest last) into the description
        so the LLM has full context on the next iteration. Pure — no time,
        no random.
        """

        if not self._comments_received:
            return base

        history = "\n\n--- Yorumlar ---\n" + "\n\n".join(
            f"[{i + 1}] {text}"
            for i, text in enumerate(self._comments_received)
        )
        # _LlmIssueData is frozen; build a new instance.
        return _LlmIssueData(
            issue_key=base.issue_key,
            summary=base.summary,
            description=(base.description or "") + history,
            issue_type=base.issue_type,
            project_key=base.project_key,
        )

    @staticmethod
    def _stringify_child_result(child_result: Any) -> str:
        """Flatten the child workflow's return value to a short summary line."""

        if child_result is None:
            return ""
        if isinstance(child_result, str):
            return child_result
        # Common shapes: dataclass with ``summary`` attribute, or a dict
        # with the same key. Fall back to ``str(...)`` for everything else.
        summary_attr = getattr(child_result, "summary", None)
        if isinstance(summary_attr, str) and summary_attr:
            return summary_attr
        if isinstance(child_result, dict):
            text = child_result.get("summary")
            if isinstance(text, str) and text:
                return text
        return str(child_result)


__all__ = [
    "AutomationInput",
    "AutomationResult",
    "AutomationWorkflow",
    "MAX_LOOP_COUNT",
    "NEEDS_INFO_TIMEOUT",
    "NewCommentSignal",
]
