"""Jira MCP activities for AgentRunnerWorkflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from temporalio import activity

from .mcp_tool import MCPToolError, call_mcp_tool, result_text


@dataclass(frozen=True)
class IssueData:
    key: str
    summary: str
    description: str
    issue_type: str
    status: str
    assignee_account_id: str | None
    project_key: str
    labels: list[str]
    priority: str | None


class JiraActivityError(RuntimeError):
    """Raised when a Jira MCP tool call fails."""

    def __init__(self, tool_name: str, issue_key: str, cause: str) -> None:
        super().__init__(f"Jira activity [{tool_name}] for {issue_key}: {cause}")
        self.tool_name = tool_name
        self.issue_key = issue_key


def _parse_jsonish(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parse_issue_data(raw: Any) -> IssueData:
    data = _parse_jsonish(raw)
    fields = data.get("fields", data)
    if not isinstance(fields, dict):
        fields = {}

    project = fields.get("project", {})
    assignee = fields.get("assignee") or {}
    issue_type = fields.get("issuetype", fields.get("issue_type", {}))
    status = fields.get("status", {})
    priority = fields.get("priority")

    return IssueData(
        key=str(data.get("key") or fields.get("key") or ""),
        summary=str(fields.get("summary") or ""),
        description=str(fields.get("description") or ""),
        issue_type=(
            str(issue_type.get("name") or "")
            if isinstance(issue_type, dict)
            else str(issue_type or "")
        ),
        status=(
            str(status.get("name") or "")
            if isinstance(status, dict)
            else str(status or "")
        ),
        assignee_account_id=(
            assignee.get("accountId") or assignee.get("account_id")
            if isinstance(assignee, dict)
            else None
        ),
        project_key=(
            str(project.get("key") or "")
            if isinstance(project, dict)
            else str(project or "")
        ),
        labels=list(fields.get("labels") or []),
        priority=(
            priority.get("name")
            if isinstance(priority, dict)
            else (str(priority) if priority else None)
        ),
    )


async def _jira_tool(tool_name: str, issue_key: str, args: dict[str, Any], dept_id: str) -> dict[str, Any]:
    try:
        return await call_mcp_tool(
            tool_name,
            args,
            dept_id=dept_id,
            service="jira",
        )
    except MCPToolError as exc:
        raise JiraActivityError(
            tool_name,
            issue_key,
            f"MCP error {exc.status_code}: {exc.detail}",
        ) from exc


@activity.defn(name="jira_get_issue")
async def jira_get_issue(issue_key: str, dept_id: str) -> IssueData:
    activity.heartbeat(f"fetching issue {issue_key}")
    result = await _jira_tool(
        "jira_get_issue",
        issue_key,
        {"issue_key": issue_key},
        dept_id,
    )
    return _parse_issue_data(result_text(result))


@activity.defn(name="jira_add_comment")
async def jira_add_comment(issue_key: str, body: str, dept_id: str) -> None:
    activity.heartbeat(f"adding comment to {issue_key}")
    await _jira_tool(
        "jira_add_comment",
        issue_key,
        {"issue_key": issue_key, "body": body},
        dept_id,
    )


@activity.defn(name="jira_build_issue_link")
async def jira_build_issue_link(issue_key: str, dept_id: str) -> str:
    """Compose the canonical ``{site_url}/browse/{issue_key}`` deep link.

    Resolves the department's Jira ``url`` from the shared credential
    resolver (the same ``org``-scoped lookup the MCP activities use) and
    joins it with the issue key. Best-effort by contract: callers treat a
    failure or empty result as a degraded provenance footer rather than a
    workflow error, so any resolver/credential problem returns ``""``.
    """

    activity.heartbeat(f"building issue link for {issue_key}")
    from . import get_credential_resolver  # noqa: PLC0415

    try:
        creds = await get_credential_resolver().get(
            dept_id, "jira", scope="org"
        )
    except Exception:  # noqa: BLE001 - best-effort
        return ""

    if isinstance(creds, dict):
        url = str(creds.get("url") or creds.get("base_url") or "").strip()
    else:
        url = str(
            getattr(creds, "url", "") or getattr(creds, "base_url", "")
        ).strip()
    if not url:
        return ""
    # The Jira site root is the credential URL; ``/wiki`` (Confluence)
    # is never part of a Jira browse link, so trim it defensively.
    root = url.rstrip("/")
    if root.endswith("/wiki"):
        root = root[: -len("/wiki")]
    return f"{root}/browse/{issue_key}"


@activity.defn(name="jira_transition_issue")
async def jira_transition_issue(issue_key: str, target_status: str, dept_id: str) -> None:
    activity.heartbeat(f"transitioning {issue_key} to {target_status}")
    await _jira_tool(
        "jira_transition_issue",
        issue_key,
        {"issue_key": issue_key, "status": target_status},
        dept_id,
    )


@dataclass(frozen=True)
class EpicChild:
    """A single child issue of an Epic.

    Attributes
    ----------
    key:
        The child issue key (e.g. ``PROJ-42``).
    summary:
        Human-readable summary/title of the child issue.
    status:
        Current Jira status name; empty when unavailable.
    """

    key: str
    summary: str
    status: str = ""


def _parse_epic_children(raw: Any) -> list[EpicChild]:
    data = _parse_jsonish(raw)
    issues = data.get("issues")
    if not isinstance(issues, list):
        return []
    children: list[EpicChild] = []
    for item in issues:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        summary = str((fields or {}).get("summary") or item.get("summary") or "")
        status_raw = (fields or {}).get("status") or item.get("status")
        if isinstance(status_raw, dict):
            status = str(status_raw.get("name") or "")
        else:
            status = str(status_raw or "")
        children.append(EpicChild(key=key, summary=summary, status=status))
    return children


@activity.defn(name="jira_list_epic_children")
async def jira_list_epic_children(epic_key: str, dept_id: str) -> list[EpicChild]:
    """Return the child issues of an Epic via a ``parent = <epic>`` search.

    Jira Cloud links an Epic's children through the ``parent`` field, so a
    JQL ``parent = <epic_key>`` query enumerates them without depending on
    a deployment-specific ``subtasks`` payload shape. The result feeds the
    multi_step fan-out so each child can run as its own automation.
    """
    activity.heartbeat(f"listing children of epic {epic_key}")
    result = await _jira_tool(
        "jira_search",
        epic_key,
        {"jql": f"parent = {epic_key}", "limit": 50},
        dept_id,
    )
    return _parse_epic_children(result_text(result))
