"""SQLAlchemy declarative models for automation tables.

These models map to the tables created by
``infra/postgres/migrations/006_platform_completion_tables.sql`` in the
``automation`` schema. They provide a typed ORM layer for the workflow
step tracking, output action logging, SSH healthcheck history, approval
gate events, and disk quota warning tables.

All models use the SQLAlchemy 2.0 declarative style with
:class:`~sqlalchemy.orm.DeclarativeBase` and ``Mapped[]`` annotations.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from .enums import ActionStatus, ActionType, ApprovalEventType, StepStatus


class Base(DeclarativeBase):
    """Shared declarative base for all automation models."""

    pass


class WorkflowStep(Base):
    """Multi-Step Orchestrator step tracking.

    Each row represents one step within a multi-step workflow execution.
    Steps transition through: pending → running → completed | failed.

    Table: automation.workflow_steps
    """

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint("workflow_id", "step_index", name="uq_workflow_steps_wf_idx"),
        {"schema": "automation"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StepStatus.PENDING.value
    )
    start_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_seconds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    output_summary: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<WorkflowStep(id={self.id!r}, workflow_id={self.workflow_id!r}, "
            f"step_index={self.step_index}, step_name={self.step_name!r}, "
            f"status={self.status!r})>"
        )


class OutputActionLog(Base):
    """Output action execution history.

    Records the outcome of each output action executed by the
    Output_Action_Executor, including action type, index order,
    status, and any error details.

    Table: automation.output_action_log
    """

    __tablename__ = "output_action_log"
    __table_args__ = {"schema": "automation"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<OutputActionLog(id={self.id!r}, workflow_id={self.workflow_id!r}, "
            f"action_type={self.action_type!r}, status={self.status!r})>"
        )


class SSHHealthcheckLog(Base):
    """SSH healthcheck result history.

    Each row records the outcome of a periodic SSH connectivity check
    performed by the Healthcheck_Cron workflow.

    Table: automation.ssh_healthcheck_log
    """

    __tablename__ = "ssh_healthcheck_log"
    __table_args__ = {"schema": "automation"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<SSHHealthcheckLog(id={self.id!r}, host={self.host!r}, "
            f"port={self.port}, healthy={self.healthy})>"
        )


class ApprovalEvent(Base):
    """Approval gate audit events.

    Records lifecycle events for the Approval Gate workflow: when
    approval is requested, granted, rejected, or times out.

    Table: automation.approval_events
    """

    __tablename__ = "approval_events"
    __table_args__ = {"schema": "automation"}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    issue_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    matched_paths: Mapped[Optional[list[str]]] = mapped_column(
        ARRAY(Text), nullable=True
    )
    approver_account_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<ApprovalEvent(id={self.id!r}, workflow_id={self.workflow_id!r}, "
            f"event_type={self.event_type!r})>"
        )


class DiskQuotaWarning(Base):
    """Disk quota warning deduplication records.

    Tracks when disk quota warnings were sent for each department to
    enforce the 60-minute deduplication window.

    Table: automation.disk_quota_warnings
    """

    __tablename__ = "disk_quota_warnings"
    __table_args__ = {"schema": "automation"}

    dept_id: Mapped[str] = mapped_column(Text, primary_key=True)
    warned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    usage_mb: Mapped[float] = mapped_column(Float, nullable=False)
    quota_mb: Mapped[float] = mapped_column(Float, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<DiskQuotaWarning(dept_id={self.dept_id!r}, "
            f"warned_at={self.warned_at!r}, "
            f"usage_mb={self.usage_mb}, quota_mb={self.quota_mb})>"
        )
