"""Workflow modules for the ``automation-worker``.

Each ``@workflow.defn``-decorated class lives in its own module so the
worker boot script can selectively register them.
"""

from __future__ import annotations

from automation_worker.workflows.approval_gate import (
    APPROVAL_TIMEOUT,
    ApprovalGateInput,
    ApprovalGateResult,
    ApprovalGateWorkflow,
    is_authorized_approver,
    match_approval_paths,
    parse_approval_decision,
)
from automation_worker.workflows.audit_prune import (
    AUDIT_PRUNE_CRON_SCHEDULE,
    AUDIT_PRUNE_WORKFLOW_ID,
    AUTOMATION_TASK_QUEUE,
    DEFAULT_RETENTION_DAYS,
    AuditPruneReport,
    AuditPruneWorkflow,
)
from automation_worker.workflows.automation_workflow import (
    AutomationWorkflow,
)
from automation_worker.workflows.bot_branch_retention import (
    BOT_BRANCH_RETENTION_CRON_SCHEDULE,
    BOT_BRANCH_RETENTION_WORKFLOW_ID,
    BRANCH_RETENTION_DAYS,
    CLOSED_JIRA_STATUSES,
    BotBranch,
    BotBranchRetention,
    BotBranchRetentionReport,
    BranchRetentionDecision,
    should_delete_branch,
)
# — IterationWorkflow is the Temporal-side
# entry point for ``[iterate]`` re-runs dispatched by the webhook
# layer. Registering it alongside ``AutomationWorkflow`` keeps the
# automation-tq queue self-contained (no extra worker process).
from automation_worker.workflows.iteration_workflow import (
    IterationWorkflow,
    IterationWorkflowInput,
    IterationWorkflowOutput,
)
# — register the multi-step orchestrator
# alongside the existing workflows so the boot script picks it up.
from automation_worker.workflows.multi_step_workflow import (
    MAX_STEPS,
    MIN_STEPS,
    EpicSubtaskDefinition,
    EpicSubtaskInput,
    EpicSubtaskResult,
    EpicSubtaskStepResult,
    EpicSubtaskWorkflow,
    MultiStepInput,
    MultiStepResult,
    MultiStepWorkflow,
    StepDefinition,
    StepResult,
)
# — periodic webhook secret rotation
# auto-finalize workflow.
from automation_worker.workflows.webhook_rotation_finalize import (
    WEBHOOK_ROTATION_FINALIZE_CRON_SCHEDULE,
    WEBHOOK_ROTATION_FINALIZE_WORKFLOW_ID,
    WebhookFinalizeError,
    WebhookOverlapEntry,
    WebhookRotationFinalizeReport,
    WebhookRotationFinalizeWorkflow,
)
# Single-runner canonical contract — G2: hourly disk auto-prune cron.
from automation_worker.workflows.workspace_cleanup import (
    DEFAULT_EVICT_PCT,
    DEFAULT_WARN_PCT,
    MAX_PRUNES_PER_TICK,
    WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE,
    WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID,
    WorkspaceCleanupReport,
    WorkspaceCleanupSchedulerWorkflow,
    WorkspaceDiskSnapshot,
    WorkspaceIterEntry,
    WorkspacePruneResult,
)

__all__: tuple[str, ...] = (
    "APPROVAL_TIMEOUT",
    "AUDIT_PRUNE_CRON_SCHEDULE",
    "AUDIT_PRUNE_WORKFLOW_ID",
    "AUTOMATION_TASK_QUEUE",
    "ApprovalGateInput",
    "ApprovalGateResult",
    "ApprovalGateWorkflow",
    "BOT_BRANCH_RETENTION_CRON_SCHEDULE",
    "BOT_BRANCH_RETENTION_WORKFLOW_ID",
    "BRANCH_RETENTION_DAYS",
    "CLOSED_JIRA_STATUSES",
    "DEFAULT_RETENTION_DAYS",
    "AuditPruneReport",
    "AuditPruneWorkflow",
    "AutomationWorkflow",
    "BotBranch",
    "BotBranchRetention",
    "BotBranchRetentionReport",
    "BranchRetentionDecision",
    "is_authorized_approver",
    "match_approval_paths",
    "parse_approval_decision",
    "should_delete_branch",
    # — IterationWorkflow + I/O envelopes.
    "IterationWorkflow",
    "IterationWorkflowInput",
    "IterationWorkflowOutput",
    # MultiStep (–5.10)
    "MAX_STEPS",
    "MIN_STEPS",
    "MultiStepInput",
    "MultiStepResult",
    "MultiStepWorkflow",
    "StepDefinition",
    "StepResult",
    # Epic subtask orchestration (,)
    "EpicSubtaskDefinition",
    "EpicSubtaskInput",
    "EpicSubtaskResult",
    "EpicSubtaskStepResult",
    "EpicSubtaskWorkflow",
    # Webhook rotation auto-finalize 
    "WEBHOOK_ROTATION_FINALIZE_CRON_SCHEDULE",
    "WEBHOOK_ROTATION_FINALIZE_WORKFLOW_ID",
    "WebhookFinalizeError",
    "WebhookOverlapEntry",
    "WebhookRotationFinalizeReport",
    "WebhookRotationFinalizeWorkflow",
    # Workspace disk auto-prune (single-runner canonical contract — G2)
    "DEFAULT_EVICT_PCT",
    "DEFAULT_WARN_PCT",
    "MAX_PRUNES_PER_TICK",
    "WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE",
    "WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID",
    "WorkspaceCleanupReport",
    "WorkspaceCleanupSchedulerWorkflow",
    "WorkspaceDiskSnapshot",
    "WorkspaceIterEntry",
    "WorkspacePruneResult",
)
