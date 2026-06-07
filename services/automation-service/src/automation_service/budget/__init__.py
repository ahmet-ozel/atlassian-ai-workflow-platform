"""Budget enforcement primitives for ``automation-service``.

This package owns the runtime gate that translates the dept-level
``budget_caps`` configuration (``config/departments.json`` mirrored
to ``shared.budget_caps``) and the running cost aggregates in
``shared.cost_tracking`` into a deterministic allow / deny decision
on every workflow start request.

Public surface:

* :class:`BudgetCaps` - frozen mirror of the dept config block.
* :class:`BudgetUsage` - running production-cost aggregate.
* :class:`BudgetDecision` - allow / deny outcome with scope label.
* :class:`BudgetCheckResult` - enhanced outcome with 90% warnings.
* :class:`BudgetCapPolicy` - async ``enforce(dept_id, user_id)``
  helper that the workflow start endpoint calls.
* :func:`check_budget` - enhanced pre-workflow check with 90%
  threshold warnings and Jira comment posting.
* :func:`pre_llm_budget_guard` - inline guard before LLM calls.
* :func:`get_budget_usage_snapshot` - Admin Dashboard data exposure.
* :func:`post_cost_prediction_comment` - best-effort Jira yorum
  poster invoked **after** ``BudgetCapPolicy.enforce`` returns
  ``allow``.
* :class:`CostCommentOutcome` / :class:`CostPredictionLike` - value
  objects exposed alongside the function above so callers can type
  their integration without importing the implementation module.

"""

from __future__ import annotations

from .jira_comment import (
    CostCommentOutcome,
    CostPredictionLike,
    post_cost_prediction_comment,
)
from .policy import (
    AlarmThreshold,
    AlarmThresholdStore,
    BudgetCapPolicy,
    BudgetCaps,
    BudgetCapsProvider,
    BudgetCheckResult,
    BudgetDecision,
    BudgetUsage,
    DenyScope,
    JiraCommentCallback,
    NotificationDispatcher,
    StaticBudgetCapsProvider,
    check_budget,
    configuration_error_response,
    deny_response_body,
    get_budget_usage_snapshot,
    pre_llm_budget_guard,
)

__all__ = [
    "AlarmThreshold",
    "AlarmThresholdStore",
    "BudgetCapPolicy",
    "BudgetCaps",
    "BudgetCapsProvider",
    "BudgetCheckResult",
    "BudgetDecision",
    "BudgetUsage",
    "CostCommentOutcome",
    "CostPredictionLike",
    "DenyScope",
    "JiraCommentCallback",
    "NotificationDispatcher",
    "StaticBudgetCapsProvider",
    "check_budget",
    "configuration_error_response",
    "deny_response_body",
    "get_budget_usage_snapshot",
    "post_cost_prediction_comment",
    "pre_llm_budget_guard",
]
