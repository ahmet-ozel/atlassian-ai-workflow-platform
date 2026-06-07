"""Notification dispatch activity for workflow completion (/).

Wires the ``automation-worker`` Temporal activity surface to:class:`notification.NotificationService` so a finishing workflow
(``AutomationWorkflow`` / ``IterationWorkflow`` / future siblings)
can fan out a Slack / Email completion notification the decision
table in ```` design.md §`NotificationService`:

* ``status == "failed"``  Slack send is **mandatory** regardless of
 ``dept.notify_on_success``.
* ``status ∈ {"completed", "partial"}``  success-gated; the dispatcher
 short-circuits to a no-op when ``notify_on_success == False``.
* Idempotency, body redaction and the ``shared.notification_log`` row
 shape are all enforced inside:class:`NotificationService` - this
 activity is the wire-in point.

Single activity exported:

*:func:`dispatch_notification` - the primary call. Builds a:class:`notification.DeptConfigView` and:class:`notification.WorkflowResult` from the flat:class:`DispatchNotificationInput` payload, then forwards to:meth:`NotificationService.notify_workflow_completion`.

Best-effort contract
--------------------

The activity **swallows** every dispatch-side exception and returns
without raising - a notification failure must never block the
workflow's terminal path (the workflow has already done the
operator-visible work; missing a Slack ping is far less bad than
holding the workflow open forever or, worse, retrying the user's
PR). Every swallowed exception is logged via:data:`activity.logger` so an operator chasing a missing notification
can correlate via Loki.

Dependency injection
--------------------

Mirrors the convention used by:mod:`automation_worker.activities.audit_prune`:

*:func:`set_notification_service` /:func:`get_notification_service`
 - the boot script (:mod:`automation_worker.main`) registers the
 shared:class:`NotificationService` (or a lazy-built wrapper) on
 startup. Tests inject an in-memory fake via:func:`set_notification_service`.

The module-level state is **separate** from:mod:`automation_worker.activities.audit_prune` (which has its own
``_notification_service`` slot for the audit-prune admin alarm) so a
test that swaps one does not interfere with the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from temporalio import activity


__all__ = (
    # Public dataclasses
    "DispatchNotificationInput",
    # Activity
    "dispatch_notification",
    # Setters / accessors
    "set_notification_service",
    "get_notification_service",
)


# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchNotificationInput:
    """Single input of:func:`dispatch_notification` (Temporal-serialisable).

 The dataclass is deliberately **flat** (no nested:class:`notification.DeptConfigView` /:class:`notification.WorkflowResult`
 instances) so Temporal's default JSON data converter can ship the
 payload without a custom codec - the activity body re-builds the
 typed objects on the worker side.

 Args:
 workflow_id: Stable id of the completed workflow. Hashed into
 the notification ``dedup_key`` so a retried call cannot
 double-deliver. Pass the parent Temporal ``workflow_id``
 (``workflow.info.workflow_id`` from inside the workflow body).
 dept_id: Stable department slug; surfaced into the:class:`notification.DeptConfigView`. Used as an audit
 field by:class:`NotificationService`.
 notify_on_success: ``True`` when the department wants
 notifications for ``"completed"`` / ``"partial"`` workflows;
 ``False`` is the default (success-gated dispatch becomes a
 no-op). Mirrors ``departments.json::notify_on_success``.
 notify_channels: Tuple of channel names the department
 subscribed to. ``frozenset`` is built inside the activity.
 Failure notifications **always** include Slack regardless
 of this set. Tuple (not frozenset) for Temporal
 JSON serialisation friendliness.
 slack_webhook: Resolved Slack webhook URL (already de-referenced
 from any ``vault:`` ref). ``None`` means the department
 has no Slack channel configured.
 notify_email: Resolved RFC-5322 email address for the
 department. ``None`` means email is not configured.
 status: Terminal workflow status. One of ``"completed"`` /
 ``"failed"`` / ``"partial"``. Drives the success-gated /
 failure-mandatory branches inside:meth:`NotificationService.notify_workflow_completion`.
 summary: Single-line human-readable summary surfaced into the
 rendered notification body via the ``{result_summary}``
 placeholder.
 error: Error message for ``status="failed"``; rendered into
 the ``{error}`` placeholder. Long stack traces should be
 truncated by the caller - the field is not redacted by
 the dispatcher.
 jira_issue_url: Optional Jira issue URL surfaced into
 ``{jira_issue_url}``. ``None`` is rendered as the literal
 string ``"-"``.
 """

    workflow_id: str
    dept_id: str
    notify_on_success: bool = False
    notify_channels: tuple[str, ...] = field(default_factory=tuple)
    slack_webhook: str | None = None
    notify_email: str | None = None
    status: str = "completed"
    summary: str = ""
    error: str | None = None
    jira_issue_url: str | None = None


# ---------------------------------------------------------------------------
# Dependency-injection registry
# ---------------------------------------------------------------------------


@runtime_checkable
class _NotificationServiceLike(Protocol):
    """Minimal:class:`notification.NotificationService` surface.

 Only:meth:`notify_workflow_completion` is part of this contract -
 the audit-prune admin alarm is wired through:mod:`automation_worker.activities.audit_prune` against the same
 underlying service instance. Declaring a Protocol keeps this
 module free of a hard runtime dependency on:mod:`notification`
 (so the worker package can import the activity even if the
 notification lib is not installed in the test environment).
 """

    async def notify_workflow_completion(
        self,
        *,
        workflow_id: str,
        dept: Any,
        result: Any,
    ) -> Any:
        ...


# Module-level state - kept **separate** from the audit_prune
# module's ``_notification_service`` slot so a test that swaps one
# does not interfere with the other. The boot script
# (:mod:`automation_worker.main`) registers the *same* underlying
#:class:`NotificationService` instance into both modules so the
# worker only constructs one Slack adapter / one notification_log
# pool.
_notification_service: _NotificationServiceLike | None = None


def set_notification_service(service: _NotificationServiceLike) -> None:
    """Register the:class:`NotificationService` instance for the activity.

 Called once at worker boot (:mod:`automation_worker.main`) after
 the dispatcher / Slack / email adapters are wired. Unit tests
 call this with an in-memory fake whose:meth:`notify_workflow_completion` records its inputs.
 """

    global _notification_service  # noqa: PLW0603
    _notification_service = service


def get_notification_service() -> _NotificationServiceLike:
    """Resolve the registered service or fail loudly.

 Activities call this through the accessor (rather than reading
 the module global directly) so misconfiguration surfaces as a
 clear ``RuntimeError`` in worker logs instead of an
 ``AttributeError`` deep inside the dispatch pipeline.
 """

    if _notification_service is None:
        raise RuntimeError(
            "notification_dispatch activity: NotificationService not "
            "initialised; call set_notification_service during "
            "worker startup."
        )
    return _notification_service


# ---------------------------------------------------------------------------
# dispatch_notification activity
# ---------------------------------------------------------------------------


@activity.defn(name="dispatch_notification")
async def dispatch_notification(inp: DispatchNotificationInput) -> None:
    """Forward a workflow completion to:class:`NotificationService`.

 Best-effort: every failure branch logs and returns rather than
 raising so the workflow's terminal path is never blocked by a
 notification transport hiccup. Idempotency is enforced inside:class:`NotificationService` (the deterministic ``dedup_key``
 derived from ``sha256(workflow_id:channel:kind)`` makes a
 Temporal-driven retry a safe no-op for any given channel).

 The activity:

 1. Resolves the registered:class:`NotificationService`. Missing
 service  log + return (dev / test environment without the
 notification lib wired).
 2. Lazily imports the notification lib types
 (:class:`DeptConfigView`,:class:`WorkflowResult`) so a worker
 that ships without:mod:`notification` (focused unit-test
 environments) does not blow up at module-import time.
 3. Builds the typed value objects from the flat:class:`DispatchNotificationInput` payload.
 4. Forwards to:meth:`NotificationService.notify_workflow_completion`.
 """

    # 1. Resolve the registered service. Missing service is a normal
    # branch in dev / test - log + return so the workflow's
    # terminal path stays unblocked.
    try:
        service = get_notification_service()
    except RuntimeError as exc:
        activity.logger.warning(
            "dispatch_notification: NotificationService not registered; "
            "skipping (workflow_id=%s, status=%s, error=%s)",
            inp.workflow_id,
            inp.status,
            exc,
        )
        return

    # 2. Lazily import the notification lib types. Deferring the
    # import means a worker that ships without:mod:`notification`
    # (focused unit-test environments) does not blow up at
    # module-import time - only the dispatch activity needs the
    # typed objects.
    try:
        from notification import (  # type: ignore[import-not-found]
            DeptConfigView,
            WorkflowResult,
        )
    except ImportError as exc:
        activity.logger.warning(
            "dispatch_notification: notification lib not importable "
            "(%s); skipping (workflow_id=%s)",
            exc,
            inp.workflow_id,
        )
        return

    # 3. Build the typed value objects. ``frozenset`` construction
    # inside the activity keeps the wire payload Temporal-friendly
    # (a tuple) while honouring the dispatcher's ``frozenset``
    # contract. Any unexpected error here is swallowed (best-effort)
    # so a malformed input never blocks the workflow.
    try:
        dept = DeptConfigView(
            dept_id=inp.dept_id,
            notify_on_success=inp.notify_on_success,
            notify_channels=frozenset(inp.notify_channels),
            slack_webhook=inp.slack_webhook,
            notify_email=inp.notify_email,
        )
        result = WorkflowResult(
            status=inp.status,  # type: ignore[arg-type]
            summary=inp.summary,
            error=inp.error,
            jira_issue_url=inp.jira_issue_url,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        activity.logger.warning(
            "dispatch_notification: failed to build dispatch payload "
            "(%s); skipping (workflow_id=%s)",
            exc,
            inp.workflow_id,
        )
        return

    # 4. Forward to the service. Exceptions are logged + swallowed
    # so the workflow's terminal path is never blocked. The
    # service itself enforces idempotency and the success-gated
    # + failure-mandatory branches.
    try:
        await service.notify_workflow_completion(
            workflow_id=inp.workflow_id,
            dept=dept,
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort (/)
        activity.logger.warning(
            "dispatch_notification failed: %s - best-effort, returning "
            "(workflow_id=%s, status=%s)",
            exc,
            inp.workflow_id,
            inp.status,
        )
        return

    activity.logger.info(
        "dispatch_notification: workflow_id=%s status=%s dept_id=%s",
        inp.workflow_id,
        inp.status,
        inp.dept_id,
    )
