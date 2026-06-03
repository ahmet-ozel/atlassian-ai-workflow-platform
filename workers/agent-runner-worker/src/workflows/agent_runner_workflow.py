"""Compatibility shim — re-exports the canonical AgentRunnerWorkflow.

The canonical implementation moved to
:mod:`agent_runner.workflows.agent_runner_workflow` to mirror the
``automation-worker`` package layout.

This module remains importable under the legacy ``src.workflows`` path
so that historical test fixtures and the ``automation_workflow`` /
``compensation`` siblings under ``src/workflows/`` keep working without
churn. New code SHOULD import from :mod:`agent_runner.workflows`.

Behavior is provided by the re-exported canonical implementation.
"""

from __future__ import annotations

from agent_runner.workflows.agent_runner_workflow import (
    EXPLAIN_CACHE_HIT_AUDIT_ACTION,
    EXPLAIN_CACHE_TTL,
    FIX_DEBOUNCE_AUDIT_ACTION,
    FIX_DEBOUNCE_WINDOW,
    FIX_RETEST_PROTECTED_AUDIT_ACTION,
    ITER_WARNING_AUDIT_ACTION,
    ITER_WARNING_BANNER_TEXT,
    ITER_WARNING_THRESHOLD,
    LLM_RETRY_POLICY,
    MAX_ACTIVITY_TOKEN_CAP,
    MAX_ITER,
    NEEDS_INFO_MAX_STREAK,
    SIGNAL_WAIT_TIMEOUT,
    TOKEN_CAP_AUDIT_ACTION,
    TOKEN_CAP_ERROR_TYPE,
    AgentRunnerWorkflow,
    CancelRequestedSignal,
    CommentAddedSignal,
    ExplainTriggeredSignal,
    FixTriggeredSignal,
    TokenCapExceededError,
)

__all__ = [
    "AgentRunnerWorkflow",
    "CancelRequestedSignal",
    "CommentAddedSignal",
    "ExplainTriggeredSignal",
    "FixTriggeredSignal",
    "TokenCapExceededError",
    "MAX_ITER",
    "MAX_ACTIVITY_TOKEN_CAP",
    "FIX_DEBOUNCE_WINDOW",
    "EXPLAIN_CACHE_TTL",
    "NEEDS_INFO_MAX_STREAK",
    "ITER_WARNING_THRESHOLD",
    "ITER_WARNING_BANNER_TEXT",
    "ITER_WARNING_AUDIT_ACTION",
    "TOKEN_CAP_AUDIT_ACTION",
    "TOKEN_CAP_ERROR_TYPE",
    "FIX_DEBOUNCE_AUDIT_ACTION",
    "FIX_RETEST_PROTECTED_AUDIT_ACTION",
    "EXPLAIN_CACHE_HIT_AUDIT_ACTION",
    "LLM_RETRY_POLICY",
    "SIGNAL_WAIT_TIMEOUT",
]
