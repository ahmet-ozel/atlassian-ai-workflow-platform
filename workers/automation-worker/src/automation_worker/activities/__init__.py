"""Activity modules for the ``automation-worker``.

Activities live in their own subpackage so the boot script can register
them en bloc with the Temporal worker (``automation-tq`` task queue)
while the workflow modules stay free of network-side imports - the
workflow sandbox only ever sees activity *names* (string literals)
referenced through ``workflow.execute_activity(<name>,...)``.

of ```` the four
activities consumed by:class:`AuditPruneWorkflow` ship inside:mod:`automation_worker.activities.audit_prune`. The
```` spec adds four more activity families,
exported here so the boot script can register them in one block:

*:mod:`output_actions` -:func:`execute_output_actions` (-3.11)
*:mod:`repo_resolver` -:func:`resolve_repo_field` (-9.5)
*:mod:`status_mapping` -:func:`resolve_jira_status` (-19.5)
*:mod:`branch_rules` -:func:`evaluate_branch_rules` (-17.5)
*:mod:`description_parser` -:func:`parse_description_frontmatter`
 (, -)
*:mod:`task_analyzer` -:func:`analyze_task` (-,)
*:mod:`iteration_manager` -:func:`prepare_iteration` (-)

Dependency-injection pattern
----------------------------

Mirrors the convention used by ``agent-runner-worker.activities``: each
activity reads its collaborators (Postgres pool, MinIO client,
NotificationService,...) through a module-level *registry* populated
once at worker boot via the ``set_*`` setters declared in each
submodule. Activities then resolve dependencies through their matching
``get_*`` accessor - this keeps the activity functions stateless and
trivially mockable in unit tests (each test just calls the setter
with an in-memory fake).
"""

from __future__ import annotations

# - re-export the new activity callables
# so the boot script can register them via a single
# ``from automation_worker.activities import...`` statement.
from automation_worker.activities.branch_rules import (
    BranchRuleInput,
    BranchRuleResult,
    evaluate_branch_rules,
)
from automation_worker.activities.description_parser import (
    ParsedFrontMatter,
    TIMEOUT_SECONDS_MAX,
    TIMEOUT_SECONDS_MIN,
    VALID_CLEANUP_POLICIES,
    VALID_WORKFLOW_TYPES,
    parse_description_frontmatter,
)
from automation_worker.activities.output_actions import (
    ActionResult,
    ExecutionBatchInput,
    ExecutionBatchResult,
    OutputAction,
    execute_output_actions,
)
from automation_worker.activities.repo_resolver import (
    RepoResolveInput,
    RepoResolveResult,
    resolve_repo_field,
)
from automation_worker.activities.status_mapping import (
    SUPPORTED_LOGICAL_STATES,
    StatusMappingResult,
    resolve_jira_status,
)
from automation_worker.activities.task_analyzer import (
    CONFIDENCE_THRESHOLD as TASK_ANALYZER_CONFIDENCE_THRESHOLD,
    TaskAnalysisError,
    TaskAnalysisInput,
    TaskAnalysisResult,
    analyze_task,
)
from automation_worker.activities.iteration_manager import (
    DEFAULT_WORKSPACE_BASE_PATH as ITERATION_DEFAULT_WORKSPACE_BASE_PATH,
    MAX_ITERATION_NUMBER as ITERATION_MAX_NUMBER,
    IterationContext,
    IterationRecord,
    IterationStore,
    PrepareIterationInput,
    prepare_iteration,
)
from automation_worker.activities.notification_dispatch import (
    DispatchNotificationInput,
    dispatch_notification,
)


__all__: tuple[str, ...] = (
    # output_actions
    "ActionResult",
    "ExecutionBatchInput",
    "ExecutionBatchResult",
    "OutputAction",
    "execute_output_actions",
    # repo_resolver
    "RepoResolveInput",
    "RepoResolveResult",
    "resolve_repo_field",
    # status_mapping
    "SUPPORTED_LOGICAL_STATES",
    "StatusMappingResult",
    "resolve_jira_status",
    # branch_rules
    "BranchRuleInput",
    "BranchRuleResult",
    "evaluate_branch_rules",
    # description_parser 
    "ParsedFrontMatter",
    "TIMEOUT_SECONDS_MAX",
    "TIMEOUT_SECONDS_MIN",
    "VALID_CLEANUP_POLICIES",
    "VALID_WORKFLOW_TYPES",
    "parse_description_frontmatter",
    # task_analyzer 
    "TASK_ANALYZER_CONFIDENCE_THRESHOLD",
    "TaskAnalysisError",
    "TaskAnalysisInput",
    "TaskAnalysisResult",
    "analyze_task",
    # iteration_manager 
    "ITERATION_DEFAULT_WORKSPACE_BASE_PATH",
    "ITERATION_MAX_NUMBER",
    "IterationContext",
    "IterationRecord",
    "IterationStore",
    "PrepareIterationInput",
    "prepare_iteration",
    # notification_dispatch (8.5 /)
    "DispatchNotificationInput",
    "dispatch_notification",
)
