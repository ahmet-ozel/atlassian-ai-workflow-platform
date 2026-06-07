"""Jira issue_assigned webhook handler with dept-handover logic (Feature 8).

Handles the edge case where a task is re-assigned from one department's
bot to another department's bot. When this happens:

1. The existing workflow (running under the old dept context) is cancelled.
2. A new workflow is started under the new dept context.
3. An audit event ``workflow_dept_handover`` is emitted.
4. A Jira bot comment is posted explaining the handover.

Only assignments to a configured Jira bot account start automation. Human
assignments are accepted and ignored. If the assignment moves from one
department bot to another, the handler performs a department handover.

Endpoint: ``POST /webhooks/jira/issue_assigned``
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from temporal_shared.capabilities import (
    GateDecision,
    SupportsDepartment,
    derive_capabilities,
    gate,
)
from temporal_shared.messages import AutomationWorkflowInput
from temporal_shared.start_helper import (
    StartResult,
    SupportsStartWorkflow,
    start_workflow_idempotent,
)
from temporal_shared.workflow_registry import task_queue_for

__all__ = ["handle_issue_assigned"]

logger = logging.getLogger(__name__)

_EVENT_ISSUE_ASSIGNED = "jira:issue_assigned"
_AUDIT_ACTOR_ID = "automation-service.webhook"
_WORKFLOW_NAME = "AutomationWorkflow"
_TASK_QUEUE = task_queue_for(_WORKFLOW_NAME)

#: Bot comment posted when a task is handed over between departments.
_DEPT_HANDOVER_COMMENT = (
    " Bu task {old_dept}  {new_dept}'e devredildi. "
    "Önceki çalışma iptal edildi, yeni dept context'i ile başlatıldı."
)

#: Bot comment posted when the assigned bot's dept has insufficient capabilities.
_CAPABILITY_DENIED_COMMENT = (
    " Otomasyon başlatılamadı: bu departman için eksik yetenek(ler): {missing}. "
    "Lütfen sistem yöneticinize başvurun."
)


def _extract_assignee_account_id(payload: dict[str, Any]) -> str | None:
    """Extract the new assignee's account_id from the webhook payload."""
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None
    assignee = fields.get("assignee")
    if not isinstance(assignee, dict):
        return None
    return assignee.get("accountId")


def _extract_issue_key(payload: dict[str, Any]) -> str | None:
    """Extract the issue key from the webhook payload."""
    issue = payload.get("issue")
    if isinstance(issue, dict):
        key = issue.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _extract_project_key(payload: dict[str, Any]) -> str | None:
    """Extract the project key from the webhook payload."""
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    fields = issue.get("fields")
    if isinstance(fields, dict):
        project = fields.get("project")
        if isinstance(project, dict):
            key = project.get("key")
            if isinstance(key, str) and key:
                return key
    return None


def _build_workflow_id(issue_key: str, suffix: str = "") -> str:
    """Build a stable workflow ID for the issue."""
    base = f"automation-jira-{issue_key}"
    if suffix:
        return f"{base}-{suffix}"
    return base


