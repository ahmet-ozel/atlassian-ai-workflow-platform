"""Jira assignee activity for AgentRunnerWorkflow."""

from __future__ import annotations

import json
import logging

from temporalio import activity

from .mcp_tool import MCPToolError, call_mcp_tool, result_data

__all__ = ["AssigneeSetError", "set_assignee_to_bot"]

_LOG = logging.getLogger(__name__)


class AssigneeSetError(RuntimeError):
    """Raised when the Jira assignee update fails."""

    def __init__(self, *, issue_key: str, account_id: str, reason: str) -> None:
        self.issue_key = issue_key
        self.account_id = account_id
        self.reason = reason
        super().__init__(
            f"failed to set assignee for issue={issue_key!r} "
            f"to account_id={account_id!r}: {reason}"
        )


def _account_id_from_profile(profile: object) -> str | None:
    if not isinstance(profile, dict):
        return None
    user = profile.get("user") if isinstance(profile.get("user"), dict) else profile
    if not isinstance(user, dict):
        return None
    for key in ("accountId", "account_id", "key", "name"):
        value = user.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def _resolve_current_jira_account_id(dept_id: str, issue_key: str) -> str:
    """Resolve the authenticated Jira bot account id from /myself."""

    try:
        result = await call_mcp_tool(
            "jira_get_myself",
            {},
            dept_id=dept_id,
            service="jira",
        )
    except MCPToolError as exc:
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id="",
            reason=f"bot_account_probe_failed_{exc.status_code}",
        ) from exc

    account_id = _account_id_from_profile(result_data(result))
    if not account_id:
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id="",
            reason="bot_account_id_missing",
        )
    return account_id


async def _assign_via_mcp(
    *,
    dept_id: str,
    issue_key: str,
    account_id: str,
) -> None:
    """Set issue assignee through the mounted Jira MCP update tool."""

    try:
        await call_mcp_tool(
            "jira_update_issue",
            {
                "issue_key": issue_key,
                "fields": json.dumps({"assignee": account_id}),
            },
            dept_id=dept_id,
            service="jira",
        )
    except MCPToolError as exc:
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id=account_id,
            reason=f"mcp_{exc.status_code}",
        ) from exc


@activity.defn(name="set_assignee_to_bot")
async def set_assignee_to_bot(
    issue_key: str,
    dept_bot_account_id: str,
    dept_id: str | None = None,
) -> None:
    """Set the issue assignee to the department bot.

    Current workflows pass ``department_id`` as the second argument. In
    that case this activity resolves the real Jira accountId from the
    bot credentials via ``jira_get_myself`` before updating the issue.
    Legacy callers may still pass the accountId explicitly and provide
    ``dept_id`` as the third argument.
    """

    if not issue_key:
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id=dept_bot_account_id,
            reason="empty_issue_key",
        )
    if not dept_bot_account_id or not isinstance(dept_bot_account_id, str):
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id=str(dept_bot_account_id),
            reason="empty_account_id",
        )

    # The canonical workflow call is ``[issue_key, department_id]``.
    # Older callers that pass an explicit accountId must also pass
    # ``dept_id`` as the third argument, otherwise the accountId cannot
    # be safely distinguished from a department identifier.
    resolved_dept = dept_id or dept_bot_account_id

    account_id = dept_bot_account_id
    if account_id == resolved_dept:
        account_id = await _resolve_current_jira_account_id(
            resolved_dept,
            issue_key,
        )

    try:
        await _assign_via_mcp(
            dept_id=resolved_dept,
            issue_key=issue_key,
            account_id=account_id,
        )
    except AssigneeSetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AssigneeSetError(
            issue_key=issue_key,
            account_id=account_id,
            reason=f"unexpected_{type(exc).__name__}",
        ) from exc

    _LOG.info(
        "jira.assignee_set issue=%s account_id=%s dept=%s",
        issue_key,
        account_id,
        resolved_dept,
    )
