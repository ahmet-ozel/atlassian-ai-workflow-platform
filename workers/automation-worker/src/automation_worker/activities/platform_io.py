"""Runtime I/O activities used by ``AutomationWorkflow``.

These activities keep the workflow body deterministic while still
letting it fetch Jira issue details, post comments, load department
configuration and call the configured LLM provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping

from temporalio import activity

from automation_worker.activities.task_analyzer import TaskAnalysisInput
from temporal_shared.messages import AutomationWorkflowInput

_LOG = logging.getLogger(__name__)

_mcp_caller: Any | None = None


def set_mcp_caller(caller: Any) -> None:
    """Register the MCP caller built at worker boot."""
    global _mcp_caller  # noqa: PLW0603
    _mcp_caller = caller


def get_mcp_caller() -> Any:
    if _mcp_caller is None:
        raise RuntimeError("platform_io: MCP caller is not initialised")
    return _mcp_caller


class ProviderLLMCaller:
    """Adapter from sync provider ``complete`` to analyzer protocol."""

    def __init__(self, primary: Any, fallback: Any | None = None) -> None:
        self._primary = primary
        self._fallback = fallback

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        del dept_id
        try:
            return await asyncio.to_thread(self._primary.complete, prompt)
        except Exception:
            if self._fallback is None:
                raise
            return await asyncio.to_thread(self._fallback.complete, prompt)


class MCPJiraCommenter:
    """Jira commenter protocol backed by stateless MCP calls."""

    def __init__(self, caller: Any) -> None:
        self._caller = caller

    async def add_comment(
        self, issue_key: str, body: str, *, dept_id: str
    ) -> None:
        await self._caller.call_tool(
            "jira_add_comment",
            {"issue_key": issue_key, "body": body},
            dept_id=dept_id,
        )


class MCPJiraTransitioner:
    """Jira transition protocol backed by stateless MCP calls."""

    def __init__(self, caller: Any) -> None:
        self._caller = caller

    async def transition_issue(
        self, issue_key: str, target_status: str, *, dept_id: str
    ) -> None:
        await self._caller.call_tool(
            "jira_transition_issue",
            {"issue_key": issue_key, "target_status": target_status},
            dept_id=dept_id,
        )


class SimpleRepoParser:
    """Low-risk repo parser used before falling back to user questions."""

    _repo_re = re.compile(
        r"(?im)^\s*(?:repo|repository|bitbucket_repo|target_repo)\s*:\s*(\S+)"
    )

    async def parse_repo_from_description(
        self, description: str, repo_mappings: list[dict[str, Any]]
    ) -> dict[str, Any]:
        match = self._repo_re.search(description or "")
        if match:
            return {"repo_url": match.group(1).strip(), "confidence": 0.95}
        repos = [
            str(m.get("bitbucket_repo", "")).strip()
            for m in repo_mappings
            if m.get("bitbucket_repo")
        ]
        if len(repos) == 1:
            return {"repo_url": repos[0], "confidence": 0.9}
        return {"repo_url": None, "confidence": 0.0}


def _platform_root() -> Path:
    here = Path(__file__).resolve()
    candidates: list[Path] = []
    for env_name in ("PLATFORM_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            candidates.append(Path(raw))
    candidates.append(Path.cwd())
    candidates.extend(here.parents)
    for candidate in candidates:
        if (candidate / "config" / "departments.json").is_file():
            return candidate
    return Path(os.environ.get("WORKSPACE_ROOT", "/app"))


def _load_department_config(dept_id: str) -> dict[str, Any]:
    path = _platform_root() / "config" / "departments.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("department config could not be loaded: %s", exc)
        return {}
    for dept in data.get("departments", []):
        if isinstance(dept, dict) and dept.get("id") == dept_id:
            return dict(dept)
    return {}


def _derive_capabilities(config: Mapping[str, Any]) -> list[str]:
    caps: set[str] = set()
    bot = config.get("bot") if isinstance(config.get("bot"), Mapping) else {}
    for service in ("jira", "bitbucket", "confluence"):
        entry = bot.get(service) if isinstance(bot, Mapping) else None
        if isinstance(entry, Mapping) and entry.get("credential_ref"):
            caps.add(service)
    runner_available = any(
        os.environ.get(key, "").strip().lower()
        in {"1", "true", "yes", "on"}
        for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
    )
    if runner_available or os.environ.get("SSH_HOST"):
        caps.add("execution")
    if config.get("web_search_enabled") and os.environ.get(
        "FIRECRAWL_ENABLED", "false"
    ) == "true":
        caps.add("web_search")
    return sorted(caps)


def _jsonish_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if isinstance(parsed, Mapping):
        return dict(parsed)
    return None


def _normalise_mcp_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = _jsonish_mapping(payload.get("result"))
    if nested is not None:
        return nested
    if isinstance(payload.get("result"), str) and len(payload) == 1:
        return {"description": str(payload["result"])}
    return dict(payload)


def _unwrap_mcp_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return _normalise_mcp_mapping(structured)
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        text = first.get("text") if isinstance(first, Mapping) else None
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"description": text}
            if isinstance(parsed, Mapping):
                return _normalise_mcp_mapping(parsed)
    nested = result.get("result")
    if isinstance(nested, Mapping):
        return _normalise_mcp_mapping(nested)
    return _normalise_mcp_mapping(result)


def _adf_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        parts = [_adf_to_text(item) for item in value.get("content", [])]
        return "\n".join(part for part in parts if part)
    if isinstance(value, list):
        return "\n".join(_adf_to_text(item) for item in value)
    return "" if value is None else str(value)


def _issue_payload_to_input(
    inp: AutomationWorkflowInput,
    issue_payload: Mapping[str, Any],
    comment_body: str,
) -> TaskAnalysisInput:
    issue = issue_payload.get("issue") if "issue" in issue_payload else issue_payload
    if not isinstance(issue, Mapping):
        issue = {}
    fields = issue.get("fields") if isinstance(issue.get("fields"), Mapping) else {}
    summary = str(fields.get("summary") or issue.get("summary") or inp.issue_key)
    description = _adf_to_text(fields.get("description") or issue.get("description"))
    if not description and inp.raw_event is not None:
        description = _adf_to_text(inp.raw_event.body_text)
    if comment_body.strip():
        description = f"{description}\n\nEk kullanıcı cevabı:\n{comment_body.strip()}"
    labels = fields.get("labels", [])
    if not isinstance(labels, list):
        labels = []
    custom_fields: dict[str, str | None] = {}
    for key, value in fields.items():
        if key.startswith("customfield_") or key.lower() in {"repository", "repo"}:
            custom_fields[key] = None if value is None else _adf_to_text(value)
    config = _load_department_config(inp.department_id)
    repo_mappings = config.get("repo_mappings") or []
    dept_config: dict[str, Any] = {
        **config,
        "available_capabilities": list(inp.available_capabilities)
        or _derive_capabilities(config),
        "available_repos": list(inp.available_repos)
        or [
            str(m.get("bitbucket_repo"))
            for m in repo_mappings
            if isinstance(m, Mapping) and m.get("bitbucket_repo")
        ],
        "available_spaces": list(inp.available_spaces)
        or list(config.get("confluence_space_keys") or []),
        "default_language": inp.default_language
        or str(config.get("default_language") or "tr"),
    }
    return TaskAnalysisInput(
        issue_key=inp.issue_key,
        title=summary,
        description=description,
        labels=[str(label) for label in labels],
        custom_fields=custom_fields,
        dept_id=inp.department_id,
        dept_config=dept_config,
        trace_id=inp.trace_id,
        issue_meta={"fields": dict(fields)},
    )


@activity.defn(name="prepare_task_analysis_input")
async def prepare_task_analysis_input(
    inp: AutomationWorkflowInput, comment_body: str
) -> TaskAnalysisInput:
    """Fetch the Jira issue and build the analyzer input."""
    payload: dict[str, Any] = {}
    try:
        result = await get_mcp_caller().call_tool(
            "jira_get_issue",
            {"issue_key": inp.issue_key},
            dept_id=inp.department_id,
            timeout=30.0,
        )
        payload = _unwrap_mcp_payload(result)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning("jira_get_issue failed for %s: %s", inp.issue_key, exc)
    return _issue_payload_to_input(inp, payload, comment_body)


@activity.defn(name="jira_add_comment")
async def jira_add_comment(issue_key: str, body: str, department_id: str) -> None:
    await get_mcp_caller().call_tool(
        "jira_add_comment",
        {"issue_key": issue_key, "body": body},
        dept_id=department_id,
    )


@activity.defn(name="jira_transition_issue")
async def jira_transition_issue(
    issue_key: str, target_status: str, department_id: str
) -> None:
    await get_mcp_caller().call_tool(
        "jira_transition_issue",
        {"issue_key": issue_key, "target_status": target_status},
        dept_id=department_id,
    )


@activity.defn(name="load_branch_pattern_rules")
async def load_branch_pattern_rules(department_id: str) -> list[dict[str, Any]]:
    return list(_load_department_config(department_id).get("branch_pattern_rules") or [])


@activity.defn(name="audit_write")
async def audit_write(event: dict[str, Any]) -> None:
    activity.logger.info("audit_write: %s", json.dumps(event, default=str))


@activity.defn(name="noop_test_post_result")
async def noop_test_post_result(
    issue_key: str, department_id: str, stdout: str, exit_code: int | None
) -> None:
    code = "unknown" if exit_code is None else str(exit_code)
    body = (
        "Noop test tamamlandi.\n\n"
        f"* Exit code: `{code}`\n"
        f"* Output: `{stdout or '(empty)'}`"
    )
    await jira_add_comment(issue_key, body, department_id)
