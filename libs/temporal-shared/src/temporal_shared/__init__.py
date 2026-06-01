"""temporal_shared — paylaşılan Temporal sabitleri ve yardımcı fonksiyonlar.

Re-exports the workflow-type → capability set mapping, the capability
gate (pure functions), pure-function ID/path formatters, and the
idempotent workflow start helper.

Sources of truth:

* :data:`WORKFLOW_TYPE_CAPABILITIES`, :func:`derive_capabilities`,
  :func:`gate`, :class:`GateDecision` —
  :mod:`temporal_shared.capabilities`
  (platform-mimari-foundation design.md §"libs/temporal-shared.capabilities";
  Requirements 4.1, 4.3, 4.4, 4.7, 4.8, 4.9).
* :func:`coerce_draft_true`, :func:`should_cleanup` —
  :mod:`temporal_shared.helpers`.
* Workflow ID / branch / artifact key formatters,
  :class:`WorkflowIdRef`, :func:`jira_workflow_id`,
  :func:`bitbucket_pr_workflow_id`, :func:`parse_workflow_id` —
  :mod:`temporal_shared.identifiers`
  (platform-mimari-workflows design.md §"temporal_shared.identifiers";
  Requirement 2.1, Property 1).
* :func:`start_workflow_idempotent`, :class:`StartResult` —
  :mod:`temporal_shared.start_helper`
  (platform-mimari-foundation design.md §"WorkflowAlreadyStarted";
  Requirement 1.6).
* :data:`WORKFLOW_TASK_QUEUES`, :func:`task_queue_for` —
  :mod:`temporal_shared.workflow_registry`
  (platform-mimari-workflows design.md §"temporal_shared.workflow_registry";
  Requirements 1.1, 1.2).
* :func:`format_page_title`, :func:`compute_provenance_footer` —
  :mod:`temporal_shared.confluence`
  (platform-mimari-workflows design.md §"Components and Interfaces"
  and Property 9; Requirements 8.1, 8.6).
* :class:`SkipDecision`, :func:`should_skip_section_update`,
  :func:`should_skip_overwrite`, :func:`is_probe_page`,
  :data:`AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP`,
  :data:`AUDIT_CONFLUENCE_OVERWRITE_PROTECTED`,
  :data:`DEFAULT_OVERWRITE_FRESHNESS`,
  :data:`PROBE_PAGE_TITLE_PREFIX` —
  :mod:`temporal_shared.confluence_dedup`
  (platform-mimari-workflows design.md §"Components and Interfaces"
  and Property 9; Requirements 8.2, 8.3, 8.7).
* :class:`BranchPatternRule`, :class:`RouteDecision`,
  :func:`route_by_branch_pattern`, :data:`DEFAULT_BRANCH_PATTERN_RULES`,
  :data:`DEFAULT_HOTFIX_RULE`, :data:`DEFAULT_RELEASE_RULE` —
  :mod:`temporal_shared.branch_rules`
  (platform-mimari-workflows design.md §"`branch_pattern_rules` bir
  saf fonksiyon"; Requirement 7.9, Property 13).
* :func:`compute_branch_name`, :func:`format_commit_message`,
  :data:`BOT_COMMIT_PREFIX`, :class:`InvalidIterationError`,
  :class:`InvalidBotEmailError` —
  :mod:`temporal_shared.code_change`
  (platform-mimari-workflows design.md §"Code change akışları";
  Requirements 7.1, 7.2, Property 13).
* Workflow / activity message dataclasses
  (:class:`AutomationWorkflowInput`,
  :class:`AutomationWorkflowOutput`,
  :class:`AgentRunnerWorkflowInput`,
  :class:`AgentRunnerWorkflowOutput`,
  :class:`ExecutionRunWorkflowInput`,
  :class:`ExecutionRunWorkflowOutput`,
  :class:`WebhookEvent`, :class:`OutputAction`,
  :class:`LlmAnalysisResult`, :class:`IterationState`,
  :class:`ChildWorkflowSpec`, :class:`CompensationContext`) —
  :mod:`temporal_shared.messages`
  (platform-mimari-workflows design.md §"Components and Interfaces";
  Requirements 1.9, 12.1).
* :class:`ChildProposal`, :class:`ChildPlan`, :class:`ChildOutcome`,
  :class:`AggregatedOutput`, :class:`InvariantViolation`,
  :func:`multi_step_dispatch`, :func:`aggregated_output` —
  :mod:`temporal_shared.multi_step`
  (platform-mimari-workflows design.md §"Workflow Type Routing"
  multi_step graceful skip; Requirement 6.3, Property 17).
* :data:`MAX_OUTPUT_BYTES`, :data:`SUMMARY_TRUNCATE_CHARS`,
  :data:`MINIO_KEY_TEMPLATE`, :data:`FINAL_COMMENT_CRITICAL_PREFIX`,
  :data:`FINAL_COMMENT_BEST_EFFORT_PREFIX`,
  :class:`MinioCallback`, :func:`measure_payload_bytes`,
  :func:`redirect_oversized_payload`,
  :func:`format_final_jira_comment` —
  :mod:`temporal_shared.output_size_cap`
  (platform-mimari-workflows design.md §"Property 10(e)" and
  Requirements 5.9, 12.3 — output-action size cap with MinIO
  redirection + final Jira comment formatter).
* :data:`PDF_MAGIC`, :data:`DETERMINISTIC_PDF_TIMESTAMP`,
  :class:`PdfRenderError`, :class:`PdfRenderUnavailableError`,
  :func:`render_pdf` —
  :mod:`temporal_shared.pdf_render`
  (platform-mimari-workflows tasks.md §8.3 / design.md
  §"Components and Interfaces"; Requirements 8.8, 12.4 —
  Jinja2 → WeasyPrint deterministic PDF rendering for the
  ``jira_attachment`` output action).
* :func:`format_noop_result_comment`,
  :data:`NOOP_STDOUT_TRUNCATE_CHARS`,
  :data:`NOOP_TRUNCATION_MARKER`, :data:`NOOP_SUCCESS_PREFIX`,
  :data:`NOOP_FAILURE_PREFIX`, :data:`NOOP_EXIT_CODE_UNKNOWN` —
  :mod:`temporal_shared.noop_formatter`
  (platform-mimari-workflows tasks.md §10.4 / design.md
  §"Workflow Type Routing"; Requirement 6.8 —
  pure Jira-comment formatter for the ``noop_test`` smoke flow).
* :class:`Branch`, :class:`PullRequest`, :data:`AI_BRANCH_PREFIX`,
  :func:`compute_orphan_branches`, :func:`compute_po_review_inbox` —
  :mod:`temporal_shared.po_review`
  (platform-mimari-workflows tasks.md §14.1 / design.md
  §"Components and Interfaces"; Requirements 10.3, 10.4, Property 8 —
  pure set-algebra helpers powering the Orphan Branches and PO
  Review Inbox API endpoints).
* :class:`RepoMapping`, :class:`RepoMappingDiff`,
  :func:`compute_repo_mapping_diff` —
  :mod:`temporal_shared.repo_sync`
  (platform-mimari-workflows tasks.md §14.3 / design.md
  §"Components and Interfaces — repo_mapping_sync API";
  Requirement 10.7 — pure three-way set-algebra diff between a
  Bitbucket workspace scan and the dept's current
  ``repo_mappings`` array, used by the
  ``POST /admin/departments/{id}/repo-mappings/sync``
  admin endpoint, MIMARI §16.16 N7).
"""

