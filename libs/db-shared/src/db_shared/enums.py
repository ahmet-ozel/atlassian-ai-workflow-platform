"""Shared enumerations for platform data models.

These enums mirror the CHECK constraints and allowed values defined in
the ``006_platform_completion_tables.sql`` migration. They are used by
both the SQLAlchemy models in :mod:`db_shared.models` and by activity /
workflow code that needs to reference valid status or type values
without hard-coding strings.
"""

from __future__ import annotations

import enum


class StepStatus(str, enum.Enum):
    """Status of a workflow step in the Multi-Step Orchestrator.

    Valid transitions: pending  running  completed | failed.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ActionStatus(str, enum.Enum):
    """Execution outcome of a single output action.
    """

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"


class ActionType(str, enum.Enum):
    """Types of output actions the Output_Action_Executor can perform.
    """

    JIRA_COMMENT = "jira_comment"
    JIRA_ATTACHMENT = "jira_attachment"
    BITBUCKET_COMMIT = "bitbucket_commit"
    BITBUCKET_PR = "bitbucket_pr"
    CONFLUENCE_PAGE = "confluence_page"
    JIRA_TRANSITION = "jira_transition"


class ApprovalEventType(str, enum.Enum):
    """Lifecycle events for the Approval Gate workflow.
    """

    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