def _simple_capabilities(dept: SupportsDepartment, env: dict[str, str]) -> tuple[str, ...]:
    """Collapse split capabilities into the workflow input vocabulary."""
    simple: set[str] = set()

    def add_capability(cap: str) -> None:
        if cap.startswith("jira_"):
            simple.add("jira")
        elif cap.startswith("bitbucket_"):
            simple.add("bitbucket")
        elif cap.startswith("confluence_"):
            simple.add("confluence")
        else:
            simple.add(cap)

    for cap in getattr(dept, "available_capabilities", ()) or ():
        if isinstance(cap, str) and cap.strip():
            add_capability(cap.strip())
    for cap in derive_capabilities(dept, env):
        add_capability(cap)
    runner_assigned = any(
        str(env.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
        for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
    )
    legacy_ssh = any(
        (key == "SSH_HOST" or key.startswith("SSH_HOST_")) and str(value).strip()
        for key, value in env.items()
    )
    if runner_assigned or legacy_ssh:
        simple.add("execution")
    return tuple(sorted(simple))


def _workflow_input(
    *,
    issue_key: str,
    project_key: str | None,
    dept_id: str,
    dept: SupportsDepartment,
    env: dict[str, str],
    trigger_event: str = _EVENT_ISSUE_ASSIGNED,
) -> AutomationWorkflowInput:
    del project_key
    repos = getattr(dept, "available_repos", ())
    spaces = getattr(dept, "available_spaces", ())
    return AutomationWorkflowInput(
        issue_key=issue_key,
        department_id=dept_id,
        available_capabilities=_simple_capabilities(dept, env),
        available_repos=tuple(repos or ()),
        available_spaces=tuple(spaces or ()),
        default_language=str(getattr(dept, "default_language", "tr") or "tr"),
        trigger_event=trigger_event,
    )


async def handle_issue_assigned(
    payload: dict[str, Any],
    *,
    dept_resolver: Any,
    workflow_client: SupportsStartWorkflow,
    jira_commenter: Any | None,
    audit_logger: AuditLogger,
    env: dict[str, str],
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    """Process a jira:issue_assigned event with dept-handover detection.

    Parameters
    ----------
    payload:
        Parsed JSON body of the webhook.
    dept_resolver:
        Resolves project_key  department.
    workflow_client:
        Temporal workflow client for starting/cancelling workflows.
    jira_commenter:
        Posts bot comments to Jira issues.
    audit_logger:
        Writes audit events.
    env:
        Environment mapping for capability gate.
    now_fn:
        Current time factory.

    Returns
    -------
    dict
        Response body with status, decision, and workflow details.
    """
    issue_key = _extract_issue_key(payload)
    if not issue_key:
        return {"status": "bad_request", "reason": "missing_issue_key"}

    project_key = _extract_project_key(payload)
    assignee_account_id = _extract_assignee_account_id(payload)

    if not assignee_account_id:
        return {"status": "accepted", "decision": "no_assignee", "issue_key": issue_key}

    bot_registry = await dept_resolver.list_bot_account_ids()
    assignee_dept_id: str | None = None
    is_bot_assignee = False

    for entry in bot_registry:
        if entry.account_id == assignee_account_id:
            is_bot_assignee = True
            assignee_dept_id = entry.dept_id
            break

    if not is_bot_assignee:
        project_dept_id: str | None = None
        if project_key:
            project_dept = await dept_resolver.resolve_by_project_key(project_key)
            if project_dept:
                project_dept_id = getattr(project_dept, "id", None)
        await _emit_audit(
            audit_logger,
            action="webhook_assignment_not_bot",
            resource="webhook:jira/issue_assigned",
            result="ok",
            dept_id=project_dept_id,
            payload={"issue_key": issue_key, "assignee_account_id": assignee_account_id},
            now_fn=now_fn,
        )
        return {
            "status": "accepted",
            "decision": "not_bot_assignee",
            "issue_key": issue_key,
        }

    # Resolve the department for the project.
    dept: SupportsDepartment | None = None
    dept_id: str | None = None
    if project_key:
        dept = await dept_resolver.resolve_by_project_key(project_key)
        if dept:
            dept_id = getattr(dept, "id", None)

    if (not dept or not dept_id) and assignee_dept_id:
        resolve_by_dept_id = getattr(dept_resolver, "resolve_by_dept_id", None)
        if callable(resolve_by_dept_id):
            dept = await resolve_by_dept_id(assignee_dept_id)
            if dept:
                dept_id = getattr(dept, "id", assignee_dept_id) or assignee_dept_id

    if not dept or not dept_id:
        await _emit_audit(
            audit_logger,
            action="webhook_dept_unresolved",
            resource=f"webhook:jira/issue_assigned",
            result="denied",
            dept_id=None,
            payload={"project_key": project_key, "issue_key": issue_key},
            now_fn=now_fn,
        )
        return {"status": "bad_request", "reason": "webhook_dept_unresolved"}

    # If assignee is a bot from a different dept  dept handover.
    if is_bot_assignee and assignee_dept_id and assignee_dept_id != dept_id:
        return await _handle_dept_handover(
            issue_key=issue_key,
            project_key=project_key or "",
            old_dept_id=dept_id,
            new_dept_id=assignee_dept_id,
            dept_resolver=dept_resolver,
            workflow_client=workflow_client,
            jira_commenter=jira_commenter,
            audit_logger=audit_logger,
            env=env,
            now_fn=now_fn,
        )

    # Standard path: assignee is the configured bot for this department.
    # Check capability gate and start workflow.
    decision = gate("noop_test", dept, env)
    if not decision.allowed:
        missing_sorted = sorted(decision.missing)
        await _emit_audit(
            audit_logger,
            action="capability_denied",
            resource=f"workflow:noop_test",
            result="denied",
            dept_id=dept_id,
            payload={"missing": missing_sorted, "issue_key": issue_key},
            now_fn=now_fn,
        )
        if jira_commenter:
            body = _CAPABILITY_DENIED_COMMENT.format(missing=", ".join(missing_sorted))
            try:
                await jira_commenter.post_comment(dept_id, issue_key, body)
            except Exception:  # noqa: BLE001
                pass
        return {
            "status": "accepted",
            "decision": "denied",
            "missing": missing_sorted,
            "issue_key": issue_key,
        }

    workflow_id = _build_workflow_id(issue_key)
    workflow_input = _workflow_input(
        issue_key=issue_key,
        project_key=project_key,
        dept_id=dept_id,
        dept=dept,
        env=env,
    )

    try:
        result: StartResult = await start_workflow_idempotent(
            workflow_client,
            _WORKFLOW_NAME,
            workflow_id,
            [workflow_input],
            task_queue=_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        await _emit_audit(
            audit_logger,
            action="webhook_workflow_start_failed",
            resource="webhook:jira/issue_assigned",
            result="error",
            dept_id=dept_id,
            payload={"workflow_id": workflow_id, "error": type(exc).__name__},
            now_fn=now_fn,
        )
        raise

    await _emit_audit(
        audit_logger,
        action="webhook_workflow_started",
        resource="webhook:jira/issue_assigned",
        result="ok",
        dept_id=dept_id,
        payload={
            "workflow_id": result.execution_id,
            "issue_key": issue_key,
            "was_existing": result.was_existing,
        },
        now_fn=now_fn,
    )

    return {
        "status": "accepted",
        "decision": "accepted",
        "workflow_id": result.execution_id,
        "was_existing": result.was_existing,
        "issue_key": issue_key,
    }


async def _handle_dept_handover(
    *,
    issue_key: str,
    project_key: str,
    old_dept_id: str,
    new_dept_id: str,
    dept_resolver: Any,
    workflow_client: SupportsStartWorkflow,
    jira_commenter: Any | None,
    audit_logger: AuditLogger,
    env: dict[str, str],
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    """Handle a department handover: cancel old workflow, start new one.

    Steps:
    1. Cancel the existing workflow (if running) under old_dept_id.
    2. Start a new workflow under new_dept_id with a -rev suffix.
    3. Emit audit ``workflow_dept_handover``.
    4. Post a Jira bot comment explaining the handover.
    """
    old_workflow_id = _build_workflow_id(issue_key)

    # Step 1: Cancel existing workflow (best-effort).
    cancel_success = False
    try:
        handle = workflow_client.get_workflow_handle(old_workflow_id)
        await handle.cancel()
        cancel_success = True
        logger.info(
            "dept_handover: cancelled old workflow %s (dept=%s)",
            old_workflow_id,
            old_dept_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "dept_handover: cancel of %s failed (may not exist): %s",
            old_workflow_id,
            exc,
        )

    # Step 2: Start new workflow under new dept context.
    # Use -rev1 suffix to avoid Temporal workflow-id collision.
    new_workflow_id = f"{old_workflow_id}-rev1"
    new_dept = None
    resolve_by_dept_id = getattr(dept_resolver, "resolve_by_dept_id", None)
    if callable(resolve_by_dept_id):
        new_dept = await resolve_by_dept_id(new_dept_id)
    if new_dept is None:
        new_dept = await dept_resolver.resolve_by_project_key(project_key)
    if new_dept is None:
        await _emit_audit(
            audit_logger,
            action="workflow_dept_handover_failed",
            resource="webhook:jira/issue_assigned",
            result="error",
            dept_id=new_dept_id,
            payload={
                "old_dept_id": old_dept_id,
                "new_dept_id": new_dept_id,
                "issue_key": issue_key,
                "error": "new_dept_unresolved",
            },
            now_fn=now_fn,
        )
        return {
            "status": "bad_request",
            "decision": "dept_handover_failed",
            "reason": "new_dept_unresolved",
            "issue_key": issue_key,
        }
    workflow_input = _workflow_input(
        issue_key=issue_key,
        project_key=project_key,
        dept_id=new_dept_id,
        dept=new_dept,
        env=env,
    )

    try:
        result: StartResult = await start_workflow_idempotent(
            workflow_client,
            _WORKFLOW_NAME,
            new_workflow_id,
            [workflow_input],
            task_queue=_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        await _emit_audit(
            audit_logger,
            action="workflow_dept_handover_failed",
            resource=f"webhook:jira/issue_assigned",
            result="error",
            dept_id=new_dept_id,
            payload={
                "old_dept_id": old_dept_id,
                "new_dept_id": new_dept_id,
                "issue_key": issue_key,
                "error": type(exc).__name__,
            },
            now_fn=now_fn,
        )
        raise

    # Step 3: Audit event.
    await _emit_audit(
        audit_logger,
        action="workflow_dept_handover",
        resource=f"workflow:{new_workflow_id}",
        result="ok",
        dept_id=new_dept_id,
        payload={
            "old_dept_id": old_dept_id,
            "new_dept_id": new_dept_id,
            "old_workflow_id": old_workflow_id,
            "new_workflow_id": result.execution_id,
            "issue_key": issue_key,
            "old_workflow_cancelled": cancel_success,
        },
        now_fn=now_fn,
    )

    # Step 4: Jira bot comment.
    if jira_commenter:
        comment_body = _DEPT_HANDOVER_COMMENT.format(
            old_dept=old_dept_id, new_dept=new_dept_id
        )
        try:
            await jira_commenter.post_comment(new_dept_id, issue_key, comment_body)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dept_handover: failed to post Jira comment for %s: %s",
                issue_key,
                exc,
            )

    return {
        "status": "accepted",
        "decision": "dept_handover",
        "old_dept_id": old_dept_id,
        "new_dept_id": new_dept_id,
        "old_workflow_id": old_workflow_id,
        "new_workflow_id": result.execution_id,
        "old_workflow_cancelled": cancel_success,
        "issue_key": issue_key,
    }


async def _emit_audit(
    audit_logger: AuditLogger,
    *,
    action: str,
    resource: str,
    result: str,
    dept_id: str | None,
    payload: dict[str, Any] | None,
    now_fn: Callable[[], datetime],
) -> None:
    """Best-effort audit write."""
    try:
        await audit_logger.write(
            AuditEvent(
                actor_id=_AUDIT_ACTOR_ID,
                actor_role="system",
                dept_id=dept_id,
                action=action,
                resource=resource,
                result=result,
                timestamp=now_fn(),
                payload=payload,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit_write_failed action=%s: %s", action, exc)
