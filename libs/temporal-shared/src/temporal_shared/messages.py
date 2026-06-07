"""Shared workflow / activity message dataclasses (single I/O payload pattern).

This module is the **single source of truth** for the input and output
dataclasses exchanged across the three Temporal workflows
(``AutomationWorkflow``, ``AgentRunnerWorkflow``,
``ExecutionRunWorkflow``) and the supporting value objects shared with
activities (``WebhookEvent``, ``OutputAction``, ``LlmAnalysisResult``,
``IterationState``, ``ChildWorkflowSpec``, ``CompensationContext``).

Wire contract:

* Every workflow ``run()`` accepts **exactly one** input dataclass and
  returns **exactly one** output dataclass.  Activities follow the same
  pattern - one frozen dataclass in, one frozen dataclass out - so that
  the Temporal data converter has a deterministic, schema-stable wire
  shape for each call boundary.
* Every dataclass in this module is declared with ``frozen=True`` and
  ``slots=True``.  Mutable collections (``list``/``dict``/``set``) are
  forbidden in field types; we use ``tuple`` and ``frozenset`` instead so
  the values are hashable, immutable, and trivially replayable through
  Temporal's history.  State-like dataclasses (e.g. :class:`IterationState`)
  evolve by being replaced wholesale via :func:`dataclasses.replace`, not
  by in-place mutation.

The dataclasses here intentionally do **not** import ``temporalio`` -
they are plain Python data containers so the same module can be re-used
by activities, services, and tests without pulling in the workflow
runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal, Mapping

__all__ = [
    # workflow envelopes
    "AutomationWorkflowInput",
    "AutomationWorkflowOutput",
    "AgentRunnerWorkflowInput",
    "AgentRunnerWorkflowOutput",
    "ExecutionRunWorkflowInput",
    "ExecutionRunWorkflowOutput",
    # supporting value objects
    "WebhookEvent",
    "OutputAction",
    "LlmAnalysisResult",
    "IterationState",
    "ExplainCacheEntry",
    "ChildWorkflowSpec",
    "CompensationContext",
    # output-action classification
    "CRITICAL_OUTPUT_ACTION_KINDS",
    "BEST_EFFORT_OUTPUT_ACTION_KINDS",
    # type aliases (Literal narrowings)
    "Provider",
    "OutputActionKind",
    "OutputActionSeverity",
    "CompensationReason",
    "AutomationDecision",
    "AgentRunnerStatus",
    "ExecutionRunStatus",
]


# ---------------------------------------------------------------------------
# Type aliases (Literal narrowings)
# ---------------------------------------------------------------------------

#: Webhook provider identifier - Atlassian product family.
Provider = Literal["jira", "bitbucket"]

#: Closed vocabulary of side-effect actions an activity may emit.
OutputActionKind = Literal[
    "jira_comment",
    "jira_attachment",
    "bitbucket_commit",
    "bitbucket_create_pr",
    "confluence_create_page",
    "confluence_update_page",
    "jira_transition",
    "slack_notify",
    "email_notify",
]

#: Output-action severity - drives partial-failure semantics:
#: ``critical`` failures fail the workflow and trigger compensation;
#: ``best_effort`` failures are logged and reported but do not abort.
OutputActionSeverity = Literal["critical", "best_effort"]

#: Cancel-request origin attached to a :class:`CompensationContext`.
#: Distinguishes ``workflow_cancelled_by_end_user`` from
#: ``workflow_cancelled_by_admin`` in the audit log.
CompensationReason = Literal["user_cancel", "admin_cancel"]

#: Outcome of the :class:`AutomationWorkflow` gateway decision.
#: ``"dispatched"`` - child :class:`AgentRunnerWorkflow` started.
#: ``"denied"`` - capability gate refused.
#: ``"out_of_scope"`` - branch-pattern rules or other policy refused
#: by policy.
#: ``"failed"`` - task-analysis or pre-dispatch step errored.
AutomationDecision = Literal["dispatched", "denied", "out_of_scope", "failed"]

#: Terminal status of an :class:`AgentRunnerWorkflow` execution.
AgentRunnerStatus = Literal[
    "completed",
    "completed_with_partial_failure",
    "needs_info",
    "out_of_scope",
    "cancelled",
    "failed",
]

#: Terminal status of an :class:`ExecutionRunWorkflow` execution
ExecutionRunStatus = Literal["passed", "failed", "timeout"]


# ---------------------------------------------------------------------------
# Output-action classification table - single source of truth
# ---------------------------------------------------------------------------

#: ``OutputActionKind`` values that abort the workflow on failure.
CRITICAL_OUTPUT_ACTION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "jira_comment",
        "bitbucket_commit",
        "bitbucket_create_pr",
        "confluence_create_page",
        "confluence_update_page",
        "jira_transition",
    }
)

#: ``OutputActionKind`` values that are reported but never abort the
#: workflow on failure.
BEST_EFFORT_OUTPUT_ACTION_KINDS: Final[frozenset[str]] = frozenset(
    {
        "slack_notify",
        "email_notify",
        "jira_attachment",
    }
)


# ---------------------------------------------------------------------------
# Supporting value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """Provider-normalised webhook event.

    The webhook gateway lifts both the Jira (``webhookEvent`` body field)
    and Bitbucket (``X-Event-Key`` header) dialects into this single
    shape before running the filter chain - so loop-guard, mention
    filter, replay-dedup, and burst-debounce can be expressed as pure
    decisions on a uniform value object.

    Attributes
    ----------
    provider:
        ``"jira"`` or ``"bitbucket"``.
    event_type:
        Normalised event name (e.g. ``"issue_created"``,
        ``"issue_commented"``, ``"pr_created"``,
        ``"pr_commented"``, ``"pr_updated"``).
    delivery_id:
        Provider-supplied delivery identifier - the natural idempotency
        key written to ``processed_events``.
    actor_account_id:
        Atlassian account_id of the actor who emitted the event;
        ``None`` if the payload omits it and the regex-fallback loop
        guard applies.
    body_text:
        Comment body or PR title - used for the ``[bot:`` regex
        loop-guard fallback and the ``[bot:hear]`` / ``[fix]`` /
        ``[explain]`` keyword detection.
    project_key:
        Jira project key when applicable, else ``None``.
    repo_slug:
        Bitbucket repo slug when applicable, else ``None``.
    issue_key:
        Jira issue key when the event references an issue, else
        ``None``.
    pr_id:
        Bitbucket PR id when the event references a PR, else ``None``.
    raw_payload:
        Snapshot of the original request body as a tuple of
        ``(key, value)`` pairs preserving insertion order.  We use a
        tuple-of-pairs (rather than a ``dict``) because frozen
        dataclasses with mutable defaults are awkward and because the
        payload is opaque to the filter chain - only HMAC verification
        consumes it, and HMAC operates on the raw bytes anyway.
    """

    provider: Provider
    event_type: str
    delivery_id: str
    actor_account_id: str | None
    body_text: str | None
    project_key: str | None
    repo_slug: str | None
    issue_key: str | None
    pr_id: int | None
    raw_payload: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class OutputAction:
    """A single side-effect requested by an activity.

    Activities never apply side effects directly - they emit a tuple of
    ``OutputAction`` values which the parent workflow then partitions
    by :data:`CRITICAL_OUTPUT_ACTION_KINDS` /
    :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS` and applies through
    a dedicated apply activity.

    Attributes
    ----------
    kind:
        Action kind drawn from :data:`OutputActionKind`.
    severity:
        ``"critical"`` or ``"best_effort"`` - must agree with the kind's
        membership in :data:`CRITICAL_OUTPUT_ACTION_KINDS` /
        :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS`.  Carrying severity
        explicitly (rather than recomputing it from the kind) lets the
        wire schema survive future migrations and lets test fixtures
        construct deliberate violations for partial-failure tests.
    payload:
        Action-specific arguments as a tuple of ``(key, value)`` pairs.
        We use a tuple-of-pairs rather than a ``dict`` so the dataclass
        remains immutable and hashable.  The keys / value shapes are
        documented per kind by the output-action contract.
    """

    kind: OutputActionKind
    severity: OutputActionSeverity
    payload: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class LlmAnalysisResult:
    """Output of the ``llm_analyze_task`` activity.

    The LLM consumes the Jira issue + dept context and returns a
    structured proposal for which workflow type to run, on which repo /
    branch, and what side effects to emit.  The shape below is the
    decoded form of the JSON the LLM returns, after the task-analysis
    parser has validated the workflow-type-specific
    required fields.

    Attributes
    ----------
    workflow_type:
        Selected workflow type - one of the keys of
        :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`.
    confidence:
        ``"high"``, ``"medium"``, or ``"low"`` - drives the
        ``needs_info`` / structured-choice branch.
    target_repo:
        Bitbucket repo slug when applicable; ``None`` for non-code
        workflow types.
    target_branch:
        Branch name when applicable; ``None`` for non-code workflow
        types.
    target_space:
        Confluence space key when applicable; ``None`` otherwise.
    target_page_id:
        Confluence page id for ``confluence_doc_update``; ``None``
        otherwise.
    title:
        Human-readable summary of the proposed work.
    rationale:
        LLM-supplied reasoning - surfaced in the audit log and Jira
        comment.
    output_actions:
        Tuple of :class:`OutputAction` proposed by the LLM.
    needs_info_questions:
        Tuple of clarification questions to surface as a Jira comment
        when ``confidence == "low"``; empty otherwise.
    token_usage:
        Recorded token usage for cost accounting.
    """

    workflow_type: str
    confidence: Literal["high", "medium", "low"]
    target_repo: str | None = None
    target_branch: str | None = None
    target_space: str | None = None
    target_page_id: str | None = None
    title: str = ""
    rationale: str = ""
    output_actions: tuple[OutputAction, ...] = ()
    needs_info_questions: tuple[str, ...] = ()
    token_usage: int = 0
    # Carry the analyser's ``needs_docker`` decision through the bridge
    # so :meth:`AutomationWorkflow._child_args` can wire it into
    # :attr:`ExecutionRunWorkflowInput.needs_docker`.
    needs_docker: bool = False
    needs_ssh: bool = False
    execution_command: str | None = None


@dataclass(frozen=True, slots=True)
class ExplainCacheEntry:
    """Single entry of :attr:`IterationState.explain_cache`.

    Attributes
    ----------
    answer:
        Cached explanation text.
    issued_at:
        ``workflow.now()`` when the entry was written; the 5-minute
        TTL is computed against this value.
    """

    answer: str
    issued_at: datetime


# Sentinel empty mappings so the ``frozen=True`` ``IterationState`` can
# default to immutable, shared instances without paying for a fresh dict
# allocation per workflow start.
_EMPTY_TEST_RESULTS: Final[Mapping[str, ExecutionRunStatus]] = {}
_EMPTY_EXPLAIN_CACHE: Final[Mapping[str, ExplainCacheEntry]] = {}


@dataclass(frozen=True, slots=True)
class IterationState:
    """Replay-safe iteration state carried by :class:`AgentRunnerWorkflow`.

    This module owns only the **shape**; the pure decision functions
    (``should_advance_iter``, ``is_fix_debounced``, ...) live in
    :mod:`temporal_shared.iteration`.

    The dataclass is frozen and stores only immutable collections -
    callers evolve state by constructing a new value via
    :func:`dataclasses.replace`, mirroring the functional update style
    that keeps Temporal replay deterministic.

    Attributes
    ----------
    iter_count:
        Number of completed iterations; the workflow checks
        ``iter_count >= max_iter`` before advancing.
    last_fix_trigger_at:
        ``workflow.now()`` of the most recent ``[fix]`` keyword
        acceptance, or ``None`` if no ``[fix]`` has fired yet.  Used
        for the 60-second debounce window.
    test_results_by_diff_hash:
        Mapping of diff hash  ``ExecutionRunWorkflowOutput.status``
        from prior test runs.  ``[fix]`` re-test protection
        consults this map: identical diff  skip re-execution.
    explain_cache:
        Mapping of PR-diff hash  cached ``[explain]`` answer with
        an issued-at timestamp.  Used for the 5-minute TTL
        cooldown + cache.
    needs_info_streak:
        Count of consecutive ``needs_info`` comments emitted by the
        bot; the loop cap terminates the workflow once
        this reaches 3.
    """

    iter_count: int = 0
    last_fix_trigger_at: datetime | None = None
    test_results_by_diff_hash: Mapping[str, ExecutionRunStatus] = field(
        default_factory=lambda: _EMPTY_TEST_RESULTS
    )
    explain_cache: Mapping[str, ExplainCacheEntry] = field(
        default_factory=lambda: _EMPTY_EXPLAIN_CACHE
    )
    needs_info_streak: int = 0


@dataclass(frozen=True, slots=True)
class ChildWorkflowSpec:
    """Specification of a child workflow to start for ``multi_step``.

    The :class:`AutomationWorkflow` (and ``multi_step`` orchestrators)
    construct a tuple of these to describe the children to dispatch.
    Carrying the spec as data - rather than imperatively calling
    ``start_child_workflow`` from a helper - keeps the dispatch step
    introspectable from Temporal's event history and keeps the
    decision logic unit-testable.

    Attributes
    ----------
    workflow_name:
        Registered workflow name, e.g. ``"AgentRunnerWorkflow"`` or
        ``"ExecutionRunWorkflow"``.  Looked up against
        :data:`temporal_shared.workflow_registry.WORKFLOW_TASK_QUEUES`
        for routing.
    workflow_id:
        Caller-supplied idempotency key.  Built via
        :func:`temporal_shared.identifiers.agent_workflow_id` /
        :func:`execution_workflow_id`.
    task_queue:
        Task queue for the child - must match the worker's poll target.
    input_payload:
        Tuple of ``(key, value)`` pairs encoding the child's single
        input dataclass.  Tuple-of-pairs (rather than the dataclass
        instance itself) keeps :class:`ChildWorkflowSpec` decoupled
        from the concrete child input class and avoids circular type
        dependencies at the package boundary.
    parent_close_policy:
        Temporal ``ParentClosePolicy`` name - ``"TERMINATE"``,
        ``"ABANDON"``, or ``"REQUEST_CANCEL"``.  Defaults to
        ``"TERMINATE"`` so abandoned-parent runs do not leak orphaned
        children.
    execution_timeout:
        Optional hard cap on the child's wall-clock runtime.  ``None``
        defers to the worker default.
    """

    workflow_name: str
    workflow_id: str
    task_queue: str
    input_payload: tuple[tuple[str, Any], ...] = ()
    parent_close_policy: Literal[
        "TERMINATE", "ABANDON", "REQUEST_CANCEL"
    ] = "TERMINATE"
    execution_timeout: timedelta | None = None


@dataclass(frozen=True, slots=True)
class CompensationContext:
    """Input passed to the compensation chain on workflow cancel.

    The chain is a fixed sequence of idempotent activities driven by
    this context;
    every step receives the same context so each can decide which side
    effect (if any) to undo without consulting external state.

    Attributes
    ----------
    workflow_id:
        Cancelled workflow id - used for MinIO prefix lookup and
        audit correlation.
    dept_id:
        Department slug - needed for credential resolution inside the
        compensation activities.
    issue_key:
        Jira issue key when the workflow was issue-driven; ``None``
        for branch-only or PR-only flows.
    pr_id:
        Bitbucket PR id for draft-PR closure; ``None`` if no PR was
        opened.
    branch:
        ``ai/{issue_key}/iter-{n}`` branch to delete (when no other PR
        references it); ``None`` if no branch was created.
    confluence_page_id:
        Page id for ``cancelled`` label + ``[CANCELLED]`` title prefix
        application; ``None`` if no Confluence write
        happened.
    minio_prefix:
        ``ai-runs/{workflow_id}/`` prefix to leave under retention
        under retention; ``None`` if no artifacts were written.
    reason:
        Cancel origin (:data:`CompensationReason`) - ``user_cancel``
        or ``admin_cancel``; surfaces in the audit event name.
    actor_id:
        OIDC subject of the cancelling user.
    """

    workflow_id: str
    dept_id: str
    issue_key: str | None
    pr_id: int | None
    branch: str | None
    confluence_page_id: str | None
    minio_prefix: str | None
    reason: CompensationReason
    actor_id: str


# ---------------------------------------------------------------------------
# AutomationWorkflow envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutomationWorkflowInput:
    """Single input of :class:`AutomationWorkflow.run`.

    Constructed by the webhook gateway (``automation-service``) after
    the filter chain accepts the event and ``signalWithStart`` is about
    to fire.  All values are derived from the normalised
    :class:`WebhookEvent` plus department configuration loaded server
    side - the workflow itself never reads from the database directly.

    Attributes
    ----------
    issue_key:
        Jira issue key associated with the trigger.  For
        Bitbucket-PR-triggered runs this is the linked Jira key (or
        ``"BB-{pr_id}"`` synthesised by the gateway when no link
        exists).
    department_id:
        Department slug for credential resolution and routing.
    available_capabilities:
        Tuple of capability strings the department holds (simple
        vocabulary - ``"jira"``, ``"bitbucket"``, ``"confluence"``,
        ``"execution"``, ``"web_search"``).  Tuple (not frozenset) for
        Temporal serialisation; the workflow body lifts it into a
        frozenset before consulting :func:`gate`.
    available_repos:
        Tuple of repository slugs the dept may target.
    available_spaces:
        Tuple of Confluence space keys the dept may target.
    default_language:
        ISO-639-1 code for LLM output and Jira-comment formatting.
    trigger_event:
        Normalised webhook event type that started this workflow
        (e.g. ``"jira:issue_assigned"`` or ``"pullrequest:created"``).
    iteration:
        Initial iteration counter - always ``1`` for a fresh start;
        higher values are reserved for future iter-N re-entry.
    raw_event:
        Snapshot of the original :class:`WebhookEvent` for audit
        correlation; the workflow does not consume the raw payload.
    trace_id:
        End-to-end correlation identifier set by
        :class:`observability.TraceMiddleware` in the webhook gateway
        (``automation-service``) on the inbound HTTP request that
        started the workflow.  Carried verbatim through every child
        workflow input and activity input so the same trace_id appears
        in:

        * the webhook log (set by the middleware)
        * the workflow start audit row
        * every activity log line (via
          :func:`observability.set_trace_id` invoked at activity
          entry)
        * every MCP request issued by an activity
          (``http_shared.make_mcp_client`` reads
          :func:`observability.get_trace_id` on each outbound
          request and stamps the ``X-Trace-Id`` header)

        The workflow input carries ``trace_id`` so propagation stays
        complete across the workflow and activity boundary.  Defaults
        to the empty string for backwards compatibility with older
        callers that have not yet been updated.
    notify_on_success:
        Department-level success-gating flag.  When ``True``,
        the gateway dispatches a workflow-completion notification
        (Slack / email) on ``"completed"`` and ``"partial"`` runs.
        ``False`` (default) makes the success-path dispatch a no-op
        - failure-path dispatch still fires regardless.
    notify_channels:
        Tuple of notification channels the department subscribed to
        (``"slack"`` / ``"email"`` / ``"teams"``).  Empty tuple
        (default) skips success-path dispatch.  Failure-path
        dispatch always includes Slack regardless of this set
        on failures.
    slack_webhook:
        Resolved Slack webhook URL (already de-referenced from any
        ``vault:`` ref by the webhook gateway).  ``None`` means the
        department has no Slack channel configured.
    notify_email:
        Resolved RFC-5322 email address.  ``None`` means email is
        not configured.
    """

    issue_key: str
    department_id: str
    available_capabilities: tuple[str, ...] = ()
    available_repos: tuple[str, ...] = ()
    available_spaces: tuple[str, ...] = ()
    default_language: str = "tr"
    trigger_event: str = "jira:issue_assigned"
    iteration: int = 1
    raw_event: WebhookEvent | None = None
    trace_id: str = ""
    # ------------------------------------------------------------------
    # Notification dispatch block.
    # Carried on the workflow input so the gateway can forward a
    # workflow-completion notification (Slack / email) at every
    # terminal return without re-loading the dept config from
    # Postgres.  All four fields default to "no notification" so
    # legacy callers that have not yet been updated keep the
    # dispatch-as-noop behaviour (failure path still falls back on
    # the audit-prune admin alarm wired in :mod:`audit_prune`).
    # ------------------------------------------------------------------
    notify_on_success: bool = False
    notify_channels: tuple[str, ...] = ()
    slack_webhook: str | None = None
    notify_email: str | None = None


@dataclass(frozen=True, slots=True)
class AutomationWorkflowOutput:
    """Single result of :class:`AutomationWorkflow.run`.

    Attributes
    ----------
    decision:
        Gateway outcome (:data:`AutomationDecision`).  ``"dispatched"``
        means a child :class:`AgentRunnerWorkflow` was started;
        ``"denied"`` means the capability gate refused; ``"out_of_scope"``
        means branch-pattern rules or other policy refused;
        ``"failed"`` means a pre-dispatch step errored.
    workflow_type:
        LLM-selected workflow type when analysis succeeded; ``None``
        when ``decision != "dispatched"`` or analysis failed.
    child_workflow_id:
        Temporal id of the dispatched child, or ``None`` when no
        child ran.
    summary:
        Short Turkish summary mirrored into the final Jira comment.
    failure_reason:
        Stable failure category for non-``"dispatched"`` decisions:
        ``"missing_capability"``, ``"loop_cap_reached"``,
        ``"needs_info_timeout"``, ``"task_analysis_failed"``,
        ``"branch_rule_denied"``, ``"child_failed"``, or ``None``.
    missing_capabilities:
        Tuple of missing capability names - populated when
        ``failure_reason == "missing_capability"``; empty
        otherwise.
    """

    decision: AutomationDecision
    workflow_type: str | None = None
    child_workflow_id: str | None = None
    summary: str = ""
    failure_reason: str | None = None
    missing_capabilities: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# AgentRunnerWorkflow envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentRunnerWorkflowInput:
    """Single input of :class:`AgentRunnerWorkflow.run`.

    Constructed by :class:`AutomationWorkflow` when dispatching the
    child.  Carries the LLM analysis result plus everything the agent
    needs to operate without re-loading department config.

    Attributes
    ----------
    parent_workflow_id:
        Temporal id of the parent :class:`AutomationWorkflow` - used
        for audit correlation and child workflow id construction.
    issue_key:
        Jira issue key.
    department_id:
        Department slug.
    workflow_type:
        Selected workflow type - must be a valid key of
        :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`.
    analysis:
        Full :class:`LlmAnalysisResult` so the child can reference
        ``rationale`` and ``token_usage`` for audit purposes.
    target_repo:
        Bitbucket repo slug; mirrors :attr:`LlmAnalysisResult.target_repo`
        but lifted to the envelope for ergonomics.
    target_branch:
        Branch name; mirrors :attr:`LlmAnalysisResult.target_branch`.
    iteration:
        Iteration counter (1 for the initial run; ≥2 for re-entry via
        ``[fix]`` or new comments).  Bounded above by ``max_iter``.
    max_iter:
        Hard cap - defaults to 5.
    default_language:
        ISO-639-1 code passed through from the parent.
    trace_id:
        End-to-end correlation identifier inherited from
        :class:`AutomationWorkflowInput.trace_id`.  Mirrored verbatim
        from the parent workflow into this child input so the
        AgentRunner's activity log lines and outbound MCP requests
        carry the same trace_id as the originating webhook.
    """

    parent_workflow_id: str
    issue_key: str
    department_id: str
    workflow_type: str
    analysis: LlmAnalysisResult
    target_repo: str | None = None
    target_branch: str | None = None
    iteration: int = 1
    max_iter: int = 5
    default_language: str = "tr"
    trace_id: str = ""
    # Department routing envelope mirrored from the parent
    # ``AutomationWorkflowInput`` so a multi_step fan-out can re-enter
    # each Epic subtask through the gateway with the same capability /
    # repo / space context. Empty tuples for non-Epic flows keep the
    # wire shape backward-compatible.
    available_capabilities: tuple[str, ...] = ()
    available_repos: tuple[str, ...] = ()
    available_spaces: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentRunnerWorkflowOutput:
    """Single result of :class:`AgentRunnerWorkflow.run`.

    Attributes
    ----------
    status:
        Terminal status (:data:`AgentRunnerStatus`).  ``"completed"``
        means every output action succeeded;
        ``"completed_with_partial_failure"`` means at least one
        ``best_effort`` action failed but no critical action did
        ; ``"out_of_scope"`` means MAX_ITER or
        ``needs_info_streak`` cap reached; ``"cancelled"``
        means a cancel signal triggered the compensation chain
        ; ``"failed"`` means a critical action or unhandled
        exception aborted the run; ``"needs_info"`` means the workflow
        exited waiting for a user reply.
    iter_count:
        Final iteration counter from :class:`IterationState`.
    pr_id:
        Bitbucket draft PR id when one was opened, otherwise ``None``.
    branch:
        Branch name written when applicable; ``None`` otherwise.
    confluence_page_id:
        Confluence page id created or updated when applicable;
        ``None`` otherwise.
    summary:
        Short Turkish summary mirrored into the final Jira comment.
    partial_failure_actions:
        Tuple of best-effort action kinds that failed during the run
        ; empty when every action succeeded.
    failure_reason:
        Stable failure category when ``status == "failed"``; ``None``
        otherwise.
    """

    status: AgentRunnerStatus
    iter_count: int = 0
    pr_id: int | None = None
    branch: str | None = None
    confluence_page_id: str | None = None
    summary: str = ""
    partial_failure_actions: tuple[str, ...] = ()
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# ExecutionRunWorkflow envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionRunWorkflowInput:
    """Single input of :class:`ExecutionRunWorkflow.run`.

    Constructed by :class:`AgentRunnerWorkflow` when the
    ``code_change_with_test`` path needs SSH/Docker test execution, or
    by the ``noop_test`` smoke path.

    Attributes
    ----------
    parent_workflow_id:
        Temporal id of the parent :class:`AgentRunnerWorkflow` (or
        :class:`AutomationWorkflow` for ``noop_test``).
    runner_id:
        SSH runner identifier - looked up against the dept's
        ``ssh_runners`` config.  ``None`` when the workflow runs against
        the default runner.
    command:
        Command to execute on the runner.  Treated as opaque text - the
        runner side handles shell quoting.
    workdir:
        Working directory on the runner; ``None`` defers to the runner
        default.
    environment:
        Tuple of ``(key, value)`` pairs for environment variables
        injected into the command.  Tuple-of-pairs (rather than dict)
        for immutability.
    artifact_minio_prefix:
        MinIO prefix for stdout / stderr / coverage uploads.  Built via
        :func:`temporal_shared.identifiers.execution_artifact_key`.
    start_to_close_timeout:
        Per-attempt timeout passed to the underlying activity. May be a
        ``timedelta`` in in-process tests or numeric seconds after JSON
        conversion between workflows. ``None`` defers to the worker
        default.
    heartbeat_timeout:
        Heartbeat interval the runner activity must beat within. May be
        a ``timedelta`` in in-process tests or numeric seconds after JSON
        conversion between workflows. ``None`` defers to the worker
        default.
    department_id:
        Department slug - used for audit and credential scoping.
    workflow_type:
        Logical workflow type that triggered this execution run.  At
        present only ``"noop_test"`` is meaningful.  When set,
        :class:`ExecutionRunWorkflow` applies a
        workflow-type-specific safety net: an empty :attr:`command`
        is replaced with the smoke-test default ``echo "ok"`` and an
        unset :attr:`start_to_close_timeout` is tightened to a value
        appropriate for a smoke run (``noop_test`` should never run
        long).  ``None`` (the default) preserves the legacy
        contract: the workflow consumes :attr:`command` and
        :attr:`start_to_close_timeout` verbatim.  The field is
        intentionally typed as ``str | None`` rather than the
        ``Literal`` of all 10 workflow types because the
        :class:`ExecutionRunWorkflow` body only reads the literal
        ``"noop_test"`` value - every other dispatch path supplies
        a non-empty :attr:`command` and never consults this field.
    """

    parent_workflow_id: str
    runner_id: str | None
    command: str
    workdir: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    artifact_minio_prefix: str | None = None
    start_to_close_timeout: timedelta | int | float | None = None
    heartbeat_timeout: timedelta | int | float | None = None
    department_id: str = ""
    workflow_type: str | None = None
    #: End-to-end correlation identifier inherited from the parent
    #: :class:`AutomationWorkflowInput` / :class:`AgentRunnerWorkflowInput`
    #: Carried into
    #: the runner activity so the captured stdout / exit-code log
    #: lines and the MinIO artifact path can be cross-referenced
    #: against the originating webhook's trace_id.
    trace_id: str = ""
    #: Department disk-quota cap (in megabytes) for the workspace base
    #: path referenced by :attr:`workdir`.  ``None`` (the default)
    #: disables the quota gate entirely - preserving the legacy
    #: contract for every existing call site.  When set, the
    #: :class:`ExecutionRunWorkflow` invokes the ``check_disk_quota``
    #: activity before ``ssh_run_test`` and fails fast with
    #: ``ApplicationError(type="DiskQuotaExceeded", non_retryable=True)``
    #: if the runner is already at or above the cap.  Sourced from
    #: ``departments.json::ssh_workspace_quota_mb`` by the dispatch
    #: layer.
    workspace_quota_mb: float | None = None
    #: When ``True`` the workflow runs the Docker chain -
    #: ``docker_daemon_healthcheck``
    #: ``docker_build_image``  ``docker_run_container``
    #: ``docker_collect_logs``  ``docker_cleanup_container`` - instead
    #: of the single ``ssh_run_test`` activity. Sourced from the
    #: analyser's ``needs_docker`` flag and propagated through
    #: ``AutomationWorkflow._child_args`` so a Jira task tagged with
    #: ``needs_docker: true`` (or classified as such by the LLM) actually
    #: drives a structured build/run/cleanup pipeline rather than relying
    #: on the user embedding ``docker ...`` in the command string.
    needs_docker: bool = False
    #: Optional Docker image tag for ``docker_build_image``. When unset
    #: the workflow synthesises ``ai-bot-{issue_key}-iter-{N}:latest``
    #: from the workflow context.
    docker_image_tag: str | None = None
    #: Optional Dockerfile path (relative to ``workdir``) for
    #: ``docker_build_image``. Defaults to ``"Dockerfile"`` when
    #: :attr:`needs_docker` is ``True`` and this field is unset.
    docker_dockerfile_path: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutionRunWorkflowOutput:
    """Single result of :class:`ExecutionRunWorkflow.run`.

    Attributes
    ----------
    status:
        ``"passed"``, ``"failed"``, or ``"timeout"``.
    exit_code:
        Process exit code; ``None`` when the run timed out before the
        process finished.
    stdout_uri:
        MinIO URI of the captured stdout, or ``None`` if no output was
        captured (e.g. immediate runner failure).
    stderr_uri:
        MinIO URI of the captured stderr; same null semantics as
        ``stdout_uri``.
    duration_seconds:
        Wall-clock duration as observed by the runner activity.
    runner_id:
        Echo of the runner that executed the command - useful when the
        input ``runner_id`` was ``None`` and the worker chose a default.
    failure_reason:
        Stable failure category when ``status != "passed"``:
        ``"non_zero_exit"``, ``"timeout"``, ``"runner_unreachable"``,
        ``"artifact_upload_failed"``, or ``None``.
    """

    status: ExecutionRunStatus
    exit_code: int | None = None
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    duration_seconds: float = 0.0
    runner_id: str | None = None
    failure_reason: str | None = None
