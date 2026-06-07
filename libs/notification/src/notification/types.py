"""Frozen dataclasses + enum-like literals for notification dispatch.

Two value objects are exported:

* :class:`WorkflowResult` - the input shape every Temporal workflow hands the
  notification service when it finishes. Lives in this lib (not a
  worker-private module) because both ``automation-worker`` and
  ``execution-runner-worker`` build the same shape, and downstream consumers
  (Streamlit "completed workflow" widget, audit dashboard) want to pin its
  schema in one place.
* :class:`DeptConfigView` - the *minimum* projection of ``departments.json``
  the notification service needs. The full :mod:`config.departments` schema
  carries ~30 fields; pinning the eight we actually consume keeps the
  notification path orthogonal to dept-schema churn.

Both dataclasses are ``frozen=True``; once a workflow completes the result
must not mutate before the audit row + notification dispatch land. The
``Literal`` types mirror the ``CHECK`` constraints declared in
``infra/postgres/20_ops.sql`` (``shared.notification_log.channel``,
``shared.notification_log.status``) so a typo at the application layer
becomes a static-type-error rather than only a runtime ``IntegrityError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

__all__ = [
    "DeptConfigView",
    "NOTIFICATION_CHANNELS",
    "NOTIFICATION_KINDS",
    "NOTIFICATION_STATUSES",
    "NotificationChannel",
    "NotificationKind",
    "NotificationStatus",
    "WORKFLOW_STATUSES",
    "WorkflowResult",
    "WorkflowStatus",
]


# ---------------------------------------------------------------------------
# Enum-like literals
# ---------------------------------------------------------------------------

#: The three terminal workflow statuses the orchestrator surfaces:
#: ``workflow_result.status ∈ {"completed","failed","partial"}``.
#:
#: * ``"completed"`` - every activity succeeded.
#: * ``"failed"`` - at least one critical activity failed; failure
#:   notification is **mandatory**.
#: * ``"partial"`` - best-effort activities failed but critical path
#:   succeeded; treated as a *success* for notification gating (i.e. only
#:   notifies when ``dept.notify_on_success == True``). This matches the
#:   critical/best-effort split used for ``output_actions``.
WorkflowStatus = Literal["completed", "failed", "partial"]


#: Runtime mirror of :data:`WorkflowStatus` - used by argument validators
#: (``service.py``) and by Hypothesis strategies in property tests.
WORKFLOW_STATUSES: Final[frozenset[str]] = frozenset(
    {"completed", "failed", "partial"}
)


#: Channels the dispatcher knows about. Mirrors the
#: ``shared.notification_log.channel`` ``CHECK`` constraint declared in
#: ``infra/postgres/20_ops.sql`` (``CHECK (channel IN ('slack','email','teams'))``).
#: ``"teams"`` is a forward-compat slot - the current adapter set only
#: implements Slack and email; passing ``"teams"`` is accepted by the
#: schema but the dispatcher will skip it (no adapter wired) until the
#: backlog item ships.
NotificationChannel = Literal["slack", "email", "teams"]


NOTIFICATION_CHANNELS: Final[frozenset[str]] = frozenset(
    {"slack", "email", "teams"}
)


#: Stable identifier for the *kind* of notification. Used as the third
#: input to the ``dedup_key`` sha256 (see :func:`notification.service._dedup_key`)
#: so the same workflow can drive both a "completion" and a future "audit
#: prune failed" alarm without colliding on ``UNIQUE``.
#:
#: Currently:
#: * ``"workflow_completion"`` - terminal workflow notification.
#: * ``"audit_prune_failed"`` - admin alarm.
NotificationKind = Literal["workflow_completion", "audit_prune_failed"]


NOTIFICATION_KINDS: Final[frozenset[str]] = frozenset(
    {"workflow_completion", "audit_prune_failed"}
)


#: Mirrors the ``shared.notification_log.status`` ``CHECK`` constraint:
#: ``CHECK (status IN ('sent','failed','retrying'))``.
#:
#: * ``"sent"`` - adapter succeeded.
#: * ``"failed"`` - adapter raised; the row carries ``error`` for forensic
#:   correlation. The dispatcher does **not** auto-retry; retry is the
#:   caller's responsibility (typically a Temporal activity with a
#:   ``RetryPolicy``).
#: * ``"retrying"`` - reserved for token-bucket back-pressure paths.
NotificationStatus = Literal["sent", "failed", "retrying"]


NOTIFICATION_STATUSES: Final[frozenset[str]] = frozenset(
    {"sent", "failed", "retrying"}
)


# ---------------------------------------------------------------------------
# WorkflowResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowResult:
    """Terminal result of a Temporal workflow.

    Args:
        status: One of :data:`WorkflowStatus`. Drives the dispatch policy:
            ``"failed"`` ⇒ failure-mandatory branch; ``"completed"``
            and ``"partial"`` ⇒ success-gated branch.
        summary: Single-line human-readable summary (eg.
            ``"PR #123 merged"``). Surfaced verbatim into the rendered
            notification body via the ``{result_summary}`` placeholder.
        error: When ``status == "failed"``, an error message suitable for
            inclusion in the Slack body. ``None`` for non-failure paths.
            Surfaced into the ``{error}`` placeholder. Long stack traces
            should be **truncated** by the caller - the field is not
            redacted by the dispatcher.
        jira_issue_url: Optional Jira issue URL. Surfaced into
            ``{jira_issue_url}``. ``None`` is rendered as the literal
            string ``"-"`` (kept stable so the Slack body never has a
            dangling "None").
        artifact_urls: Optional tuple of artifact URLs (PR / Confluence /
            etc.). ``tuple()`` is the empty default. Joined with newlines
            into the ``{artifact_urls}`` placeholder; an empty tuple
            renders as ``"-"``.

    Frozen + ``slots=True`` so the result behaves as a value object: the
    dispatcher hashes ``workflow_id``-derived data into ``dedup_key`` and
    expects the input to be stable across the call.
    """

    status: WorkflowStatus
    summary: str
    error: str | None = None
    jira_issue_url: str | None = None
    artifact_urls: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# DeptConfigView
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptConfigView:
    """Minimal projection of ``departments.json`` consumed by the dispatcher.

    The notification service does **not** depend on the full department
    config schema - only on the eight fields that drive the success-gated
    + failure-mandatory dispatch policy. Defining a narrow view here keeps
    the notification path testable without spinning up the dept-config
    loader and lets ``departments.schema.json`` evolve without forcing a
    notification-lib release.

    Args:
        dept_id: Stable id of the department (eg. ``"payment"``). Used as
            an audit field and as the ``target`` column when dispatching
            to a dept's Slack channel.
        notify_on_success: ``True`` when the department wants notifications
            for ``"completed"`` and ``"partial"`` workflows; ``False`` is
            the default (dispatch becomes a no-op for non-failure outcomes).
            Mirrors ``departments.json::departments[].notify_on_success``
            .
        notify_channels: Channels the department subscribed to for
            **success** notifications. Failure notifications **always**
            include Slack regardless of this set. Stored as a
            ``frozenset`` so the value is hashable and unordered.
        slack_webhook: Resolved Slack webhook URL (already de-referenced
            from the ``vault:`` ref the schema records). The dispatcher
            never reads from Vault directly - the caller resolves the ref
            and passes the secret in. ``None`` is allowed and means the
            department has no Slack channel configured; failure
            notifications then fall back to the admin Slack channel via
            the admin alarm path, but :meth:`notify_workflow_completion` itself
            simply skips the Slack channel.
        notify_email: Resolved RFC-5322 email address for the department.
            ``None`` means email is not configured.

    Frozen + ``slots=True`` so the view is hashable and may appear in
    audit payloads / dispatch logs.
    """

    dept_id: str
    notify_on_success: bool
    notify_channels: frozenset[NotificationChannel]
    slack_webhook: str | None
    notify_email: str | None
