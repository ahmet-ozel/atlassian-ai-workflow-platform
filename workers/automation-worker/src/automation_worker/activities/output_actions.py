"""Output Action Executor activity for the automation-worker.

Implements the ``execute_output_actions`` Temporal activity that
sequentially executes LLM-generated output actions (Jira comment,
Jira attachment, Bitbucket PR, Confluence page, Jira transition).

Each action is dispatched to the MCP Server via an authenticated
HTTP client. Failures are logged and execution continues to the
next action. After all actions complete, any failures are reported
back as a summary Jira comment on the issue.

Design reference: design.md §3 (Output Action Executor)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from temporalio import activity

from db_shared.enums import ActionType

__all__ = (
    "execute_output_actions",
    "OutputAction",
    "ActionResult",
    "ExecutionBatchInput",
    "ExecutionBatchResult",
    "set_mcp_caller",
    "get_mcp_caller",
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of actions allowed in a single execution batch.
MAX_ACTIONS_PER_BATCH: int = 20

#: Timeout in seconds for each individual action's MCP call.
ACTION_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputAction:
    """A single output action to be executed.

 Attributes:
 type: The action type (from ActionType enum).
 params: Action-specific parameters dictionary.
 index: Execution order index (0-based).
 """

    type: ActionType
    params: dict[str, Any]
    index: int


@dataclass(frozen=True)
class ActionResult:
    """Result of executing a single output action.

 Attributes:
 action_type: The type of action that was executed.
 index: The action's position in the execution batch.
 status: Outcome — success, failed, skipped, or timeout.
 error: Error message if the action failed (None on success).
 timestamp: When the action completed execution.
 """

    action_type: ActionType
    index: int
    status: Literal["success", "failed", "skipped", "timeout"]
    error: str | None = None
    timestamp: datetime | None = None


@dataclass(frozen=True)
class ExecutionBatchInput:
    """Input for the execute_output_actions activity.

 Attributes:
 actions: List of output actions to execute (max 20).
 issue_key: The Jira issue key for context and error reporting.
 dept_id: Department identifier for credential resolution.
 workflow_id: Parent workflow identifier for tracing.
 """

    actions: list[OutputAction | dict[str, Any]]
    issue_key: str
    dept_id: str
    workflow_id: str


@dataclass(frozen=True)
class ExecutionBatchResult:
    """Result of executing a batch of output actions.

 Attributes:
 results: Individual result for each action in execution order.
 all_succeeded: True if every action completed successfully.
 failed_actions: Subset of results where status != "success".
 """

    results: list[ActionResult]
    all_succeeded: bool
    failed_actions: list[ActionResult]


# ---------------------------------------------------------------------------
# MCP Caller Protocol (dependency injection)
# ---------------------------------------------------------------------------


@runtime_checkable
class MCPCallerProtocol(Protocol):
    """Protocol for making authenticated MCP Server calls.

 Production wires this to an HTTP client that calls the MCP Server
 endpoints. Tests inject a fake that records calls and returns
 predetermined responses.
 """

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        dept_id: str,
        timeout: float = ACTION_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Call an MCP Server tool with the given parameters.

 Args:
 tool_name: The MCP tool name to invoke.
 params: Tool-specific parameters.
 dept_id: Department ID for credential resolution.
 timeout: Maximum seconds to wait for a response.

 Returns:
 The tool's response payload.

 Raises:
 asyncio.TimeoutError: If the call exceeds timeout.
 Exception: On any other failure.
 """
        ...


_mcp_caller: MCPCallerProtocol | None = None


def set_mcp_caller(caller: MCPCallerProtocol) -> None:
    """Register the MCP caller used by the output actions activity.

 Called once at worker boot after the HTTP client and credential
 resolver are constructed. Unit tests call this with an in-memory
 fake before invoking the activity directly.
 """
    global _mcp_caller  # noqa: PLW0603
    _mcp_caller = caller


def get_mcp_caller() -> MCPCallerProtocol:
    """Resolve the registered MCP caller or fail loudly."""
    if _mcp_caller is None:
        raise RuntimeError(
            "output_actions activity: MCP caller not initialised; "
            "call set_mcp_caller during worker startup."
        )
    return _mcp_caller


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


