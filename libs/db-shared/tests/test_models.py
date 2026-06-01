"""Tests for platform-completion SQLAlchemy models and enums.

Validates that models are correctly defined, enums have the expected
values, and the table schema mappings match the migration SQL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from db_shared import (
    ActionStatus,
    ActionType,
    ApprovalEvent,
    ApprovalEventType,
    Base,
    DiskQuotaWarning,
    OutputActionLog,
    SSHHealthcheckLog,
    StepStatus,
    WorkflowStep,
)


# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------


class TestStepStatus:
    """StepStatus enum has exactly the expected values."""

    def test_values(self) -> None:
        assert StepStatus.PENDING.value == "pending"
        assert StepStatus.RUNNING.value == "running"
        assert StepStatus.COMPLETED.value == "completed"
        assert StepStatus.FAILED.value == "failed"

    def test_member_count(self) -> None:
        assert len(StepStatus) == 4

    def test_is_str_enum(self) -> None:
        # str enum allows direct string comparison
        assert StepStatus.PENDING == "pending"


class TestActionStatus:
    """ActionStatus enum has exactly the expected values."""

    def test_values(self) -> None:
        assert ActionStatus.SUCCESS.value == "success"
        assert ActionStatus.FAILED.value == "failed"
        assert ActionStatus.SKIPPED.value == "skipped"
        assert ActionStatus.TIMEOUT.value == "timeout"

    def test_member_count(self) -> None:
        assert len(ActionStatus) == 4


class TestActionType:
    """ActionType enum has exactly the expected values."""

    def test_values(self) -> None:
        assert ActionType.JIRA_COMMENT.value == "jira_comment"
        assert ActionType.JIRA_ATTACHMENT.value == "jira_attachment"
        assert ActionType.BITBUCKET_COMMIT.value == "bitbucket_commit"
        assert ActionType.BITBUCKET_PR.value == "bitbucket_pr"
        assert ActionType.CONFLUENCE_PAGE.value == "confluence_page"
        assert ActionType.JIRA_TRANSITION.value == "jira_transition"

    def test_member_count(self) -> None:
        assert len(ActionType) == 6


class TestApprovalEventType:
    """ApprovalEventType enum has exactly the expected values."""

    def test_values(self) -> None:
        assert ApprovalEventType.REQUESTED.value == "requested"
        assert ApprovalEventType.APPROVED.value == "approved"
        assert ApprovalEventType.REJECTED.value == "rejected"
        assert ApprovalEventType.TIMEOUT.value == "timeout"

    def test_member_count(self) -> None:
        assert len(ApprovalEventType) == 4


# ---------------------------------------------------------------------------
# Model schema mapping tests
# ---------------------------------------------------------------------------


class TestWorkflowStep:
    """WorkflowStep model maps to automation.workflow_steps."""

    def test_table_name(self) -> None:
        assert WorkflowStep.__tablename__ == "workflow_steps"

    def test_schema(self) -> None:
        assert WorkflowStep.__table__.schema == "automation"

    def test_columns(self) -> None:
        col_names = {c.name for c in WorkflowStep.__table__.columns}
        expected = {
            "id", "workflow_id", "step_index", "step_name", "status",
            "start_time", "end_time", "duration_seconds", "output_summary",
            "error", "retry_count", "input_hash", "created_at",
        }
        assert col_names == expected

    def test_unique_constraint(self) -> None:
        # workflow_id + step_index should be unique
        constraints = WorkflowStep.__table__.constraints
        unique_constraints = [
            c for c in constraints
            if hasattr(c, "columns") and len(c.columns) == 2
        ]
        found = False
        for uc in unique_constraints:
            col_names = {c.name for c in uc.columns}
            if col_names == {"workflow_id", "step_index"}:
                found = True
                break
        assert found, "Expected UNIQUE(workflow_id, step_index) constraint"

    def test_repr(self) -> None:
        step = WorkflowStep()
        step.id = uuid.uuid4()
        step.workflow_id = "wf-123"
        step.step_index = 0
        step.step_name = "build"
        step.status = StepStatus.PENDING.value
        repr_str = repr(step)
        assert "WorkflowStep" in repr_str
        assert "wf-123" in repr_str


class TestOutputActionLog:
    """OutputActionLog model maps to automation.output_action_log."""

    def test_table_name(self) -> None:
        assert OutputActionLog.__tablename__ == "output_action_log"

    def test_schema(self) -> None:
        assert OutputActionLog.__table__.schema == "automation"

    def test_columns(self) -> None:
        col_names = {c.name for c in OutputActionLog.__table__.columns}
        expected = {
            "id", "workflow_id", "issue_key", "action_type",
            "action_index", "status", "error", "executed_at",
        }
        assert col_names == expected

    def test_repr(self) -> None:
        log = OutputActionLog()
        log.id = uuid.uuid4()
        log.workflow_id = "wf-456"
        log.action_type = ActionType.JIRA_COMMENT.value
        log.status = ActionStatus.SUCCESS.value
        repr_str = repr(log)
        assert "OutputActionLog" in repr_str
        assert "jira_comment" in repr_str


class TestSSHHealthcheckLog:
    """SSHHealthcheckLog model maps to automation.ssh_healthcheck_log."""

    def test_table_name(self) -> None:
        assert SSHHealthcheckLog.__tablename__ == "ssh_healthcheck_log"

    def test_schema(self) -> None:
        assert SSHHealthcheckLog.__table__.schema == "automation"

    def test_columns(self) -> None:
        col_names = {c.name for c in SSHHealthcheckLog.__table__.columns}
        expected = {"id", "host", "port", "healthy", "error", "checked_at"}
        assert col_names == expected

    def test_repr(self) -> None:
        log = SSHHealthcheckLog()
        log.id = uuid.uuid4()
        log.host = "runner-01.internal"
        log.port = 22
        log.healthy = True
        repr_str = repr(log)
        assert "SSHHealthcheckLog" in repr_str
        assert "runner-01.internal" in repr_str


class TestApprovalEvent:
    """ApprovalEvent model maps to automation.approval_events."""

    def test_table_name(self) -> None:
        assert ApprovalEvent.__tablename__ == "approval_events"

    def test_schema(self) -> None:
        assert ApprovalEvent.__table__.schema == "automation"

    def test_columns(self) -> None:
        col_names = {c.name for c in ApprovalEvent.__table__.columns}
        expected = {
            "id", "workflow_id", "issue_key", "event_type",
            "matched_paths", "approver_account_id", "created_at",
        }
        assert col_names == expected

    def test_repr(self) -> None:
        event = ApprovalEvent()
        event.id = uuid.uuid4()
        event.workflow_id = "wf-789"
        event.event_type = ApprovalEventType.REQUESTED.value
        repr_str = repr(event)
        assert "ApprovalEvent" in repr_str
        assert "requested" in repr_str


class TestDiskQuotaWarning:
    """DiskQuotaWarning model maps to automation.disk_quota_warnings."""

    def test_table_name(self) -> None:
        assert DiskQuotaWarning.__tablename__ == "disk_quota_warnings"

    def test_schema(self) -> None:
        assert DiskQuotaWarning.__table__.schema == "automation"

    def test_columns(self) -> None:
        col_names = {c.name for c in DiskQuotaWarning.__table__.columns}
        expected = {"dept_id", "warned_at", "usage_mb", "quota_mb"}
        assert col_names == expected

    def test_composite_primary_key(self) -> None:
        pk_cols = {c.name for c in DiskQuotaWarning.__table__.primary_key.columns}
        assert pk_cols == {"dept_id", "warned_at"}

    def test_repr(self) -> None:
        warning = DiskQuotaWarning()
        warning.dept_id = "engineering"
        warning.warned_at = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        warning.usage_mb = 8500.0
        warning.quota_mb = 10240.0
        repr_str = repr(warning)
        assert "DiskQuotaWarning" in repr_str
        assert "engineering" in repr_str


# ---------------------------------------------------------------------------
# Base class tests
# ---------------------------------------------------------------------------


class TestBase:
    """Base declarative class is properly configured."""

    def test_base_has_metadata(self) -> None:
        assert Base.metadata is not None

    def test_all_models_registered(self) -> None:
        table_names = {t.name for t in Base.metadata.sorted_tables}
        expected = {
            "workflow_steps",
            "output_action_log",
            "ssh_healthcheck_log",
            "approval_events",
            "disk_quota_warnings",
        }
        assert expected.issubset(table_names)