from .branch_rules import (
    DEFAULT_BRANCH_PATTERN_RULES,
    DEFAULT_HOTFIX_RULE,
    DEFAULT_RELEASE_RULE,
    BranchPatternRule,
    RouteDecision,
    route_by_branch_pattern,
)
from .capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    GateDecision,
    HasCredential,
    SupportsBot,
    SupportsDepartment,
    derive_capabilities,
    gate,
)
from .compensation import (
    COMPENSATION_STEPS,
    STEP_RESULT_FAILED,
    STEP_RESULT_OK,
    STEP_RESULT_SKIPPED,
    CompensationReport,
    StepOutcome,
)
from .confluence import (
    PAGE_TITLE_DATE_FORMAT,
    PAGE_TITLE_MAX_LENGTH,
    PAGE_TITLE_SEPARATOR,
    PROVENANCE_FOOTER_TEXT_TR,
    InvalidJiraIssueLinkError,
    InvalidTargetLangError,
    InvalidTopicError,
    TargetLang,
    compute_provenance_footer,
    format_page_title,
)
from .confluence_dedup import (
    AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
    AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
    DEFAULT_OVERWRITE_FRESHNESS,
    PROBE_PAGE_TITLE_PREFIX,
    SkipDecision,
    is_probe_page,
    should_skip_overwrite,
    should_skip_section_update,
)
from .code_change import (
    BOT_COMMIT_PREFIX,
    InvalidBotEmailError,
    InvalidIterationError,
    compute_branch_name,
    format_commit_message,
)
from .helpers import (
    CleanupPolicy,
    coerce_draft_true,
    should_cleanup,
)
from .identifiers import (
    InvalidIssueKeyError,
    InvalidSlugError,
    InvalidWorkflowIdError,
    WorkflowIdRef,
    agent_artifact_key,
    agent_workflow_id,
    automation_workflow_id_bb,
    automation_workflow_id_jira,
    bitbucket_pr_workflow_id,
    branch_name,
    execution_artifact_key,
    execution_workflow_id,
    jira_workflow_id,
    parse_workflow_id,
)
from .iteration import (
    EXPLAIN_CACHE_MAXSIZE,
    EXPLAIN_CACHE_TTL,
    FIX_DEBOUNCE_WINDOW,
    NEEDS_INFO_MAX_STREAK,
    IterDecision,
    explain_should_skip_llm,
    fix_should_skip_retest,
    is_fix_debounced,
    needs_info_should_terminate,
    record_explain_answer,
    should_advance_iter,
)
from .llm_dedup import (
    compute_diff_summary,
    dedup_findings,
)
from .messages import (
    BEST_EFFORT_OUTPUT_ACTION_KINDS,
    CRITICAL_OUTPUT_ACTION_KINDS,
    AgentRunnerStatus,
    AgentRunnerWorkflowInput,
    AgentRunnerWorkflowOutput,
    AutomationDecision,
    AutomationWorkflowInput,
    AutomationWorkflowOutput,
    ChildWorkflowSpec,
    CompensationContext,
    CompensationReason,
    ExecutionRunStatus,
    ExecutionRunWorkflowInput,
    ExecutionRunWorkflowOutput,
    ExplainCacheEntry,
    IterationState,
    LlmAnalysisResult,
    OutputAction,
    OutputActionKind,
    OutputActionSeverity,
    Provider,
    WebhookEvent,
)
from .multi_step import (
    REASON_DISPATCHED,
    REASON_NESTED_MULTI_STEP,
    REASON_OUT_OF_SCOPE,
    REASON_UNKNOWN_WORKFLOW_TYPE,
    AggregatedOutput,
    ChildOutcome,
    ChildPlan,
    ChildProposal,
    InvariantViolation,
    aggregated_output,
    multi_step_dispatch,
)
from .noop_formatter import (
    NOOP_EXIT_CODE_UNKNOWN,
    NOOP_FAILURE_PREFIX,
    NOOP_STDOUT_TRUNCATE_CHARS,
    NOOP_SUCCESS_PREFIX,
    NOOP_TRUNCATION_MARKER,
    format_noop_result_comment,
)
from .output_actions import (
    UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE,
    ApplyResult,
    partition,
)
from .output_size_cap import (
    FINAL_COMMENT_BEST_EFFORT_PREFIX,
    FINAL_COMMENT_CRITICAL_PREFIX,
    MAX_OUTPUT_BYTES,
    MINIO_KEY_TEMPLATE,
    SUMMARY_TRUNCATE_CHARS,
    MinioCallback,
    format_final_jira_comment,
    measure_payload_bytes,
    redirect_oversized_payload,
)
from .pdf_render import (
    DETERMINISTIC_PDF_TIMESTAMP,
    PDF_MAGIC,
    PdfRenderError,
    PdfRenderUnavailableError,
    render_pdf,
)
from .po_review import (
    AI_BRANCH_PREFIX,
    Branch,
    PullRequest,
    compute_orphan_branches,
    compute_po_review_inbox,
)
from .repo_sync import (
    RepoMapping,
    RepoMappingDiff,
    compute_repo_mapping_diff,
)
from .start_helper import (
    StartResult,
    SupportsStartWorkflow,
    start_workflow_idempotent,
)
from .structured_choice import (
    MAX_CANDIDATES,
    UNRESOLVED,
    format_choice_list,
    resolve_choice,
)
from .task_analysis import (
    TaskAnalysisParseError,
    parse_llm_analysis,
)
from .workflow_registry import (
    WORKFLOW_TASK_QUEUES,
    SupportsWorkerBoot,
    task_queue_for,
)