async def _handle_jira_comment(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a jira_comment action via MCP Server.

 """
    return await caller.call_tool(
        "jira_add_comment",
        _drop_control_params(params, keep_issue_key=True),
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


async def _handle_jira_attachment(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a jira_attachment action via MCP Server.

 Two upload modes are supported:

 * **MinIO pipeline** — when ``params`` carries both ``bucket`` and
 ``key`` the action is dispatched to the
 ``upload_artifact_to_jira`` activity (registered on the
 ``agent-runner-tq`` queue). That activity downloads the artifact
 from MinIO, stages it to a tempfile, and forwards the upload
 through the ``jira_add_attachment`` MCP tool. This is the
 preferred path for agent-runner-produced artifacts (markdown
 reports, generated PDFs, …).
 * **Local file fallback** — when ``bucket``/``key`` are absent the
 action keeps the legacy contract and is forwarded directly to the
 ``jira_add_attachment`` MCP tool with whatever ``file_path`` the
 caller provided. This preserves backward compatibility with
 existing description-parser payloads that reference a local
 file path.

 """

    normalized = _drop_control_params(params, keep_issue_key=True)
    if "bucket" in normalized and "key" in normalized:
        # MinIO pipeline — delegate to the agent-runner activity. The
        # activity handles the download → tempfile → MCP call → cleanup
        # sequence end-to-end so this dispatcher is intentionally thin.
        return await caller.call_tool(
            "upload_artifact_to_jira",
            normalized,
            dept_id=dept_id,
            timeout=ACTION_TIMEOUT_SECONDS,
        )

    # Legacy local-path contract. Forwarded verbatim to the MCP tool so
    # description-parser payloads that still reference a local
    # ``file_path`` continue to work without modification.
    return await caller.call_tool(
        "jira_add_attachment",
        normalized,
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


async def _handle_bitbucket_pr(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a bitbucket_pr action via MCP Server.

 Creates a draft PR from source to target branch.
 """
    normalized = _drop_control_params(params, keep_issue_key=False)
    if "from_branch" not in normalized and "source" in normalized:
        normalized["from_branch"] = normalized["source"]
    if "to_branch" not in normalized and "target" in normalized:
        normalized["to_branch"] = normalized["target"]
    return await caller.call_tool(
        "bitbucket_create_pr",
        normalized,
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


async def _handle_bitbucket_commit(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a single-file Bitbucket commit via MCP Server.

 The mounted Bitbucket MCP exposes file writes as
 ``bitbucket_put_file_content``. This output action provides a
 friendlier task-level alias (``bitbucket_commit``) for publishing
 generated reports or small code/result files to a branch.
 """

    normalized = _drop_control_params(params, keep_issue_key=False)
    if "file_path" not in normalized and "path" in normalized:
        normalized["file_path"] = normalized["path"]
    if "message" not in normalized and "commit_message" in normalized:
        normalized["message"] = normalized["commit_message"]
    if "branch" not in normalized and "target_branch" in normalized:
        normalized["branch"] = normalized["target_branch"]
    return await caller.call_tool(
        "bitbucket_put_file_content",
        normalized,
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


async def _handle_confluence_page(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a confluence_page action via MCP Server.

 If page_id is present in params, updates the existing page.
 If page_id is absent, creates a new page in the specified space.
 """
    normalized = _drop_control_params(params, keep_issue_key=False)
    if normalized.get("page_id"):
        tool_name = "confluence_update_page"
    else:
        tool_name = "confluence_create_page"
    return await caller.call_tool(
        tool_name,
        normalized,
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


async def _handle_jira_transition(
    params: dict[str, Any],
    caller: MCPCallerProtocol,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a jira_transition action via MCP Server.

 Uses the department's status_mapping configuration to resolve
 the target Jira status. If no mapping is found, the action is
 skipped.
 """
    return await caller.call_tool(
        "jira_transition_issue",
        _drop_control_params(params, keep_issue_key=True),
        dept_id=dept_id,
        timeout=ACTION_TIMEOUT_SECONDS,
    )


#: Maps ActionType to its handler function.
_ACTION_HANDLERS: dict[ActionType, Any] = {
    ActionType.JIRA_COMMENT: _handle_jira_comment,
    ActionType.JIRA_ATTACHMENT: _handle_jira_attachment,
    ActionType.BITBUCKET_COMMIT: _handle_bitbucket_commit,
    ActionType.BITBUCKET_PR: _handle_bitbucket_pr,
    ActionType.CONFLUENCE_PAGE: _handle_confluence_page,
    ActionType.JIRA_TRANSITION: _handle_jira_transition,
}


def _drop_control_params(
    params: dict[str, Any],
    *,
    keep_issue_key: bool,
) -> dict[str, Any]:
    """Remove workflow-only keys before calling MCP tools."""

    blocked = {"dept_id"}
    if not keep_issue_key:
        blocked.add("issue_key")
    return {key: value for key, value in params.items() if key not in blocked}


# ---------------------------------------------------------------------------
# Core Activity
# ---------------------------------------------------------------------------


def _coerce_action_type(raw: Any) -> ActionType | None:
    """Decode Temporal/JSON variants into an:class:`ActionType`."""

    if isinstance(raw, ActionType):
        return raw
    if isinstance(raw, str):
        candidate = raw
    elif isinstance(raw, dict):
        candidate = raw.get("value") or raw.get("type") or raw.get("name")
    elif isinstance(raw, (list, tuple)):
        if raw and all(isinstance(item, str) for item in raw):
            joined = "".join(raw)
            coerced = _coerce_action_type(joined)
            if coerced is not None:
                return coerced
        for item in raw:
            coerced = _coerce_action_type(item)
            if coerced is not None:
                return coerced
        return None
    else:
        return None

    aliases = {
        "bitbucket_create_pr": ActionType.BITBUCKET_PR.value,
        "confluence_create_page": ActionType.CONFLUENCE_PAGE.value,
        "confluence_update_page": ActionType.CONFLUENCE_PAGE.value,
    }
    value = aliases.get(str(candidate), str(candidate))
    try:
        return ActionType(value)
    except ValueError:
        return None


def _normalise_action(raw: Any, fallback_index: int) -> OutputAction | None:
    """Accept dataclass or JSON-decoded action payloads."""

    if isinstance(raw, OutputAction):
        action_type = _coerce_action_type(raw.type)
        if action_type is None:
            return None
        return OutputAction(
            type=action_type,
            params=dict(raw.params),
            index=int(raw.index),
        )
    if not isinstance(raw, dict):
        return None
    action_type = _coerce_action_type(raw.get("type"))
    if action_type is None:
        return None
    params = raw.get("params")
    index = raw.get("index", fallback_index)
    try:
        parsed_index = int(index)
    except (TypeError, ValueError):
        parsed_index = fallback_index
    return OutputAction(
        type=action_type,
        params=dict(params) if isinstance(params, dict) else {},
        index=parsed_index,
    )


async def _execute_single_action(
    action: OutputAction,
    caller: MCPCallerProtocol,
    dept_id: str,
) -> ActionResult:
    """Execute a single output action with timeout handling.

 Returns an ActionResult regardless of outcome — never raises.
 """
    handler = _ACTION_HANDLERS.get(action.type)
    if handler is None:
        activity.logger.warning(
            "output_actions: unknown action type %s at index %d — skipping",
            action.type,
            action.index,
        )
        return ActionResult(
            action_type=action.type,
            index=action.index,
            status="skipped",
            error=f"Unknown action type: {action.type}",
            timestamp=datetime.now(timezone.utc),
        )

    try:
        await asyncio.wait_for(
            handler(action.params, caller, dept_id),
            timeout=ACTION_TIMEOUT_SECONDS,
        )
        return ActionResult(
            action_type=action.type,
            index=action.index,
            status="success",
            error=None,
            timestamp=datetime.now(timezone.utc),
        )
    except asyncio.TimeoutError:
        error_msg = (
            f"Action {action.type.value} at index {action.index} "
            f"timed out after {ACTION_TIMEOUT_SECONDS}s"
        )
        activity.logger.warning("output_actions: %s", error_msg)
        return ActionResult(
            action_type=action.type,
            index=action.index,
            status="timeout",
            error=error_msg,
            timestamp=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001
        error_msg = (
            f"Action {action.type.value} at index {action.index} "
            f"failed: {exc}"
        )
        activity.logger.warning("output_actions: %s", error_msg)
        return ActionResult(
            action_type=action.type,
            index=action.index,
            status="failed",
            error=error_msg,
            timestamp=datetime.now(timezone.utc),
        )


def _build_failure_comment(failed_actions: list[ActionResult]) -> str:
    """Build a Jira comment summarizing failed actions.

 """
    lines = ["⚠️ Aşağıdaki output action'lar başarısız oldu:\n"]
    for result in failed_actions:
        status_label = "timeout" if result.status == "timeout" else "hata"
        lines.append(
            f"• [{result.index}] {result.action_type.value} — "
            f"{status_label}: {result.error or 'bilinmeyen hata'}"
        )
    return "\n".join(lines)


@activity.defn(name="execute_output_actions")
async def execute_output_actions(input: ExecutionBatchInput) -> ExecutionBatchResult:
    """Execute a batch of output actions sequentially.

 Actions are executed in strict index order (0, 1, 2,...). Each
 action has a 30-second timeout. Failures are logged and execution
 continues to the next action. After all actions complete, if any
 failed, a summary comment is posted to the Jira issue.

 If the actions list is empty or None, returns immediately with a
 successful result.

 """
    # Handle empty/null actions list — 
    if not input.actions:
        activity.logger.info(
            "output_actions: empty action list for workflow %s, "
            "issue %s — completing successfully",
            input.workflow_id,
            input.issue_key,
        )
        return ExecutionBatchResult(
            results=[],
            all_succeeded=True,
            failed_actions=[],
        )

    # Enforce max batch size — 
    normalised_actions: list[OutputAction] = []
    for fallback_index, raw_action in enumerate(input.actions):
        action = _normalise_action(raw_action, fallback_index)
        if action is None:
            activity.logger.warning(
                "output_actions: malformed action at position %d skipped: %r",
                fallback_index,
                raw_action,
            )
            continue
        normalised_actions.append(action)

    actions_to_execute = normalised_actions[:MAX_ACTIONS_PER_BATCH]
    if len(normalised_actions) > MAX_ACTIONS_PER_BATCH:
        activity.logger.warning(
            "output_actions: action list for workflow %s has %d items, "
            "truncating to %d",
            input.workflow_id,
            len(normalised_actions),
            MAX_ACTIONS_PER_BATCH,
        )

    caller = get_mcp_caller()

    # Sort by index to ensure strict sequential order — 
    sorted_actions = sorted(actions_to_execute, key=lambda a: a.index)

    results: list[ActionResult] = []

    # Execute actions sequentially — 
    for action in sorted_actions:
        activity.logger.info(
            "output_actions: executing action %s at index %d "
            "(workflow=%s, issue=%s)",
            action.type.value,
            action.index,
            input.workflow_id,
            input.issue_key,
        )
        result = await _execute_single_action(action, caller, input.dept_id)
        results.append(result)

    # Collect failed actions
    failed_actions = [r for r in results if r.status != "success"]
    all_succeeded = len(failed_actions) == 0

    # Post failure summary to Jira if any actions failed — 
    if failed_actions:
        failure_comment = _build_failure_comment(failed_actions)
        try:
            await asyncio.wait_for(
                caller.call_tool(
                    "jira_add_comment",
                    {
                        "issue_key": input.issue_key,
                        "body": failure_comment,
                    },
                    dept_id=input.dept_id,
                    timeout=ACTION_TIMEOUT_SECONDS,
                ),
                timeout=ACTION_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            activity.logger.warning(
                "output_actions: failed to post failure summary comment "
                "to %s: %s",
                input.issue_key,
                exc,
            )

    activity.logger.info(
        "output_actions: batch complete for workflow %s, issue %s — "
        "%d/%d succeeded",
        input.workflow_id,
        input.issue_key,
        len(results) - len(failed_actions),
        len(results),
    )

    return ExecutionBatchResult(
        results=results,
        all_succeeded=all_succeeded,
        failed_actions=failed_actions,
    )
