"""``AutomationWorkflow`` — gateway + capability gate + workflow_type router.

This ``AutomationWorkflow`` replaces the older orchestrator that lives in
``platform/workers/agent-runner-worker/src/workflows/automation_workflow.py``.
This module is hosted by the dedicated ``automation-worker``
(``automation-tq`` task queue) and consumes the strictly-typed I/O
dataclasses from:mod:`temporal_shared.messages`.

Responsibilities:

1. Best-effort acknowledgement comment on the triggering Jira issue
 so the user sees that the bot has picked up the task.
2. ``llm_analyze_task`` activity — derive ``workflow_type``,
 ``target_repo`` / ``target_branch`` / ``target_space`` and an
 ``output_actions`` proposal. The token cap is enforced inside
 the activity; the workflow only fails the run when the activity
 itself raises.
3. Capability gate — translate the LLM-selected
 ``workflow_type`` to the simple-vocabulary capability set via:func:`temporal_shared.capabilities.required_capabilities`, then
 compare against ``inp.available_capabilities`` with:func:`missing_capabilities`. Missing capability → ``decision="denied"``
 plus an audit-action / Jira comment activity call, then return.
4. ``branch_pattern_rules`` routing — for code-change /
 pr_review workflows the workflow asks an activity for the
 department's rule list, then runs the **pure**:func:`temporal_shared.branch_rules.route_by_branch_pattern` to
 accept or reject the (branch, workflow_type) pair. A denial here
 maps to ``decision="out_of_scope"``.
5. Dispatch the appropriate child workflow via
 ``workflow.start_child_workflow`` — ``AgentRunnerWorkflow`` for
 the LLM/MCP path, ``ExecutionRunWorkflow`` for the SSH-only path.
 The dispatch awaits the child's ``WorkflowHandle`` (``await
 start_child_workflow(...)``) so the parent records a stable
 ``child_workflow_id`` and then awaits the result so failures
 propagate as exceptions.

Determinism contract: the workflow body uses **only** Temporal-deterministic
primitives:

* ``workflow.now`` — replay-safe wallclock (currently unused in
 this module; reserved for future audit timestamp emission).
* ``workflow.execute_activity(<name>,...)`` for every side effect
 (audit write, Jira comment, dept-config lookup, LLM analysis).
* ``workflow.start_child_workflow`` for every child dispatch.
* No ``random`` / ``uuid.uuid4`` / ``os.environ`` / direct I/O —
 ``workflow.uuid4`` is the only acceptable UUID source if needed.
* Activity / shared-helper imports are placed inside
 ``workflow.unsafe.imports_passed_through`` so the Temporal
 sandbox accepts them and the static AST scanner
 (``platform/tests/property/test_workflow_determinism_static.py``)
 recognises them as legitimate.

The workflow runs on ``automation-tq``, uses the workflow-type
capability mapping for the gate, applies the denied behavior for
missing capabilities, and applies ``branch_pattern_rules`` for
hotfix/release routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Activity / shared-helper imports inside the Temporal sandbox escape hatch.
#
# ``workflow.unsafe.imports_passed_through`` is the Temporal-blessed way
# to import modules that may pull in I/O machinery (httpx, asyncpg,...)
# without breaking the workflow sandbox. The static AST determinism test
# explicitly tolerates this with-block. Activities themselves are still
# referenced **by string name** through ``workflow.execute_activity`` so
# the workflow module never imports activity callables.
# ---------------------------------------------------------------------------

with workflow.unsafe.imports_passed_through():
    from temporal_shared.branch_rules import (
        DEFAULT_BRANCH_PATTERN_RULES,
        BranchPatternRule,
        RouteDecision,
        route_by_branch_pattern,
    )
    from temporal_shared.capabilities import (
        WORKFLOW_TYPE_CAPABILITIES,
        missing_capabilities,
        required_capabilities,
    )
    from temporal_shared.identifiers import execution_artifact_key
    from temporal_shared.messages import (
        AutomationWorkflowInput,
        AutomationWorkflowOutput,
        ChildWorkflowSpec,
        ExecutionRunWorkflowInput,
        ExecutionRunWorkflowOutput,
        LlmAnalysisResult,
        OutputAction,
    )
    from temporal_shared.workflow_registry import task_queue_for

    # ExecutionRunWorkflow result wiring (
    # — execution-runner output_actions wire-in). The
    # ``execute_output_actions`` activity lives in the
    # automation-worker activity package; we import its dataclass
    # shapes (the activity itself stays referenced by string name via
    # ``workflow.execute_activity``) so the gateway can synthesise a
    # batch from the analyser-supplied:class:`OutputAction` list and
    # the child's MinIO artifact URIs.
    from automation_worker.activities.output_actions import (
        ExecutionBatchInput,
        ExecutionBatchResult,
    )
    from automation_worker.activities.output_actions import (
        OutputAction as ExecutorOutputAction,
    )
    from db_shared.enums import ActionType

    # Workflow-completion notification dispatch. The activity itself is referenced by
    # string name through ``workflow.execute_activity`` so the
    # workflow module stays decoupled from the activity body's
    # network-side imports. Only the input dataclass shape is
    # imported here (Temporal serialises it via the default JSON
    # data converter).
    from automation_worker.activities.notification_dispatch import (
        DispatchNotificationInput,
    )

    #: the new ``analyze_task`` activity is
    # the primary task analyser for AutomationWorkflow. It hosts both
    # the YAML front-matter (deterministic) path and the LLM analysis
    # path internally, and is allowed to post Jira comments
    # (``rejected`` / ``needs_info`` / ``downgrade``) as side effects.
    # The workflow body therefore only consumes the structured
    #:class:`TaskAnalysisResult` envelope; it does NOT post the same
    # comments a second time.
    #
    # ``TaskAnalysisInput`` / ``TaskAnalysisResult`` live inside the
    # activity module (rather than ``temporal_shared.messages``) because
    # they are activity-private types — the workflow only needs to hold
    # the dataclass shapes for its return-type hints, not import the
    # activity callable. They are referenced inside the sandbox escape
    # hatch alongside the other deterministic shared types.
    from automation_worker.activities.task_analyzer import (
        TaskAnalysisInput,
        TaskAnalysisResult,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Activity name strings — referenced via ``workflow.execute_activity(<name>,
#:...)`` so the workflow module stays decoupled from the concrete activity
#: implementations (which carry network-side imports).
_ACT_JIRA_ADD_COMMENT: Final[str] = "jira_add_comment"
_ACT_JIRA_TRANSITION_ISSUE: Final[str] = "jira_transition_issue"
_ACT_LLM_ANALYZE_TASK: Final[str] = "llm_analyze_task"
_ACT_LOAD_BRANCH_PATTERN_RULES: Final[str] = "load_branch_pattern_rules"
_ACT_AUDIT_WRITE: Final[str] = "audit_write"

#: Activities used for the analyser front-door.
#: ``prepare_task_analysis_input`` fetches the Jira issue
#: text + department config and packages them into a
#::class:`TaskAnalysisInput`; ``analyze_task`` then runs the YAML
#: front-matter / LLM analysis pipeline and returns a
#::class:`TaskAnalysisResult`. Both names are resolved to concrete
#: activity callables at worker boot time — the workflow body only
#: ever sees the string literals.
_ACT_PREPARE_TASK_ANALYSIS_INPUT: Final[str] = "prepare_task_analysis_input"
_ACT_ANALYZE_TASK: Final[str] = "analyze_task"

#: Name of the signal sent by the webhook dispatcher when the user
#: replies to a needs_info comment on the Jira issue. The
#: dispatcher derives this from ``shared.dispatcher._signal_workflow``
#: in:mod:`platform.services.automation-service.src.webhooks.dispatcher`.
_SIGNAL_INFO_RECEIVED: Final[str] = "info_received"

#: Maximum wall-clock time the gateway will park in the needs_info wait
#: state before transitioning the Jira issue to ``stale`` and ending the
#: workflow. Implemented via ``workflow.wait_condition(timeout=...)``
#: which uses Temporal's deterministic timer.
#:
#: The parked window matches the Turkish prose
#: in:func:`_format_needs_info_timeout_comment` ("7 gün") and the sibling
#: ``agent_runner.SIGNAL_WAIT_TIMEOUT`` constant.
_NEEDS_INFO_TIMEOUT: Final[timedelta] = timedelta(days=7)

#: Maximum number of needs_info iterations. After this many comment-based
#: re-analyses fail to lift confidence above the threshold the workflow
#: terminates as ``failed`` with ``failure_reason="loop_cap_reached"``.
#: Three iterations matches the cap used by the legacy
#::class:`AgentRunnerWorkflow.needs_info_streak` invariant,
#: keeping operator expectations aligned across the two pathways.
_NEEDS_INFO_MAX_ITERATIONS: Final[int] = 3

#: Activity that posts the captured ``noop_test`` stdout / exit code as
#: a Jira comment after the child:class:`ExecutionRunWorkflow`
#: completes. Wired into the gateway's child-await branch so
#: the smoke-test path verifies the full pipeline (webhook →
#: AutomationWorkflow → ExecutionRunWorkflow → SSH runner → Jira
#: comment) without involving the LLM. The activity body lives in
#::mod:`automation_worker.activities.noop_test`.
_ACT_NOOP_TEST_POST_RESULT: Final[str] = "noop_test_post_result"

#: Name of the output-action executor activity.
#: Hosted on the same ``automation-tq`` queue as this workflow so
#: dispatch is local. The activity translates each
#::class:`automation_worker.activities.output_actions.OutputAction`
#: into the matching MCP call (Jira comment, Jira attachment,
#: Bitbucket PR, Confluence page, Jira transition).
_ACT_EXECUTE_OUTPUT_ACTIONS: Final[str] = "execute_output_actions"

#: Name of the notification dispatch activity. Posts a workflow-completion
#: notification through:class:`notification.NotificationService`
#: at every terminal return of:meth:`AutomationWorkflow.run`. The
#: activity is **best-effort** — its body swallows transport
#: failures and never raises, but the workflow body wraps every
#: dispatch call in its own try/except as well so a
#: ``WorkflowFailureError`` (eg. activity registration drift)
#: cannot block the workflow's terminal return either.
_ACT_DISPATCH_NOTIFICATION: Final[str] = "dispatch_notification"

#: Wall-clock budget for the execution-result output-action batch.
#: The activity itself enforces a per-action 30 s timeout
#: (``ACTION_TIMEOUT_SECONDS``) and a 20-action batch ceiling
#: (``MAX_ACTIONS_PER_BATCH``); 20 × 30 s = 600 s plus headroom for
#: artifact downloads / MCP retries → 15 minutes is a safe upper
#: bound that mirrors:data:`workflow_helpers._OUTPUT_ACTIONS_TIMEOUT`.
_OUTPUT_ACTIONS_TIMEOUT: Final[timedelta] = timedelta(minutes=15)

#: Default MinIO bucket used by the SSH runner for stdout / stderr
#: artifacts (mirrors
#::data:`execution_runner_worker.activities.minio.DEFAULT_BUCKET`).
#: Surfaced as a literal here so the workflow body does not import
#: the execution-runner-worker activity package.
_EXECUTION_DEFAULT_BUCKET: Final[str] = "ai-runs"

#: ``noop_test`` smoke command template. The placeholder
#: ``{issue_key}`` is substituted with the triggering Jira issue key
#: so the Jira-side reader can correlate the runner output with the
#: issue. The wrapping double quotes are part of the shell command
#: and are passed through to the runner as opaque text.
_NOOP_TEST_COMMAND_TEMPLATE: Final[str] = 'echo "noop_test ok: {issue_key}"'

#: Default short-lived activity timeout. Generous enough to absorb
#: transient network blips on Jira / Postgres without blocking the
#: workflow for too long.
_SHORT_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: LLM activity timeout — task analysis may take longer for large issues.
#: The activity itself enforces the per-call token cap; this
#: timeout is just the overall wall-clock bound.
_LLM_TIMEOUT: Final[timedelta] = timedelta(minutes=5)

#: Retry policy for short, idempotent side-effecting activities.
#: ``maximum_attempts=5`` matches the existing ``AutomationWorkflow``
#: orchestrator in ``agent-runner-worker`` so operators see consistent
#: retry behaviour across both gateways.
_DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Retry policy for the LLM analysis activity. Failures here are
#: usually fatal (token cap, prompt validation) — three attempts is
#: enough; we surface a clear failure rather than retrying forever.
_LLM_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


#: Wall-clock budget for the workflow-completion notification
#: dispatch activity. The
#: activity body is best-effort — Slack / email transport failures
#: are swallowed inside the activity — so the workflow tolerates a
#: short timeout here. ``maximum_attempts=2`` matches the
#: dispatcher's idempotency contract (the deterministic
#: ``dedup_key`` makes a Temporal-driven retry a safe no-op for any
#: given channel).
_NOTIFICATION_DISPATCH_TIMEOUT: Final[timedelta] = timedelta(seconds=30)
_NOTIFICATION_DISPATCH_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=2,
)


# ---------------------------------------------------------------------------
# Workflow-type → child workflow routing table
# ---------------------------------------------------------------------------
#
# Each workflow type is assigned to one of two child workflows:
#
# * ``AgentRunnerWorkflow`` — the LLM + MCP path. Handles every
# workflow type that needs at least one LLM call (code change,
# PR review, Confluence, research, multi_step).
# * ``ExecutionRunWorkflow`` — the SSH/Docker path. Handles
# ``remote_ssh_test_only`` and ``noop_test`` because both run a
# single shell command on a runner without LLM involvement.
#
# Multi-step workflows are dispatched as a single ``AgentRunnerWorkflow``
# child that internally fans out to further children — the
# parent only needs to know which queue to hand the work to.
_AGENT_RUNNER_WORKFLOW_TYPES: Final[frozenset[str]] = frozenset(
    {
        "code_change_with_test",
        "code_change_commit_only",
        "pr_review",
        "confluence_doc_create",
        "confluence_doc_update",
        "research_basic",
        "research_with_web",
        "research_publish_confluence",
        "research_summary_jira",
        "multi_step",
    }
)

_EXECUTION_RUN_WORKFLOW_TYPES: Final[frozenset[str]] = frozenset(
    {
        "remote_ssh_test_only",
        "noop_test",
        # ``script_execute`` runs an ad-hoc script on a runner — no LLM involvement, same shape as
        # ``remote_ssh_test_only``. Previously absent from both routing
        # sets so it fell into the ``else`` branch of ``_build_child_spec``
        # and was misrouted to AgentRunnerWorkflow.
        "script_execute",
    }
)

#: Code-change-shaped workflow types: those for which a non-empty
#: ``target_branch`` is meaningful and the ``branch_pattern_rules``
#: gate applies. Confluence / research / noop workflows do not
#: operate on a Git branch and therefore skip the routing step.
_BRANCH_AWARE_WORKFLOW_TYPES: Final[frozenset[str]] = frozenset(
    {
        "code_change_with_test",
        "code_change_commit_only",
        "pr_review",
    }
)


# ---------------------------------------------------------------------------
# Internal value objects (workflow-private; not part of the activity wire)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AutomationStop:
    """Internal early-exit envelope produced by helper coroutines.

 A helper that decides the workflow should stop returns one of
 these instead of raising — keeps the run body linear and lets
 pyright follow the ``decision`` value into the:class:`AutomationWorkflowOutput` at the call site.
 """

    decision: str  # "denied" | "out_of_scope" | "failed"
    workflow_type: str | None
    summary: str
    failure_reason: str
    missing_capabilities: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Helper formatters (pure)
# ---------------------------------------------------------------------------
#
# All helpers below are pure: they only build Turkish prose strings
# from their inputs. Keeping them at module scope (rather than as
# methods) makes them trivially unit-testable without instantiating
# the workflow.


def _format_ack_comment() -> str:
    """Initial acknowledgement comment posted on the Jira issue."""

    return "🤖 Task alındı, otomasyon hattı analiz ediyor..."


def _format_missing_caps_comment(
    workflow_type: str, missing: tuple[str, ...]
) -> str:
    """Missing-capability denial comment."""

    listed = ", ".join(sorted(missing))
    return (
        f"⛔ Eksik capability — '{workflow_type}' iş akışı için "
        f"şu servis(ler) tanımlı değil: {listed}. "
        f"Lütfen departman bot kimlik bilgilerini tamamladıktan sonra "
        f"görevi tekrar atayın."
    )


def _format_unknown_workflow_type_comment(workflow_type: str) -> str:
    """Unknown workflow type denial — surfaces the LLM mistake."""

    return (
        f"⛔ Bilinmeyen workflow_type '{workflow_type}'. "
        "Görev otomasyon hattı tarafından reddedildi."
    )


def _format_branch_rule_denied_comment(
    workflow_type: str,
    branch: str,
    decision: RouteDecision,
) -> str:
    """Branch-pattern denial comment."""

    matched = decision.matched_glob or "?"
    return (
        f"⛔ '{branch}' branch'i üzerinde '{workflow_type}' iş akışına "
        f"izin verilmiyor (kural: {matched}, sebep: {decision.reason}). "
        "Lütfen branch'ı veya iş akışını gözden geçirin."
    )


def _format_analysis_failed_comment(reason: str) -> str:
    """LLM analysis failure comment."""

    return (
        f"❌ Görev analizi başarısız: {reason}. Lütfen issue açıklamasını "
        "netleştirip yeniden atayın."
    )


def _format_needs_info_comment(questions: tuple[str, ...]) -> str:
    """Needs-info question comment posted on the Jira issue.

 The comment lists the LLM-supplied clarification questions / missing
 fields and instructs the user to reply directly on the issue. The
 webhook dispatcher routes the reply back into the workflow as an
 ``info_received`` signal.
 """

    if questions:
        bullets = "\n".join(f"• {q}" for q in questions if q)
        body = bullets or "• (eksik bilgi belirtilmedi)"
    else:
        body = "• Lütfen görevle ilgili eksik detayları belirtin."

    return (
        "🤖 Görevi başlatabilmem için aşağıdaki bilgilere ihtiyacım "
        "var:\n\n"
        f"{body}\n\n"
        "Lütfen bu yorumun altına bilgileri yazın; otomatik olarak "
        "yakalanıp analiz edilecektir."
    )


def _format_needs_info_timeout_comment() -> str:
    """Stale / timeout comment posted when the needs_info window expires.

 — the prose explicitly mentions "7 gün"
 so the user-visible message matches:data:`_NEEDS_INFO_TIMEOUT` and the
 sibling ``agent_runner.SIGNAL_WAIT_TIMEOUT`` constant.
 """

    return (
        "⌛ 7 gün içinde yanıt alınamadı, görev `stale` durumuna "
        "alındı ve otomatik işleme kapatıldı. Tekrar başlatmak için "
        "görevi yeniden atayın."
    )


def _format_needs_info_loop_cap_comment() -> str:
    """Loop-cap comment posted when re-analysis cannot lift confidence."""

    return (
        "🛑 Görev analizi birkaç iterasyon sonra hâlâ düşük güven "
        "üretti, otomatik işleme kapatıldı. Lütfen görev tanımını "
        "netleştirip yeniden atayın."
    )


# ---------------------------------------------------------------------------
# AutomationWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="AutomationWorkflow")
class AutomationWorkflow:
    """Gateway workflow — capability gate + workflow_type routing.

 See module docstring for the full lifecycle. The class takes no
 constructor arguments (Temporal calls ``__init__`` with no
 positional args); state is built up through the:meth:`run`
 coroutine which reads everything it needs from the:class:`AutomationWorkflowInput` envelope.
 """

    def __init__(self) -> None:
        # Signal-driven state for the needs_info loop (–).
        #
        # ``_pending_comment_body`` carries the Turkish reply text the
        # webhook dispatcher forwarded via the ``info_received`` signal
        # (see:func:`platform.services.automation-service.src.webhooks.
        # dispatcher.WebhookDispatcher._signal_workflow`). The flag
        # ``_info_received`` flips ``True`` on every signal arrival and
        # the run body resets it before each ``wait_condition`` so a
        # spurious wake-up from a previous iteration cannot leak into
        # the next.
        #
        # Both attributes are simple Python primitives — Temporal's
        # determinism contract treats writes inside signal handlers
        # the same as writes inside ``run`` (the SDK records them in
        # the workflow event history), so we can read them from
        # wait-condition predicates without any extra synchronisation.
        self._info_received: bool = False
        self._pending_comment_body: str | None = None
        self._info_received_history: list[str] = []

    @workflow.signal(name=_SIGNAL_INFO_RECEIVED)
    def info_received(self, comment_body: str | None = None) -> None:
        """Signal handler for ``info_received`` (,).

 Called by the webhook dispatcher after a non-bot user posts a
 new comment on a Jira issue whose latest workflow status is
 ``needs_info``. The signal carries the raw comment body so the
 run body can append it to the issue description before the
 next ``llm_analyze_task`` call.

 Determinism: the handler does no I/O — it only mutates workflow
 state. The ``run`` body's ``wait_condition`` predicate
 observes the flip and resumes execution.

 The handler tolerates both string payloads (the dispatcher's
 normal shape) and dict payloads (used by some test fakes that
 do not unpack signal arguments). Empty / None bodies still
 flip the edge flag so the workflow does not get permanently
 stuck if a malformed signal lands; the run body simply
 re-emits the needs_info question.
 """

        if isinstance(comment_body, str):
            text = comment_body
        elif isinstance(comment_body, dict):
            # Defensive: some Temporal data converters wrap a single
            # positional payload in ``{"comment_body": "..."}``.
            text = str(comment_body.get("comment_body", "") or "")
        elif comment_body is None:
            text = ""
        else:
            text = str(comment_body)

        self._pending_comment_body = text
        if text:
            self._info_received_history.append(text)
        self._info_received = True

    @workflow.run
    async def run(
        self, inp: AutomationWorkflowInput
    ) -> AutomationWorkflowOutput:
        # The Temporal workflow info is deterministic — its ``workflow_id``
        # is derived from the start request, not the wallclock or RNG.
        info = workflow.info()
        parent_workflow_id: str = info.workflow_id

        # 1. Best-effort acknowledgement comment. A failed ack must not
        # abort the run — the issue may have been deleted between
        # webhook delivery and workflow start, and the rest of the
        # pipeline still has useful work to do for the audit trail.
        await self._best_effort_jira_comment(
            inp.issue_key, _format_ack_comment(), inp.department_id
        )

        # 2. Task analysis (—,,).
        #
        # The new ``analyze_task`` activity is the **first** action
        # the workflow takes after the ack comment. It owns the
        # full analyser pipeline:
        #
        # a. ``prepare_task_analysis_input`` — fetches the Jira
        # issue text + dept config and builds a
        #:class:`TaskAnalysisInput`. Failure here is fatal
        # (we cannot reason about a workflow we can't read).
        # b. ``analyze_task`` — runs YAML front-matter parsing
        # first (deterministic path,) and falls back to
        # the LLM when no YAML block is present. The
        # activity also enforces the workflow_type vocabulary
        #, the confidence threshold (,), and
        # the web_search downgrade. Side-effecting
        # comments (rejection / needs_info / downgrade /
        # description_parser warning) are posted by the
        # activity itself — the workflow body only reads the
        # resulting:class:`TaskAnalysisResult`.
        #
        # Hot-reload of ``platform/prompts/task_analysis.md``
        # is provided by the ``PromptCache`` *inside* the
        # activity (mtime-aware). The workflow always issues a
        # fresh ``execute_activity`` call workflow start, so
        # operators editing the prompt do not need to restart the
        # worker — the activity re-reads the file when the mtime
        # changes (verified by ``test_task_analyzer.py``::
        # ``test_prompt_cache_reload_on_mtime_change``).
        analysis_or_stop = await self._run_task_analyser(inp)
        if isinstance(analysis_or_stop, _AutomationStop):
            return await self._finalize_stop(inp, analysis_or_stop)
        task_result, override = analysis_or_stop

        # 2a. Bridge: convert the analyser's:class:`TaskAnalysisResult`
        # to the older:class:`LlmAnalysisResult` shape consumed
        # by the existing capability-gate / branch-rules / dispatch
        # pipeline. The bridge is a pure mapping (see
        #:func:`description_override.to_llm_analysis_result`) so
        # it has no replay-determinism concerns.
        from automation_worker.workflows.description_override import (  # noqa: PLC0415
            to_llm_analysis_result,
        )

        analysis: LlmAnalysisResult = to_llm_analysis_result(task_result)

        # 2.5. Needs-info loop (–). Defensive — the analyser
        # activity already short-circuits low-confidence outputs
        # into its own ``needs_info`` status (handled in step 2),
        # but if a future change ever surfaces a ``"low"`` LLM
        # confidence to the bridge we still want the existing
        # Temporal-signal-driven loop to wake the workflow on a
        # reply comment (added by). The fast-path guard
        # inside:meth:`_handle_needs_info_loop` returns the
        # analysis untouched when no needs_info handling is
        # required, so this call is free in the common case.
        analysis_or_stop = await self._handle_needs_info_loop(
            inp=inp, analysis=analysis
        )
        if isinstance(analysis_or_stop, _AutomationStop):
            return await self._finalize_stop(inp, analysis_or_stop)
        analysis = analysis_or_stop

        workflow_type = analysis.workflow_type

        # 3. Capability gate. The simple-vocabulary helpers translate
        # the split capability table (``"jira_read"`` /
        # ``"jira_write"`` /...) into the simple service vocabulary
        # (``"jira"`` / ``"bitbucket"`` /...) carried in
        # ``inp.available_capabilities`` so the workflow can compare
        # set-against-set without re-implementing the mapping.
        try:
            required = required_capabilities(workflow_type)
        except KeyError:
            # Unknown workflow_type — the LLM strayed outside the
            # 10-entry vocabulary. Treat as a denial so the user sees
            # an explanation rather than the workflow looping.
            if workflow_type in WORKFLOW_TYPE_CAPABILITIES:
                # Defensive: capability table reports the type as known
                # but the simple-name helper rejects it. This should
                # never happen — if it does, surface the contradiction
                # as a failure rather than a silent denial.
                stop = await self._stop_with_audit(
                    inp=inp,
                    decision="failed",
                    workflow_type=workflow_type,
                    failure_reason="capability_table_inconsistency",
                    summary=(
                        f"workflow_type {workflow_type!r} present in "
                        "WORKFLOW_TYPE_CAPABILITIES but rejected by "
                        "required_capabilities — table inconsistency."
                    ),
                    jira_message=_format_analysis_failed_comment(
                        "iç tutarsızlık — operatöre bildirildi"
                    ),
                )
            else:
                stop = await self._stop_with_audit(
                    inp=inp,
                    decision="denied",
                    workflow_type=workflow_type,
                    failure_reason="unknown_workflow_type",
                    summary=f"unknown workflow_type {workflow_type!r}",
                    jira_message=_format_unknown_workflow_type_comment(
                        workflow_type
                    ),
                )
            return await self._finalize_stop(inp, stop)

        available = frozenset(inp.available_capabilities)
        missing = missing_capabilities(required, available)
        if missing:
            missing_tuple = tuple(sorted(missing))
            stop = await self._stop_with_audit(
                inp=inp,
                decision="denied",
                workflow_type=workflow_type,
                failure_reason="missing_capability",
                summary=(
                    f"missing capabilities for {workflow_type}: "
                    + ", ".join(missing_tuple)
                ),
                jira_message=_format_missing_caps_comment(
                    workflow_type, missing_tuple
                ),
                missing_capabilities=missing_tuple,
            )
            return await self._finalize_stop(inp, stop)

        # 4. branch_pattern_rules — only applies to code-change /
        # pr_review workflow types that operate on a Git branch.
        # Skip for confluence / research / noop / multi_step.
        if (
            workflow_type in _BRANCH_AWARE_WORKFLOW_TYPES
            and analysis.target_branch
        ):
            decision = await self._evaluate_branch_pattern_rules(
                department_id=inp.department_id,
                branch=analysis.target_branch,
                workflow_type=workflow_type,
            )
            if not decision.allowed:
                stop = await self._stop_with_audit(
                    inp=inp,
                    decision="out_of_scope",
                    workflow_type=workflow_type,
                    failure_reason="branch_rule_denied",
                    summary=(
                        f"branch_pattern_rules denied {workflow_type} on "
                        f"{analysis.target_branch}: {decision.reason}"
                    ),
                    jira_message=_format_branch_rule_denied_comment(
                        workflow_type, analysis.target_branch, decision
                    ),
                )
                return await self._finalize_stop(inp, stop)

        # 5. Dispatch the appropriate child workflow.
        child_spec = self._build_child_spec(
            parent_workflow_id=parent_workflow_id,
            inp=inp,
            analysis=analysis,
        )

        try:
            child_handle = await workflow.start_child_workflow(
                child_spec.workflow_name,
                args=list(
                    self._child_args(
                        child_spec, inp, analysis, override=override
                    )
                ),
                id=child_spec.workflow_id,
                task_queue=child_spec.task_queue,
                parent_close_policy=getattr(
                    workflow.ParentClosePolicy,
                    child_spec.parent_close_policy,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            stop = await self._stop_with_audit(
                inp=inp,
                decision="failed",
                workflow_type=workflow_type,
                failure_reason="child_dispatch_failed",
                summary=f"start_child_workflow failed: {exc}",
                jira_message=_format_analysis_failed_comment(
                    f"alt iş akışı başlatılamadı: {exc}"
                ),
            )
            return await self._finalize_stop(inp, stop)

        # ``noop_test`` is the only workflow type for which the
        # gateway opts in to awaiting the child's terminal status —
        # The gateway otherwise treats dispatch as its responsibility,
        # with each child reporting
        # its own outcome. The smoke-test path (—)
        # verifies the full pipeline (webhook → AutomationWorkflow →
        # ExecutionRunWorkflow → SSH runner → Jira comment) end-to-end,
        # so the gateway awaits the:class:`ExecutionRunWorkflow`
        # child and, on success, posts the captured stdout / exit code
        # back to the triggering Jira issue via the
        # ``noop_test_post_result`` activity.
        #
        # All other workflow types (code change / pr_review /
        # confluence / research / multi_step / remote_ssh_test_only)
        # remain dispatch-and-forget so the gateway holds no long
        # workflow slot.
        if workflow_type == "noop_test":
            await self._await_noop_test_and_post_result(
                child_handle=child_handle,
                inp=inp,
            )
        elif (
            workflow_type in {"remote_ssh_test_only", "script_execute"}
            and analysis.output_actions
        ):
            # — execution-runner
            # output_actions wire-in. When the analyser surfaced
            # output actions for a ``remote_ssh_test_only`` run
            # (e.g. ``jira_attachment`` to publish stdout, or
            # ``confluence_page`` to capture results) the gateway
            # awaits the child:class:`ExecutionRunWorkflow` so it
            # can:
            #
            # * synthesise MinIO bucket/key params for any
            # ``jira_attachment`` action that did not carry them
            # explicitly (so the executor can stream stdout /
            # stderr from MinIO without the analyser knowing the
            # workflow_id ahead of time), and
            # * dispatch the resulting:class:`ExecutionBatchInput`
            # to the ``execute_output_actions`` activity.
            #
            # Empty:attr:`LlmAnalysisResult.output_actions` keeps
            # the dispatch-and-forget contract verbatim — this branch
            # is opt-in by the analyser (/ Description Parser
            # ``output:`` block), so existing remote_ssh_test_only
            # runs without an output_actions stanza behave exactly
            # as before.
            await self._await_execution_run_and_publish_results(
                child_handle=child_handle,
                inp=inp,
                child_workflow_id=child_spec.workflow_id,
                actions=analysis.output_actions,
            )

        # Successful dispatch.
        success_summary = (
            f"Dispatched {child_spec.workflow_name} "
            f"({workflow_type}) for {inp.issue_key}"
        )
        # Best-effort success-gated notification dispatch. A successful gateway dispatch lands
        # on the success path: ``status="completed"`` makes the
        # dispatcher consult ``inp.notify_on_success`` and the
        # ``inp.notify_channels`` set; departments that opted out
        # see a no-op, opted-in departments get the
        # rendered Slack / email body.
        await self._dispatch_notification(
            inp=inp,
            status="completed",
            summary=success_summary,
            error=None,
        )

        return AutomationWorkflowOutput(
            decision="dispatched",
            workflow_type=workflow_type,
            child_workflow_id=child_spec.workflow_id,
            summary=success_summary,
            failure_reason=None,
            missing_capabilities=(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _best_effort_jira_comment(
        self, issue_key: str, body: str, department_id: str
    ) -> None:
        """Post a Jira comment, swallowing failures.

 Used by the ack-comment step where a missing comment must
 never abort the workflow. All other ``jira_add_comment``
 calls in this module use the regular activity invocation
 path so failures propagate.

 An empty ``body`` is treated as a no-op so callers (notably:meth:`_run_task_analyser` for the ``rejected`` branch) can
 skip a redundant comment when the analyser activity has
 already posted its own user-facing message.
 """

        if not body:
            return

        try:
            await workflow.execute_activity(
                _ACT_JIRA_ADD_COMMENT,
                args=[issue_key, body, department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "jira_add_comment failed for %s — continuing", issue_key
            )

    async def _best_effort_jira_transition(
        self, issue_key: str, target_status: str, department_id: str
    ) -> None:
        """Transition a Jira issue, swallowing failures.

 Status transitions are best-effort: the workflow's terminal
 decision is the source of truth and Jira may converge later.
 Used by the needs_info loop to flip the issue to ``needs_info``
 / ``stale`` (,).
 """

        try:
            await workflow.execute_activity(
                _ACT_JIRA_TRANSITION_ISSUE,
                args=[issue_key, target_status, department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "jira_transition_issue(%s -> %s) failed — continuing",
                issue_key,
                target_status,
            )

    async def _run_task_analyser(
        self,
        inp: AutomationWorkflowInput,
    ) -> "tuple[TaskAnalysisResult, Any] | _AutomationStop":
        """Run the analyser front-door.

 Replaces the legacy direct ``llm_analyze_task`` call with the
 two-stage analyser introduced by:

 1. ``prepare_task_analysis_input`` — fetches the Jira issue
 text + dept config and packages them into a:class:`TaskAnalysisInput` (the activity does the I/O so
 the workflow body stays deterministic).
 2. ``analyze_task`` — runs the YAML front-matter parser
 first (deterministic path) and falls back to the LLM
 on miss. Activity-side comments
 (``rejected`` / ``needs_info`` / ``downgrade`` /
 description_parser warning) are posted by the activity
 itself.

 Branching contract (,):

 * ``accepted=False`` and ``status="rejected"`` → return an:class:`_AutomationStop` with ``decision="denied"`` and
 ``failure_reason="task_analysis_rejected"``. The activity
 has already posted the explanation comment to Jira; we
 only need to record the audit row and end the workflow.
 * ``status="needs_info"`` → enter the signal-driven wait
 loop. The webhook dispatcher emits ``info_received`` when
 a non-bot user replies (/ — wiring).
 On signal the helper re-runs steps 1 + 2 with the new
 comment text appended; on 24h timeout the issue
 transitions to ``stale``; on three consecutive
 ``needs_info`` results the workflow terminates with
 ``loop_cap_reached``. The signal flag and pending body
 live on the same workflow attributes used by the legacy:meth:`_handle_needs_info_loop` so the dispatcher's
 existing wiring keeps working without changes.
 * ``status="ready"`` → return the:class:`TaskAnalysisResult` together with the distilled:class:`DescriptionOverride`.

 Hot-reload: the prompt cache lives inside:func:`automation_worker.activities.task_analyzer.analyze_task`
 and is mtime-aware. Each workflow start issues a fresh
 ``execute_activity`` call so an admin editing the prompt
 sees the new text on the *next* analysis without a worker
 restart — the workflow body never caches the activity result.

 Returns
 -------
 tuple[TaskAnalysisResult, DescriptionOverride] | _AutomationStop
 Successful analysis: ``(task_result, override)`` where
 both objects carry already-merged dept defaults.
 Rejected / timed-out / cap-exhausted: an:class:`_AutomationStop` envelope ready for:meth:`_stop_to_output`.
 """

        # Local imports keep the static AST determinism scanner happy
        # — these are pure helpers / dataclass shapes, not activity
        # callables.
        from automation_worker.workflows.description_override import (  # noqa: PLC0415
            DescriptionOverride,
            build_description_override,
        )

        # ---- Iteration 0: initial prepare + analyse --------------
        prepared = await self._prepare_and_analyse(
            inp=inp, comment_body=None
        )
        if isinstance(prepared, _AutomationStop):
            return prepared
        task_result = prepared

        # Rejected on the first pass — surface the denial with the
        # standard ``task_analysis_rejected`` reason. The activity
        # already posted the rejection comment so we only need the
        # audit + structured output.
        if (
            not task_result.accepted
            and task_result.status == "rejected"
        ):
            return await self._stop_with_audit(
                inp=inp,
                decision="denied",
                workflow_type=task_result.workflow_type,
                failure_reason="task_analysis_rejected",
                summary=(
                    task_result.error
                    or "task analyzer rejected the request"
                ),
                # Activity already posted the rejection comment; pass
                # an empty string so the audit-side helper does not
                # double-post. ``_best_effort_jira_comment`` treats
                # empty bodies as no-ops by skipping the activity call.
                jira_message="",
            )

        # ---- Needs-info loop --------------------------------------
        if task_result.status == "needs_info":
            # The activity has already posted the missing-fields
            # comment and transitioned the issue to ``needs_info`` is
            # *not* the activity's responsibility (it's a workflow
            # side effect — the activity stays read-only on Jira state
            # transitions). Flip the status here so the dispatcher's
            # ``[iterate]`` / ``info_received`` routing recognises the
            # waiting state.
            await self._best_effort_jira_transition(
                inp.issue_key,
                "needs_info",
                inp.department_id,
            )

            for iteration in range(_NEEDS_INFO_MAX_ITERATIONS):
                # Audit the park so operators see the workflow paused
                # intentionally (rather than crashed).
                try:
                    await workflow.execute_activity(
                        _ACT_AUDIT_WRITE,
                        args=[
                            {
                                "action": "task_analyser_needs_info_parked",
                                "actor_role": "system",
                                "department_id": inp.department_id,
                                "issue_key": inp.issue_key,
                                "workflow_type": (
                                    task_result.workflow_type
                                ),
                                "iteration": iteration + 1,
                                "missing_fields": list(
                                    task_result.missing_fields
                                ),
                                "confidence": task_result.confidence,
                                "source": task_result.source,
                            }
                        ],
                        start_to_close_timeout=_SHORT_TIMEOUT,
                        retry_policy=_DEFAULT_RETRY,
                    )
                except Exception:  # noqa: BLE001 — audit best-effort
                    workflow.logger.warning(
                        "audit_write(task_analyser_needs_info_parked) "
                        "failed for %s",
                        inp.issue_key,
                    )

                # Reset the edge flag *before* the wait so a stale flip
                # from a previous iteration cannot race ahead of the
                # timeout.
                self._info_received = False
                self._pending_comment_body = None

                try:
                    await workflow.wait_condition(
                        lambda: self._info_received,
                        timeout=_NEEDS_INFO_TIMEOUT,
                    )
                except TimeoutError:
                    workflow.logger.warning(
                        "AutomationWorkflow: task_analyser needs_info "
                        "timed out for %s after 24h — marking stale.",
                        inp.issue_key,
                    )
                    await self._best_effort_jira_transition(
                        inp.issue_key, "stale", inp.department_id
                    )
                    return await self._stop_with_audit(
                        inp=inp,
                        decision="failed",
                        workflow_type=task_result.workflow_type,
                        failure_reason="needs_info_timeout",
                        summary=(
                            "task_analyser needs_info timed out after "
                            f"24h (iteration {iteration + 1})"
                        ),
                        jira_message=(
                            _format_needs_info_timeout_comment()
                        ),
                    )

                comment_body = self._pending_comment_body or ""

                # Re-run the analyser with the new comment text so the
                # activity can see the additional context.
                prepared = await self._prepare_and_analyse(
                    inp=inp, comment_body=comment_body
                )
                if isinstance(prepared, _AutomationStop):
                    return prepared
                task_result = prepared

                if (
                    not task_result.accepted
                    and task_result.status == "rejected"
                ):
                    return await self._stop_with_audit(
                        inp=inp,
                        decision="denied",
                        workflow_type=task_result.workflow_type,
                        failure_reason="task_analysis_rejected",
                        summary=(
                            task_result.error
                            or "task analyzer rejected after re-analysis"
                        ),
                        jira_message="",
                    )

                if task_result.status == "ready":
                    break

                # Still needs_info — loop and wait again. The
                # activity posted a refreshed missing-fields comment
                # on this iteration too.
                continue
            else:
                # Loop cap reached — three re-analyses still produced
                # ``needs_info``. Post the cap comment and terminate.
                return await self._stop_with_audit(
                    inp=inp,
                    decision="failed",
                    workflow_type=task_result.workflow_type,
                    failure_reason="loop_cap_reached",
                    summary=(
                        "task_analyser needs_info loop cap reached "
                        f"after {_NEEDS_INFO_MAX_ITERATIONS} iterations"
                    ),
                    jira_message=(
                        _format_needs_info_loop_cap_comment()
                    ),
                )

        # ---- Final guard ------------------------------------------
        # Defensive — unknown status the workflow does not know how to
        # route. Treat as a failed analysis so the operator sees a
        # clear audit row instead of a silently dropped task.
        if task_result.status != "ready":
            return await self._stop_with_audit(
                inp=inp,
                decision="failed",
                workflow_type=task_result.workflow_type,
                failure_reason="task_analysis_failed",
                summary=(
                    f"task analyzer returned unexpected status "
                    f"{task_result.status!r}"
                ),
                jira_message=_format_analysis_failed_comment(
                    f"beklenmeyen analiz durumu: {task_result.status}"
                ),
            )

        override = build_description_override(task_result)

        # ---- Epic auto-detect audit ----------------
        # When the task analyzer deterministically assigned multi_step
        # because the issue is an Epic with subtasks, emit an audit
        # event so operators can trace the decision.
        if task_result.source == "epic_auto_detect" and task_result.status == "ready":
            # Extract subtask_count from the metadata entry placed by
            # _result_for_epic_multi_step in output_actions.
            _subtask_count = 0
            for _oa in (task_result.output_actions or []):
                if isinstance(_oa, dict) and _oa.get("_meta") == "epic_auto_detect":
                    _subtask_count = _oa.get("subtask_count", 0)
                    break
            try:
                await workflow.execute_activity(
                    _ACT_AUDIT_WRITE,
                    args=[
                        {
                            "action": "epic_auto_detected_multi_step",
                            "actor_role": "system",
                            "department_id": inp.department_id,
                            "issue_key": inp.issue_key,
                            "payload": {
                                "issue_key": inp.issue_key,
                                "subtask_count": _subtask_count,
                            },
                        }
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception:  # noqa: BLE001 — audit best-effort
                workflow.logger.warning(
                    "audit_write(epic_auto_detected_multi_step) "
                    "failed for %s — continuing",
                    inp.issue_key,
                )

        return task_result, override

    async def _prepare_and_analyse(
        self,
        *,
        inp: AutomationWorkflowInput,
        comment_body: str | None,
    ) -> "TaskAnalysisResult | _AutomationStop":
        """One round of ``prepare_task_analysis_input`` + ``analyze_task``.

 Failure on either activity is converted to a structured:class:`_AutomationStop` so the caller can keep its branching
 linear. Both activities are retried via the standard policies
 (LLM activity uses a dedicated retry to keep token-cap
 violations non-retryable).

 Args:
 inp: Original:class:`AutomationWorkflowInput`.
 comment_body: Latest user comment to append to the
 analyser context, or ``None`` for the initial pass.
 The ``prepare_task_analysis_input`` activity is
 responsible for splicing the body into the issue
 description before invoking the LLM.

 Returns:
 ``TaskAnalysisResult`` on success, or an:class:`_AutomationStop` on hard failure.
 """

        # 1. prepare_task_analysis_input — fetches issue + dept config.
        try:
            analysis_input: Any = await workflow.execute_activity(
                _ACT_PREPARE_TASK_ANALYSIS_INPUT,
                args=[inp, comment_body or ""],
                result_type=TaskAnalysisInput,
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "prepare_task_analysis_input failed for %s: %s",
                inp.issue_key,
                exc,
            )
            return await self._stop_with_audit(
                inp=inp,
                decision="failed",
                workflow_type=None,
                failure_reason="task_analysis_failed",
                summary=f"prepare_task_analysis_input failed: {exc}",
                jira_message=_format_analysis_failed_comment(
                    f"görev bilgisi yüklenemedi: {exc}"
                ),
            )

        # 2. analyze_task — runs the YAML / LLM analyser pipeline.
        try:
            task_result: TaskAnalysisResult = (
                await workflow.execute_activity(
                    _ACT_ANALYZE_TASK,
                    args=[analysis_input],
                    result_type=TaskAnalysisResult,
                    start_to_close_timeout=_LLM_TIMEOUT,
                    retry_policy=_LLM_RETRY,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return await self._stop_with_audit(
                inp=inp,
                decision="failed",
                workflow_type=None,
                failure_reason="task_analysis_failed",
                summary=f"analyze_task failed: {exc}",
                jira_message=_format_analysis_failed_comment(str(exc)),
            )

        return task_result

    async def _handle_needs_info_loop(
        self,
        *,
        inp: AutomationWorkflowInput,
        analysis: LlmAnalysisResult,
    ) -> "LlmAnalysisResult | _AutomationStop":
        """Run the needs_info wait + re-analysis loop.

 The loop only runs when the most recent ``llm_analyze_task``
 result is ambiguous — i.e. ``confidence == "low"`` *and* at
 least one ``needs_info_questions`` entry is present. Anything
 else (high / medium confidence, or low confidence with no
 questions to ask) is returned to the caller untouched so the
 gateway proceeds straight to the capability gate.

 Behaviour matches:class:`AgentRunnerWorkflow.run` needs_info loop
 in shape but is bounded by a 24h timeout
 and a three-iteration cap. Each iteration:

 1. Posts a Turkish comment listing the missing fields and
 transitions the Jira issue to ``needs_info``.
 2. Audits the parking event so operators see the workflow
 paused intentionally (rather than crashed).
 3. Resets the signal edge flag, then ``wait_condition``s for
 ``info_received`` or the 24h timeout.
 4. On signal: build a synthetic input that appends the new
 comment text to the description and re-runs
 ``llm_analyze_task``. If the re-analysis returns
 non-low confidence (or runs out of questions) the loop
 exits with the resolved analysis.
 5. On 24h timeout: transition the issue to ``stale``, post the
 timeout comment, audit the stale event, and return a
 ``failed``:class:`_AutomationStop`.
 6. After three consecutive low-confidence re-analyses, post
 the loop-cap comment, audit the cap, and return a
 ``failed``:class:`_AutomationStop` with
 ``failure_reason="loop_cap_reached"``.

 Determinism: the body uses only ``workflow.execute_activity``
 (string-named) and ``workflow.wait_condition`` (Temporal
 timer); no ``random`` / ``uuid`` / direct I/O. The signal
 edge flag (``self._info_received``) is reset *before* each
 wait so a stale flip from a previous iteration cannot leak
 across boundaries.
 """

        # Fast path: nothing to ask, nothing to wait for.
        if (
            analysis.confidence != "low"
            or not analysis.needs_info_questions
        ):
            return analysis

        for iteration in range(_NEEDS_INFO_MAX_ITERATIONS):
            # --- 1. Post the question + transition to needs_info ----
            await self._best_effort_jira_comment(
                inp.issue_key,
                _format_needs_info_comment(analysis.needs_info_questions),
                inp.department_id,
            )
            await self._best_effort_jira_transition(
                inp.issue_key, "needs_info", inp.department_id
            )

            # --- 2. Audit the park ----------------------------------
            try:
                await workflow.execute_activity(
                    _ACT_AUDIT_WRITE,
                    args=[
                        {
                            "action": "automation_needs_info_parked",
                            "actor_role": "system",
                            "department_id": inp.department_id,
                            "issue_key": inp.issue_key,
                            "workflow_type": analysis.workflow_type,
                            "iteration": iteration + 1,
                            "questions": list(
                                analysis.needs_info_questions
                            ),
                        }
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception:  # noqa: BLE001 — audit best-effort
                workflow.logger.warning(
                    "audit_write(needs_info_parked) failed for %s",
                    inp.issue_key,
                )

            # --- 3. Wait for signal or 24h timeout -------------------
            # Reset the edge flag *before* the wait so a spurious
            # flip from a previous iteration does not race ahead
            # of the timeout.
            self._info_received = False
            self._pending_comment_body = None

            try:
                await workflow.wait_condition(
                    lambda: self._info_received,
                    timeout=_NEEDS_INFO_TIMEOUT,
                )
            except TimeoutError:
                # 24h elapsed — transition to stale and terminate.
                workflow.logger.warning(
                    "AutomationWorkflow: needs_info timed out for %s "
                    "after 24h — marking stale.",
                    inp.issue_key,
                )
                await self._best_effort_jira_transition(
                    inp.issue_key, "stale", inp.department_id
                )
                return await self._stop_with_audit(
                    inp=inp,
                    decision="failed",
                    workflow_type=analysis.workflow_type,
                    failure_reason="needs_info_timeout",
                    summary=(
                        "needs_info timed out after 24h — issue marked "
                        f"stale (iteration {iteration + 1})"
                    ),
                    jira_message=_format_needs_info_timeout_comment(),
                )

            comment_body = self._pending_comment_body or ""

            # --- 4. Re-analyse with the new comment appended --------
            updated_inp = self._with_comment_appended(inp, comment_body)
            try:
                next_analysis: LlmAnalysisResult = (
                    await workflow.execute_activity(
                        _ACT_LLM_ANALYZE_TASK,
                        args=[updated_inp],
                        result_type=LlmAnalysisResult,
                        start_to_close_timeout=_LLM_TIMEOUT,
                        retry_policy=_LLM_RETRY,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                # Re-analysis failure ends the workflow with the
                # standard ``task_analysis_failed`` reason — same path
                # as the initial analysis failure in:meth:`run`.
                return await self._stop_with_audit(
                    inp=inp,
                    decision="failed",
                    workflow_type=analysis.workflow_type,
                    failure_reason="task_analysis_failed",
                    summary=f"needs_info re-analysis failed: {exc}",
                    jira_message=_format_analysis_failed_comment(str(exc)),
                )

            analysis = next_analysis

            # If the re-analysis lifted confidence (or ran out of
            # questions) the gateway can proceed to the capability
            # gate. Both branches return up to:meth:`run`.
            if (
                analysis.confidence != "low"
                or not analysis.needs_info_questions
            ):
                return analysis

            # Otherwise loop and ask again — the next iteration will
            # post the (possibly refined) questions.

        # Loop cap reached — three re-analyses still produced low
        # confidence. Post the cap comment and terminate.
        return await self._stop_with_audit(
            inp=inp,
            decision="failed",
            workflow_type=analysis.workflow_type,
            failure_reason="loop_cap_reached",
            summary=(
                f"needs_info loop cap reached after "
                f"{_NEEDS_INFO_MAX_ITERATIONS} iterations"
            ),
            jira_message=_format_needs_info_loop_cap_comment(),
        )

    @staticmethod
    def _with_comment_appended(
        inp: AutomationWorkflowInput, comment_body: str
    ) -> AutomationWorkflowInput:
        """Return a copy of ``inp`` with the comment text noted.:class:`AutomationWorkflowInput` does not carry a description
 / comment-history field directly — the gateway only forwards
 ``issue_key`` and the department envelope to
 ``llm_analyze_task``, which fetches the issue text inside the
 activity. Most production callers therefore pass the comment
 through the issue body (the user replied to the ticket) and
 this helper is a no-op pass-through.

 Tests, however, may stub the LLM activity to inspect a
 ``raw_event`` field —:class:`WebhookEvent` is the closest
 carrier on the input — so the helper preserves it as-is and
 relies on the activity to re-fetch. The method is kept
 pure (no time / no I/O) so it is replay-safe and trivially
 testable.

 Determinism: returning the original input when the comment
 body is empty avoids spurious re-replays of the same
 re-analysis with subtly different inputs.
 """

        del comment_body  # currently unused — see docstring
        return inp

    async def _await_noop_test_and_post_result(
        self,
        *,
        child_handle: Any,
        inp: AutomationWorkflowInput,
    ) -> None:
        """Await a ``noop_test``:class:`ExecutionRunWorkflow` child and
 post its result back to Jira (—).

 ``noop_test`` is the **only** workflow type for which the
 gateway opts in to awaiting its child — every other type is
 dispatch-and-forget. The smoke-test path proves the full
 pipeline (webhook → AutomationWorkflow → ExecutionRunWorkflow
 → SSH runner → Jira comment) without any LLM or code-change
 logic, so that newly-onboarded depts can verify end-to-end
 connectivity.

 Behaviour:

 * Wait for the child workflow handle to complete. Any
 exception raised by the child is swallowed and audited —
 the smoke-test path must not turn a runner outage into a
 gateway-level failure that paged the operator. The
 ``decision`` returned by the parent stays
 ``"dispatched"`` because the child *was* dispatched
 successfully; the runner-side outcome is reported via the
 Jira comment.
 * On a successful return, invoke the
 ``noop_test_post_result`` activity with the captured
 stdout URI and exit code so the activity body can fetch
 the runner output (or treat it as opaque text) and post
 a Jira comment. Activity failures are also swallowed —
 the comment is best-effort, the gateway has already
 recorded ``automation_dispatched`` in the audit log.

 The helper accepts the handle as ``Any`` because Temporal's:class:`ChildWorkflowHandle` is parameterised by the child's
 result type and importing the generic from inside the
 sandbox escape hatch adds noise without buying type safety.

 Determinism: this method only ``await``s a child workflow
 handle and an activity by string name, both of which are
 replay-safe Temporal primitives.
 """

        try:
            result: ExecutionRunWorkflowOutput | Any = await child_handle
        except Exception as exc:  # noqa: BLE001 — surface in audit only
            workflow.logger.warning(
                "noop_test child workflow failed for %s: %s",
                inp.issue_key,
                exc,
            )
            return

        # Best-effort coercion: tests that wire a stub child workflow
        # may return a plain dict instead of a fully-typed
        #:class:`ExecutionRunWorkflowOutput`. We treat both shapes
        # identically — the activity sees the captured stdout / exit
        # code regardless of how the child encoded them.
        stdout = self._extract_noop_stdout(result)
        exit_code = self._extract_noop_exit_code(result)

        try:
            await workflow.execute_activity(
                _ACT_NOOP_TEST_POST_RESULT,
                args=[
                    inp.issue_key,
                    inp.department_id,
                    stdout,
                    exit_code,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "noop_test_post_result failed for %s — continuing",
                inp.issue_key,
            )

    @staticmethod
    def _extract_noop_stdout(result: Any) -> str:
        """Pull the captured stdout (or its URI) out of the child result.

 Production wires this to:class:`ExecutionRunWorkflowOutput`
 (whose ``stdout_uri`` field carries the MinIO key written by
 the SSH runner activity). Tests may return a plain dict
 with ``stdout`` or ``stdout_uri`` keys; we honour both shapes
 and fall back to ``""`` when neither is present so the
 activity always receives a string.
 """

        if result is None:
            return ""
        if isinstance(result, ExecutionRunWorkflowOutput):
            return result.stdout_uri or ""
        if isinstance(result, dict):
            for key in ("stdout", "stdout_uri"):
                value = result.get(key)
                if isinstance(value, str):
                    return value
            return ""
        # Unknown shape — best-effort string coercion keeps the
        # activity contract intact even when a future child workflow
        # returns a different envelope.
        stdout_uri = getattr(result, "stdout_uri", None)
        if isinstance(stdout_uri, str):
            return stdout_uri
        stdout = getattr(result, "stdout", None)
        if isinstance(stdout, str):
            return stdout
        return ""

    @staticmethod
    def _extract_noop_exit_code(result: Any) -> int | None:
        """Pull the runner exit code out of the child result.

 Mirrors:meth:`_extract_noop_stdout` for the
 ``exit_code`` field. Returns ``None`` when the child did
 not surface an exit code (the activity treats this as
 ``"n/a"`` when formatting the Jira comment).
 """

        if result is None:
            return None
        if isinstance(result, ExecutionRunWorkflowOutput):
            return result.exit_code
        if isinstance(result, dict):
            value = result.get("exit_code")
            if isinstance(value, int):
                return value
            return None
        value = getattr(result, "exit_code", None)
        if isinstance(value, int):
            return value
        return None

    # ------------------------------------------------------------------
    # ExecutionRunWorkflow result publishing (— output_actions
    # wire-in for remote_ssh_test_only)
    # ------------------------------------------------------------------

    async def _await_execution_run_and_publish_results(
        self,
        *,
        child_handle: Any,
        inp: AutomationWorkflowInput,
        child_workflow_id: str,
        actions: tuple[OutputAction, ...],
    ) -> None:
        """Await ``ExecutionRunWorkflow`` child and publish its results.

 Bridges the analyser-supplied:class:`OutputAction` list onto
 the:func:`execute_output_actions` activity once the child:class:`ExecutionRunWorkflow` has produced its:class:`ExecutionRunWorkflowOutput`. The bridge resolves a
 few small mismatches between the two contracts:

 * The analyser's:class:`OutputAction` carries
 ``payload`` (tuple-of-pairs) keyed by the:data:`OutputActionKind` literals
 (``"jira_comment"`` / ``"jira_attachment"`` /...);
 the executor activity consumes:class:`ExecutorOutputAction` keyed by:class:`db_shared.enums.ActionType`. The bridge maps
 one to the other (with ``bitbucket_create_pr`` collapsing
 to:data:`ActionType.BITBUCKET_PR`, and
 ``confluence_create_page`` /
 ``confluence_update_page`` both collapsing to:data:`ActionType.CONFLUENCE_PAGE` — the executor
 dispatches based on the presence of ``page_id`` in
 ``params`` rather than the kind literal).

 * Jira-attachment params often need to reference the
 stdout/stderr artifacts the SSH runner uploaded to MinIO.
 The analyser cannot know the runner's child ``workflow_id``
 ahead of time, so the bridge synthesises ``bucket`` /
 ``key`` params from:func:`temporal_shared.identifiers.execution_artifact_key`
 for any ``jira_attachment`` action that did not carry an
 explicit MinIO reference (``bucket`` + ``key``) or local
 ``file_path``. The default attachment is the captured
 stdout (``stdout.log``); operators wanting stderr can opt
 in by setting ``params={"key_name": "stderr.log"}``.

 Failure modes (every branch is best-effort):

 * Child workflow failure — logged + audited; we still
 attempt to publish whatever artifacts the runner did
 upload (the SSH activity always tries to upload
 stdout / stderr even when the command itself fails, so the
 MinIO key is usually present even with ``exit_code != 0``).
 * Empty ``actions`` — the helper short-circuits inside:meth:`run` (``analysis.output_actions`` is checked before
 calling), so by the time we get here at least one action
 is present. We still defend against an unexpected empty
 tuple by returning early.
 * Executor activity failure — logged; the gateway's
 ``decision="dispatched"`` outcome remains correct because
 the child workflow itself was dispatched successfully.

 Determinism: only awaits a child workflow handle and a
 single ``workflow.execute_activity`` call — both replay-safe.
 ``execution_artifact_key`` is a pure string formatter.
 """

        if not actions:
            return

        # Await the child to pick up its MinIO artifact URIs. Any
        # exception is captured and audit-logged but does not abort
        # the publish branch — the SSH activity uploads stdout /
        # stderr even when the command exits non-zero, so most
        # ``jira_attachment`` actions can still proceed against the
        # known bucket/key derived from the child id.
        result: ExecutionRunWorkflowOutput | Any
        try:
            result = await child_handle
        except Exception as exc:  # noqa: BLE001 — log + continue
            workflow.logger.warning(
                "remote_ssh_test_only child workflow failed for %s: "
                "%s — attempting output_actions publish anyway",
                inp.issue_key,
                exc,
            )
            result = None

        stdout_uri = self._extract_execution_stdout_uri(result)
        stderr_uri = self._extract_execution_stderr_uri(result)

        # Resolve default MinIO key for actions that omit explicit
        # bucket/key. ``execution_artifact_key(child_id, "stdout.log")``
        # mirrors the path the SSH activity would have written to
        # if the runner picked up a structured prefix; for the
        # canonical ExecutionRunWorkflow path the URI is in fact
        # ``s3://{bucket}/{prefix}/stdout.txt``, so we honour the
        # child output's ``stdout_uri`` first and only fall back to
        # the deterministic key when the child did not surface one.
        default_stdout_bucket, default_stdout_key = self._split_minio_uri(
            stdout_uri,
            fallback_key=execution_artifact_key(
                child_workflow_id, "stdout.log"
            ),
        )
        default_stderr_bucket, default_stderr_key = self._split_minio_uri(
            stderr_uri,
            fallback_key=execution_artifact_key(
                child_workflow_id, "stderr.log"
            ),
        )

        executor_actions: list[ExecutorOutputAction] = []
        for index, action in enumerate(actions):
            try:
                action_type = self._kind_to_action_type(action.kind)
            except ValueError:
                workflow.logger.warning(
                    "remote_ssh_test_only: skipping output action "
                    "with unmappable kind=%r",
                    action.kind,
                )
                continue

            params = self._payload_to_params(action.payload)

            # Issue key + dept_id are workflow-level constants; let
            # the executor see them even when the analyser left them
            # off (the LLM cannot know the issue key ahead of time
            # for description-driven flows, and the executor still
            # needs them to dispatch the MCP call).
            params.setdefault("issue_key", inp.issue_key)
            params.setdefault("dept_id", inp.department_id)

            if action_type is ActionType.JIRA_ATTACHMENT:
                # Synthesise MinIO references when missing so the
                # executor's MinIO pipeline can pick them up. Prefer
                # the child output's actual URI, falling back to the
                # deterministic ``execution_artifact_key`` when the
                # child did not surface one (e.g. runner unreachable
                # before upload).
                if "file_path" not in params and "key" not in params:
                    requested = str(params.get("key_name", "stdout.log"))
                    if requested.startswith("stderr"):
                        bucket = default_stderr_bucket
                        key = default_stderr_key
                    else:
                        bucket = default_stdout_bucket
                        key = default_stdout_key
                    params["bucket"] = bucket
                    params["key"] = key
                    params.setdefault("file_name", requested)

            executor_actions.append(
                ExecutorOutputAction(
                    type=action_type,
                    params=params,
                    index=index,
                )
            )

        if not executor_actions:
            workflow.logger.info(
                "remote_ssh_test_only: no executor actions after "
                "translation for %s — nothing to publish",
                inp.issue_key,
            )
            return

        batch = ExecutionBatchInput(
            actions=executor_actions,
            issue_key=inp.issue_key,
            dept_id=inp.department_id,
            workflow_id=child_workflow_id,
        )

        try:
            await workflow.execute_activity(
                _ACT_EXECUTE_OUTPUT_ACTIONS,
                args=[batch],
                result_type=ExecutionBatchResult,
                start_to_close_timeout=_OUTPUT_ACTIONS_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort publish
            workflow.logger.warning(
                "execute_output_actions failed for %s — "
                "continuing with dispatched decision",
                inp.issue_key,
                exc_info=True,
            )

    @staticmethod
    def _extract_execution_stdout_uri(result: Any) -> str | None:
        """Read ``stdout_uri`` off an:class:`ExecutionRunWorkflowOutput`.

 Returns ``None`` when the child failed before upload (or when
 a stub test returns a plain dict without the field). The
 helper is intentionally permissive — the publish branch
 still fires with the deterministic:func:`execution_artifact_key` fallback when the URI is
 missing.
 """

        if result is None:
            return None
        if isinstance(result, ExecutionRunWorkflowOutput):
            return result.stdout_uri
        if isinstance(result, dict):
            value = result.get("stdout_uri")
            return value if isinstance(value, str) else None
        value = getattr(result, "stdout_uri", None)
        return value if isinstance(value, str) else None

    @staticmethod
    def _extract_execution_stderr_uri(result: Any) -> str | None:
        """Read ``stderr_uri`` off an:class:`ExecutionRunWorkflowOutput`.

 Mirrors:meth:`_extract_execution_stdout_uri` for the stderr
 artifact.
 """

        if result is None:
            return None
        if isinstance(result, ExecutionRunWorkflowOutput):
            return result.stderr_uri
        if isinstance(result, dict):
            value = result.get("stderr_uri")
            return value if isinstance(value, str) else None
        value = getattr(result, "stderr_uri", None)
        return value if isinstance(value, str) else None

    @staticmethod
    def _split_minio_uri(
        uri: str | None, *, fallback_key: str
    ) -> tuple[str, str]:
        """Split a ``s3://bucket/key`` URI into ``(bucket, key)``.

 Falls back to ``(_EXECUTION_DEFAULT_BUCKET, fallback_key)``
 when ``uri`` is ``None`` / empty / malformed. Pure helper —
 no Temporal primitives consulted, safe to unit-test directly.
 """

        if not uri:
            return (_EXECUTION_DEFAULT_BUCKET, fallback_key)
        raw = uri
        if raw.startswith("s3://"):
            raw = raw[len("s3://") :]
        raw = raw.strip().strip("/")
        if "/" not in raw:
            return (_EXECUTION_DEFAULT_BUCKET, fallback_key)
        bucket, key = raw.split("/", 1)
        bucket = bucket or _EXECUTION_DEFAULT_BUCKET
        key = key.lstrip("/") or fallback_key
        return (bucket, key)

    @staticmethod
    def _kind_to_action_type(kind: str) -> ActionType:
        """Map:data:`OutputActionKind` to:class:`ActionType`.

 The analyser-side vocabulary
 (:data:`temporal_shared.messages.OutputActionKind`) is wider
 than the executor enum because it carries the
 ``confluence_create_page`` / ``confluence_update_page``
 distinction that the executor recovers from
 ``params["page_id"]`` instead. Slack / email kinds have no
 executor mapping yet — they raise:class:`ValueError` so the
 publish branch can skip them.
 """

        mapping = {
            "jira_comment": ActionType.JIRA_COMMENT,
            "jira_attachment": ActionType.JIRA_ATTACHMENT,
            "bitbucket_commit": ActionType.BITBUCKET_COMMIT,
            "bitbucket_put_file": ActionType.BITBUCKET_COMMIT,
            "bitbucket_create_pr": ActionType.BITBUCKET_PR,
            "bitbucket_pr": ActionType.BITBUCKET_PR,
            "confluence_create_page": ActionType.CONFLUENCE_PAGE,
            "confluence_update_page": ActionType.CONFLUENCE_PAGE,
            "confluence_page": ActionType.CONFLUENCE_PAGE,
            "jira_transition": ActionType.JIRA_TRANSITION,
        }
        try:
            return mapping[kind]
        except KeyError as exc:
            raise ValueError(
                f"no executor mapping for output action kind={kind!r}"
            ) from exc

    @staticmethod
    def _payload_to_params(
        payload: tuple[tuple[str, object], ...],
    ) -> dict[str, Any]:
        """Coerce an:class:`OutputAction` payload back to a dict.:class:`temporal_shared.messages.OutputAction` stores its
 params as a tuple-of-pairs (immutable / hashable / replay-
 safe); the executor activity consumes a plain ``dict``. The
 helper rebuilds the dict and discards malformed pairs.
 """

        params: dict[str, Any] = {}
        for entry in payload or ():
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            key, value = entry
            if not isinstance(key, str) or not key:
                continue
            params[key] = value
        return params

    async def _evaluate_branch_pattern_rules(
        self, *, department_id: str, branch: str, workflow_type: str
    ) -> RouteDecision:
        """Load dept rules via activity, then run the pure router.

 The rule list lives in the department config (``departments.json``);
 loading it is a side effect (Postgres / file read) so it goes
 through an activity. The router itself
 (:func:`temporal_shared.branch_rules.route_by_branch_pattern`)
 is a pure function and runs inside the workflow body.

 On any error loading the rules we fall back to:data:`temporal_shared.branch_rules.DEFAULT_BRANCH_PATTERN_RULES`
 — that pinned default still enforces the configured
 ``hotfix/*`` and ``release/*`` constraints, which is
 the safer default than skipping the gate entirely.
 """

        rules: tuple[BranchPatternRule, ...]
        try:
            loaded: Any = await workflow.execute_activity(
                _ACT_LOAD_BRANCH_PATTERN_RULES,
                args=[department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001
            workflow.logger.warning(
                "load_branch_pattern_rules failed for dept %s — using "
                "DEFAULT_BRANCH_PATTERN_RULES",
                department_id,
            )
            rules = DEFAULT_BRANCH_PATTERN_RULES
        else:
            rules = self._coerce_rules(loaded)

        return route_by_branch_pattern(branch, workflow_type, rules)

    def _coerce_rules(
        self, loaded: Any
    ) -> tuple[BranchPatternRule, ...]:
        """Best-effort coercion of activity output to a rule tuple.

 The loader activity is allowed to return either a tuple/list
 of:class:`BranchPatternRule` instances or a sequence of
 plain dicts (Temporal's data converter happily decodes
 dataclasses, but tests may stub the activity with dicts).
 Anything that fails to coerce is logged and replaced with
 the pinned defaults so the gate stays safe.
 """

        if loaded is None:
            return DEFAULT_BRANCH_PATTERN_RULES
        if isinstance(loaded, BranchPatternRule):
            return (loaded,)

        coerced: list[BranchPatternRule] = []
        try:
            for item in loaded:
                if isinstance(item, BranchPatternRule):
                    coerced.append(item)
                    continue
                if isinstance(item, dict):
                    coerced.append(
                        BranchPatternRule(
                            glob=str(item.get("glob", "")),
                            denied_workflow_types=frozenset(
                                item.get("denied_workflow_types") or ()
                            ),
                            allowed_workflow_types=frozenset(
                                item.get("allowed_workflow_types") or ()
                            ),
                            reason=str(item.get("reason", "")) or "",
                        )
                    )
                    continue
                # Unknown item shape — bail out to defaults. We do
                # *not* partially honour the list because doing so
                # could turn a deny rule into a passthrough.
                workflow.logger.warning(
                    "unexpected branch_pattern_rules item shape; "
                    "using DEFAULT_BRANCH_PATTERN_RULES"
                )
                return DEFAULT_BRANCH_PATTERN_RULES
        except (TypeError, ValueError):
            workflow.logger.warning(
                "branch_pattern_rules coercion failed; using "
                "DEFAULT_BRANCH_PATTERN_RULES"
            )
            return DEFAULT_BRANCH_PATTERN_RULES
        return tuple(coerced)

    def _build_child_spec(
        self,
        *,
        parent_workflow_id: str,
        inp: AutomationWorkflowInput,
        analysis: LlmAnalysisResult,
    ) -> ChildWorkflowSpec:
        """Pick the child workflow + task queue for the LLM-selected type.

 Returns a:class:`ChildWorkflowSpec` describing the dispatch.
 The actual ``start_child_workflow`` call lives in:meth:`run`
 so this helper stays trivially testable.
 """

        wf_type = analysis.workflow_type
        if wf_type in _AGENT_RUNNER_WORKFLOW_TYPES:
            workflow_name = "AgentRunnerWorkflow"
        elif wf_type in _EXECUTION_RUN_WORKFLOW_TYPES:
            workflow_name = "ExecutionRunWorkflow"
        else:
            # required_capabilities already rejected unknown types
            # earlier; this is purely defensive.
            workflow_name = "AgentRunnerWorkflow"

        task_queue = task_queue_for(workflow_name)

        # Derive a deterministic child workflow_id. We avoid
        # ``workflow.uuid4`` here because the parent's id + a fixed
        # iteration counter is enough to make the child id stable
        # across replays — Temporal's id namespace is per-cluster, so
        # the suffix only needs to disambiguate retries within the
        # same parent.
        child_workflow_id = f"{workflow_name}-{parent_workflow_id}-iter-1"

        awaits_child = wf_type == "noop_test" or (
            wf_type in {"remote_ssh_test_only", "script_execute"}
            and bool(analysis.output_actions)
        )

        return ChildWorkflowSpec(
            workflow_name=workflow_name,
            workflow_id=child_workflow_id,
            task_queue=task_queue,
            # input_payload is unused at the dispatch site — we pass
            # the concrete child input dataclass directly via
            # ``args=[child_input]`` in:meth:`run`. Carrying an
            # empty tuple here keeps the child specification serialisable while
            # avoiding the indirection of a dict-of-pairs payload.
            input_payload=(),
            parent_close_policy="TERMINATE" if awaits_child else "ABANDON",
        )

    def _child_args(
        self,
        spec: ChildWorkflowSpec,
        inp: AutomationWorkflowInput,
        analysis: LlmAnalysisResult,
        *,
        override: Any | None = None,
    ) -> tuple[Any, ...]:
        """Return the positional args tuple for the child workflow.:class:`AgentRunnerWorkflow` consumes:class:`AgentRunnerWorkflowInput` and:class:`ExecutionRunWorkflow` consumes:class:`ExecutionRunWorkflowInput`. The two children are not
 statically imported here (their modules carry network-side
 machinery) — instead we construct the input dataclass
 defined in:mod:`temporal_shared.messages` and Temporal's
 data converter handles the wire shape.

 Parameters
 ----------
 override:
 Optional:class:`automation_worker.workflows.
 description_override.DescriptionOverride` carrying the
 per-task fields (``cleanup_policy``, ``timeout_seconds``,
 ``web_search``, ``target_repo``, ``target_branch``,
 ``output_actions``) the analyser distilled from the YAML
 front-matter / LLM result. Applied on top of the
 default child input shape (,,,,,). ``None`` means "no override" — the dept
 defaults already baked into ``analysis`` win.
 """

        # Resolve override-aware repo / branch up front so both child
        # branches see the same merge. The override values *replace*
        # whatever the analyser stored on:class:`LlmAnalysisResult`
        # because the analyser already merged dept defaults — so
        # ``override.target_repo == analysis.target_repo`` in the
        # common path; the only case they diverge is when the
        # override-merge layer needs to pin the
        # description-supplied repo / branch verbatim while the
        # analyser still has the dept default in place.
        target_repo = (
            override.target_repo
            if override is not None and override.target_repo
            else analysis.target_repo
        )
        target_branch = (
            override.target_branch
            if override is not None and override.target_branch
            else analysis.target_branch
        )

        if spec.workflow_name == "AgentRunnerWorkflow":
            # Lazy import inside the sandbox escape hatch already covered
            # the dataclass; build it inline.
            from temporal_shared.messages import AgentRunnerWorkflowInput

            child_input: Any = AgentRunnerWorkflowInput(
                parent_workflow_id=inp.issue_key,  # human-readable correlation
                issue_key=inp.issue_key,
                department_id=inp.department_id,
                workflow_type=analysis.workflow_type,
                analysis=analysis,
                target_repo=target_repo,
                target_branch=target_branch,
                iteration=inp.iteration,
                default_language=inp.default_language,
                trace_id=inp.trace_id,
            )
            return (child_input,)

        # ExecutionRunWorkflow path — build a minimal input. The
        # ``remote_ssh_test_only`` flow defers command resolution to
        # the child workflow so the gateway leaves
        # ``command`` empty. ``noop_test`` (—),
        # however, is a smoke test for newly-onboarded depts: the
        # gateway synthesises a deterministic
        # ``echo "noop_test ok: {issue_key}"`` so the full pipeline
        # (webhook → AutomationWorkflow → ExecutionRunWorkflow → SSH
        # runner → Jira comment) can be exercised without any LLM
        # involvement. The synthesised command is replay-safe — it
        # depends only on the issue key, which is part of the
        # workflow input and therefore stable across replays.
        if analysis.workflow_type == "noop_test":
            command = _NOOP_TEST_COMMAND_TEMPLATE.format(
                issue_key=inp.issue_key
            )
        else:
            command = (
                getattr(analysis, "execution_command", None)
                or (
                    override.execution_command
                    if override is not None
                    else None
                )
                or ""
            )

        # Apply the per-task ``timeout_seconds`` override. The
        # child input crosses a JSON payload converter, so carry seconds
        # as an integer and let ExecutionRunWorkflow coerce to timedelta.
        timeout_seconds = (
            override.timeout_seconds if override is not None else None
        )
        timeout = int(timeout_seconds) if timeout_seconds is not None else None

        # --------------------------------------------------------------
        # Populate ``workdir``, ``environment``, and
        # ``artifact_minio_prefix``. Previously these
        # were left at their defaults so the SSH command ran in the
        # runner's home directory (gereksinim #15 — "task başına klasör"
        # — was not satisfied) and stdout/stderr artifacts were uploaded
        # under a default prefix instead of a per-execution one.
        # --------------------------------------------------------------

        # 1. Canonical workspace path. ``build_workspace_path`` is pure
        # (no I/O) and the issue_key against a
        # path-traversal-safe pattern. Non-matching keys (e.g. an
        # ad-hoc test fixture without the ``PROJ-N`` shape) fall
        # back to ``None`` so the activity uses the runner default.
        # The base ``/var/ai-runner`` matches workspace_path.py's
        # documented default and the ``RUNNER_BASE_PATH`` env
        # convention shipped in execution-runner-worker/.env.example.
        workdir: str | None = None
        try:
            from runners.workspace_path import (  # noqa: PLC0415
                build_workspace_path as _build_workspace_path,
            )

            workdir = _build_workspace_path(
                "/var/ai-runner", inp.issue_key, inp.iteration
            )
        except Exception:  # noqa: BLE001 — fall back to runner default
            workdir = None

        # 2. Environment tuple carrying the cleanup policy so the child
        # workflow's ``apply_cleanup_policy`` activity sees the
        # user's preference (override.cleanup_policy, dept default,
        # or "on_success" floor).
        cleanup_policy = (
            override.cleanup_policy
            if override is not None and override.cleanup_policy
            else "on_success"
        )
        environment: tuple[tuple[str, str], ...] = (
            ("CLEANUP_POLICY", cleanup_policy),
        )

        # 3. Stable MinIO artifact prefix keyed by parent workflow id —
        # the executor uploads ``{prefix}/stdout.txt`` and
        # ``{prefix}/stderr.txt`` under this path so operators can
        # correlate runs back to the triggering issue.
        try:
            from temporal_shared.identifiers import (  # noqa: PLC0415
                execution_artifact_key as _execution_artifact_key,
            )

            artifact_minio_prefix: str | None = _execution_artifact_key(
                inp.issue_key, ""
            ).rstrip("/")
        except Exception:  # noqa: BLE001
            artifact_minio_prefix = f"executions/{inp.issue_key}"

        # Pass the analyser's ``needs_docker`` flag through to the
        # child so ExecutionRunWorkflow runs the
        # docker_* chain (build → run → collect_logs → cleanup) instead
        # of a single ssh_run_test invocation. The flag itself comes
        # from TaskAnalysisResult via the to_llm_analysis_result bridge.
        needs_docker = bool(getattr(analysis, "needs_docker", False))

        # Derive a deterministic image tag from the issue context. The
        # child workflow can override via ``docker_image_tag`` field
        # when callers need a custom tag.
        docker_image_tag = None
        if needs_docker:
            docker_image_tag = (
                f"ai-bot-{inp.issue_key.lower()}"
                f"-iter-{inp.iteration}:latest"
            )

        exec_input = ExecutionRunWorkflowInput(
            parent_workflow_id=inp.issue_key,
            runner_id=None,
            command=command,
            workdir=workdir,
            environment=environment,
            artifact_minio_prefix=artifact_minio_prefix,
            department_id=inp.department_id,
            workflow_type=analysis.workflow_type,
            start_to_close_timeout=timeout,
            trace_id=inp.trace_id,
            needs_docker=needs_docker,
            docker_image_tag=docker_image_tag,
            docker_dockerfile_path="Dockerfile" if needs_docker else None,
        )
        return (exec_input,)

    async def _stop_with_audit(
        self,
        *,
        inp: AutomationWorkflowInput,
        decision: str,
        workflow_type: str | None,
        failure_reason: str,
        summary: str,
        jira_message: str,
        missing_capabilities: tuple[str, ...] = (),
    ) -> _AutomationStop:
        """Emit audit + Jira comment, then return an early-exit envelope.

 Best-effort semantics for both side effects: a failed audit
 write does not silence the Jira comment, and a failed Jira
 comment does not silence the audit. The workflow always
 returns the structured ``decision`` regardless.
 """

        # Audit first so the operator has an authoritative record even
        # if Jira is unavailable. ``audit_write`` is a generic activity
        # name — the activity is responsible for the
        # ``actor_role`` / ``action`` invariants.
        try:
            await workflow.execute_activity(
                _ACT_AUDIT_WRITE,
                args=[
                    {
                        "action": f"automation_{decision}",
                        "actor_role": "system",
                        "department_id": inp.department_id,
                        "issue_key": inp.issue_key,
                        "workflow_type": workflow_type,
                        "failure_reason": failure_reason,
                        "summary": summary,
                        "missing_capabilities": list(missing_capabilities),
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 — audit best-effort
            workflow.logger.warning(
                "audit_write failed for decision=%s reason=%s",
                decision,
                failure_reason,
            )

        await self._best_effort_jira_comment(
            inp.issue_key, jira_message, inp.department_id
        )

        return _AutomationStop(
            decision=decision,
            workflow_type=workflow_type,
            summary=summary,
            failure_reason=failure_reason,
            missing_capabilities=missing_capabilities,
        )

    @staticmethod
    def _stop_to_output(stop: _AutomationStop) -> AutomationWorkflowOutput:
        """Lift an:class:`_AutomationStop` into the public output shape."""

        # The ``decision`` literal is constrained by
        #:data:`temporal_shared.messages.AutomationDecision` — the
        # caller is responsible for passing one of
        # ``"dispatched" | "denied" | "out_of_scope" | "failed"``.
        return AutomationWorkflowOutput(
            decision=stop.decision,  # type: ignore[arg-type]
            workflow_type=stop.workflow_type,
            child_workflow_id=None,
            summary=stop.summary,
            failure_reason=stop.failure_reason,
            missing_capabilities=stop.missing_capabilities,
        )

    # ------------------------------------------------------------------
    # Notification dispatch
    # ------------------------------------------------------------------

    async def _dispatch_notification(
        self,
        *,
        inp: AutomationWorkflowInput,
        status: str,
        summary: str,
        error: str | None,
    ) -> None:
        """Best-effort workflow-completion notification dispatch.

 Forwards a:class:`DispatchNotificationInput` to the
 ``dispatch_notification`` activity at every terminal return
 of:meth:`run`. The activity body itself is best-effort
 (transport failures are logged + swallowed inside the
 activity); the workflow wraps the whole call in another
 ``try / except`` so even a ``WorkflowFailureError`` (eg.
 activity registration drift) cannot block the workflow's
 terminal return.

 Decision table:

 * ``status == "failed"`` ⇒ Slack send is **mandatory**
 regardless of ``inp.notify_on_success``.
 * ``status ∈ {"completed", "partial"}`` ⇒ success-gated; the
 dispatcher short-circuits to a no-op when
 ``notify_on_success == False``.
 """

        try:
            await workflow.execute_activity(
                _ACT_DISPATCH_NOTIFICATION,
                args=[
                    DispatchNotificationInput(
                        workflow_id=workflow.info().workflow_id,
                        dept_id=inp.department_id,
                        notify_on_success=inp.notify_on_success,
                        notify_channels=tuple(inp.notify_channels),
                        slack_webhook=inp.slack_webhook,
                        notify_email=inp.notify_email,
                        status=status,
                        summary=summary,
                        error=error,
                        jira_issue_url=None,
                    )
                ],
                start_to_close_timeout=_NOTIFICATION_DISPATCH_TIMEOUT,
                retry_policy=_NOTIFICATION_DISPATCH_RETRY,
            )
        except Exception:  # noqa: BLE001 — best-effort
            workflow.logger.warning(
                "dispatch_notification failed (best-effort)",
                exc_info=True,
            )

    async def _finalize_stop(
        self,
        inp: AutomationWorkflowInput,
        stop: _AutomationStop,
    ) -> AutomationWorkflowOutput:
        """Dispatch a notification for an early-exit stop, then return.

 Maps the gateway's internal:class:`_AutomationStop`
 ``decision`` literal to the:class:`notification.WorkflowResult` ``status`` literal.
 Every non-``"dispatched"`` terminal decision is treated as a
 ``"failed"`` for notification purposes — ``denied`` /
 ``out_of_scope`` are user-visible rejections that the
 operator wants the mandatory Slack alarm to fire on,
 and ``failed`` naturally maps to the same branch.

 The ``"dispatched"`` decision is handled in the success
 branch of:meth:`run` directly (``status="completed"``), not
 through this helper.
 """

        await self._dispatch_notification(
            inp=inp,
            status="failed",
            summary=stop.summary,
            error=stop.failure_reason,
        )
        return self._stop_to_output(stop)


__all__: tuple[str, ...] = ("AutomationWorkflow",)