__all__ = [
    # branch rules (R7.9)
    "BranchPatternRule",
    "RouteDecision",
    "route_by_branch_pattern",
    "DEFAULT_BRANCH_PATTERN_RULES",
    "DEFAULT_HOTFIX_RULE",
    "DEFAULT_RELEASE_RULE",
    # capabilities
    "WORKFLOW_TYPE_CAPABILITIES",
    "GateDecision",
    "HasCredential",
    "SupportsBot",
    "SupportsDepartment",
    "derive_capabilities",
    "gate",
    # code_change formatters
    "BOT_COMMIT_PREFIX",
    "InvalidBotEmailError",
    "InvalidIterationError",
    "compute_branch_name",
    "format_commit_message",
    # confluence formatters (R8.1, R8.6)
    "PAGE_TITLE_DATE_FORMAT",
    "PAGE_TITLE_MAX_LENGTH",
    "PAGE_TITLE_SEPARATOR",
    "PROVENANCE_FOOTER_TEXT_TR",
    "InvalidJiraIssueLinkError",
    "InvalidTargetLangError",
    "InvalidTopicError",
    "TargetLang",
    "compute_provenance_footer",
    "format_page_title",
    # confluence dedup / overwrite-protection / probe filter (R8.2, R8.3, R8.7)
    "AUDIT_CONFLUENCE_OVERWRITE_PROTECTED",
    "AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP",
    "DEFAULT_OVERWRITE_FRESHNESS",
    "PROBE_PAGE_TITLE_PREFIX",
    "SkipDecision",
    "is_probe_page",
    "should_skip_overwrite",
    "should_skip_section_update",
    # helpers
    "CleanupPolicy",
    "coerce_draft_true",
    "should_cleanup",
    # identifiers
    "InvalidIssueKeyError",
    "InvalidSlugError",
    "InvalidWorkflowIdError",
    "WorkflowIdRef",
    "agent_artifact_key",
    "agent_workflow_id",
    "automation_workflow_id_bb",
    "automation_workflow_id_jira",
    "bitbucket_pr_workflow_id",
    "branch_name",
    "execution_artifact_key",
    "execution_workflow_id",
    "jira_workflow_id",
    "parse_workflow_id",
    # start helper
    "StartResult",
    "SupportsStartWorkflow",
    "start_workflow_idempotent",
    # workflow registry
    "WORKFLOW_TASK_QUEUES",
    "SupportsWorkerBoot",
    "task_queue_for",
    # messages — workflow / activity I/O dataclasses
    "AutomationWorkflowInput",
    "AutomationWorkflowOutput",
    "AgentRunnerWorkflowInput",
    "AgentRunnerWorkflowOutput",
    "ExecutionRunWorkflowInput",
    "ExecutionRunWorkflowOutput",
    "WebhookEvent",
    "OutputAction",
    "LlmAnalysisResult",
    "IterationState",
    "ExplainCacheEntry",
    "ChildWorkflowSpec",
    "CompensationContext",
    "CRITICAL_OUTPUT_ACTION_KINDS",
    "BEST_EFFORT_OUTPUT_ACTION_KINDS",
    "Provider",
    "OutputActionKind",
    "OutputActionSeverity",
    "CompensationReason",
    "AutomationDecision",
    "AgentRunnerStatus",
    "ExecutionRunStatus",
    # multi_step — graceful skip dispatcher (R6.3, Property 17)
    "ChildProposal",
    "ChildPlan",
    "ChildOutcome",
    "AggregatedOutput",
    "InvariantViolation",
    "multi_step_dispatch",
    "aggregated_output",
    "REASON_OUT_OF_SCOPE",
    "REASON_UNKNOWN_WORKFLOW_TYPE",
    "REASON_NESTED_MULTI_STEP",
    "REASON_DISPATCHED",
    # noop_formatter — Jira-comment formatter for noop_test smoke flow (R6.8)
    "format_noop_result_comment",
    "NOOP_STDOUT_TRUNCATE_CHARS",
    "NOOP_TRUNCATION_MARKER",
    "NOOP_SUCCESS_PREFIX",
    "NOOP_FAILURE_PREFIX",
    "NOOP_EXIT_CODE_UNKNOWN",
    # output_actions — partition + ApplyResult (R12.1, R12.2, R12.3)
    "ApplyResult",
    "UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE",
    "partition",
    # output_size_cap — MinIO redirection + final Jira comment (R5.9, R12.3)
    "MAX_OUTPUT_BYTES",
    "SUMMARY_TRUNCATE_CHARS",
    "MINIO_KEY_TEMPLATE",
    "FINAL_COMMENT_CRITICAL_PREFIX",
    "FINAL_COMMENT_BEST_EFFORT_PREFIX",
    "MinioCallback",
    "measure_payload_bytes",
    "redirect_oversized_payload",
    "format_final_jira_comment",
    # pdf_render — Jinja2 → WeasyPrint deterministic PDF (R8.8, R12.4)
    "DETERMINISTIC_PDF_TIMESTAMP",
    "PDF_MAGIC",
    "PdfRenderError",
    "PdfRenderUnavailableError",
    "render_pdf",
    # po_review — Orphan Branches + PO Review Inbox set algebra (R10.3, R10.4)
    "AI_BRANCH_PREFIX",
    "Branch",
    "PullRequest",
    "compute_orphan_branches",
    "compute_po_review_inbox",
    # repo_sync — Bitbucket workspace scan vs current mappings diff (R10.7, N7)
    "RepoMapping",
    "RepoMappingDiff",
    "compute_repo_mapping_diff",
    # iteration — pure helpers for the AgentRunnerWorkflow signal handlers (R5.1-R5.6)
    "EXPLAIN_CACHE_MAXSIZE",
    "EXPLAIN_CACHE_TTL",
    "FIX_DEBOUNCE_WINDOW",
    "NEEDS_INFO_MAX_STREAK",
    "IterDecision",
    "explain_should_skip_llm",
    "fix_should_skip_retest",
    "is_fix_debounced",
    "needs_info_should_terminate",
    "record_explain_answer",
    "should_advance_iter",
    # llm_dedup — finding dedup + diff-summary cache (R7.6, R10.6)
    "compute_diff_summary",
    "dedup_findings",
    # task_analysis — LLM JSON parser with workflow-type required fields (R6.7)
    "TaskAnalysisParseError",
    "parse_llm_analysis",
    # structured_choice — Y8 multi-repo + Z3 execution fallback (R6.5, R6.6)
    "MAX_CANDIDATES",
    "UNRESOLVED",
    "format_choice_list",
    "resolve_choice",
    # compensation — cancel + compensation chain constants (R8.5, R11.2, R11.3)
    "COMPENSATION_STEPS",
    "STEP_RESULT_FAILED",
    "STEP_RESULT_OK",
    "STEP_RESULT_SKIPPED",
    "CompensationReport",
    "StepOutcome",
]
