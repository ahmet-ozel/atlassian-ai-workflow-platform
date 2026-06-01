"""Adapters for sync LLM providers used by the assistant chat stream."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any, AsyncIterator, Mapping, Sequence

from messages import Message
from llm_orchestrator.orchestrator import ProviderChunk


class StreamingProviderAdapter:
    """Expose ``stream(...)`` for providers that only implement ``complete``."""

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def downtime(self) -> int:
        fn = getattr(self._provider, "downtime", None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001
                return 0
        return 0

    async def stream(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
    ) -> AsyncIterator[ProviderChunk]:
        heuristic_call = _heuristic_tool_call(history, tools)
        if heuristic_call is not None:
            yield ProviderChunk(
                kind="tool_call",
                call=heuristic_call,
                token_count=1,
            )
            return

        prompt = _messages_to_prompt(system, history, tools)
        text = await asyncio.to_thread(self._provider.complete, prompt)
        tool_call = _extract_tool_call(text, tools)
        if tool_call is not None:
            yield ProviderChunk(
                kind="tool_call",
                call=tool_call,
                token_count=_token_estimate(text),
            )
            return
        yield ProviderChunk(kind="token", text=str(text), token_count=_token_estimate(text))
        yield ProviderChunk(kind="final", is_final=True)


def _messages_to_prompt(
    system: str,
    history: Sequence[Message],
    tools: Sequence[Any],
) -> str:
    parts = [f"SYSTEM:\n{system.strip()}", ""]
    tool_names = [_tool_name(tool) for tool in tools]
    tool_names = [name for name in tool_names if name]
    if tool_names:
        parts.append(
            "AVAILABLE_TOOLS:\n"
            + "\n".join(f"- {name}" for name in tool_names)
            + "\n\n"
            "When current information from a tool is needed, respond only "
            "with JSON in this form: "
            '{"tool_call":{"name":"tool_name","arguments":{}}}. '
            "For latest Jira issues in a project, prefer "
            'jira_search_issues with {"jql":"project = KEY ORDER BY '
            'created DESC","fields":"key,summary,status,assignee,updated",'
            '"limit":1}.'
        )
        parts.append("")
    for message in history:
        role = getattr(message, "role", "user")
        text = getattr(message, "text", "")
        heading = "TOOL_RESULT" if role == "tool" else role.upper()
        parts.append(f"{heading}:\n{text}")
        if role == "tool":
            parts.append(
                "Use the TOOL_RESULT above to answer the user's latest "
                "question. Do not say you lack access when the tool result "
                "contains data."
            )
    parts.append("ASSISTANT:")
    return "\n\n".join(parts)


def _token_estimate(text: Any) -> int:
    value = str(text or "")
    return max(1, len(value.split()))


def _extract_tool_call(text: Any, tools: Sequence[Any]) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    allowed = {_tool_name(tool) for tool in tools if _tool_name(tool)}
    candidates = [raw]
    start = raw.find("{")
    end = raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, Mapping):
            continue
        call = payload.get("tool_call") or payload.get("call")
        if not isinstance(call, Mapping):
            continue
        name = str(call.get("name") or call.get("tool_name") or "")
        if not name or (allowed and name not in allowed):
            continue
        args = call.get("arguments") or call.get("args") or {}
        return {
            "tool_name": name,
            "arguments": dict(args) if isinstance(args, Mapping) else {},
            "intent": payload.get("intent"),
        }
    return None


def _heuristic_tool_call(
    history: Sequence[Message],
    tools: Sequence[Any],
) -> dict[str, Any] | None:
    """Return a read-only tool call for common current-data questions."""

    if not tools or _last_role(history) == "tool":
        return None
    last_user_raw = _last_user_text(history)
    last_user = last_user_raw.lower()
    last_user_norm = _fold_text(last_user_raw)
    if not last_user:
        return None
    allowed = {_tool_name(tool) for tool in tools if _tool_name(tool)}

    asks_for_jira = any(
        marker in last_user
        for marker in ("jira", "issue", "issues", "task", "tasks")
    ) or any(
        marker in last_user_norm
        for marker in ("jira", "issue", "issues", "task", "tasks", "gorev")
    )
    asks_for_assigned = any(
        marker in last_user
        for marker in ("assigned to me", "my ", "bana", "atan", "assignee")
    ) or any(
        marker in last_user_norm
        for marker in ("assigned to me", "my ", "bana", "atan", "assignee")
    )
    asks_for_open = any(
        marker in last_user
        for marker in ("open", "unresolved", "acik", "aktif")
    ) or any(
        marker in last_user_norm
        for marker in ("open", "unresolved", "acik", "aktif")
    )
    asks_to_list = any(
        marker in last_user
        for marker in ("list", "liste", "show", "get", "cek", "fetch")
    ) or any(
        marker in last_user_norm
        for marker in ("list", "liste", "show", "get", "cek", "fetch")
    )
    if asks_for_jira and asks_for_assigned and asks_for_open and asks_to_list:
        if "jira_search" in allowed:
            return {
                "tool_name": "jira_search",
                "arguments": {
                    "jql": (
                        "assignee = currentUser() AND resolution = Unresolved "
                        "ORDER BY updated DESC"
                    ),
                    "fields": "key,summary,status,assignee,updated",
                    "limit": 10,
                },
            }
        if "jira_search_issues" in allowed:
            return {
                "tool_name": "jira_search_issues",
                "arguments": {
                    "jql": (
                        "assignee = currentUser() AND resolution = Unresolved "
                        "ORDER BY updated DESC"
                    )
                },
            }
    project_key = _extract_jira_project_key(last_user_raw)
    asks_latest = any(
        marker in last_user
        for marker in ("latest", "last", "recent", "son", "en son", "guncel", "güncel")
    )
    asks_project_lookup = asks_for_jira and bool(project_key) and (
        asks_to_list
        or asks_latest
        or any(marker in last_user for marker in ("ara", "getir", "cek", "çek"))
    )
    if asks_project_lookup and project_key:
        limit = _extract_requested_limit(last_user)
        fields = "key,summary,status,assignee,updated"
        if "jira_search_issues" in allowed:
            return {
                "tool_name": "jira_search_issues",
                "arguments": {
                    "jql": f"project = {project_key} ORDER BY created DESC",
                    "fields": fields,
                    "limit": limit,
                },
            }
        if "jira_search" in allowed:
            return {
                "tool_name": "jira_search",
                "arguments": {
                    "jql": f"project = {project_key} ORDER BY created DESC",
                    "fields": fields,
                    "limit": limit,
                },
            }
        if "jira_get_project_issues" in allowed:
            return {
                "tool_name": "jira_get_project_issues",
                "arguments": {"project_key": project_key, "limit": limit},
            }
    asks_for_confluence = "confluence" in last_user_norm
    asks_to_search = any(
        marker in last_user_norm
        for marker in ("ara", "search", "bul", "find")
    )
    if asks_for_confluence and asks_to_search and "confluence_search" in allowed:
        return {
            "tool_name": "confluence_search",
            "arguments": {
                "query": _extract_search_query(last_user_raw) or last_user_raw,
                "limit": _extract_requested_limit(last_user_norm),
            },
        }
    asks_for_bitbucket = "bitbucket" in last_user_norm
    asks_for_repo = any(
        marker in last_user_norm
        for marker in ("repo", "repository", "repositories")
    )
    if asks_for_bitbucket and asks_for_repo and asks_to_list:
        limit = _extract_requested_limit(last_user_norm)
        if "bitbucket_list_repos" in allowed:
            return {
                "tool_name": "bitbucket_list_repos",
                "arguments": {"limit": limit},
            }
        if "bitbucket_search_repos" in allowed:
            return {
                "tool_name": "bitbucket_search_repos",
                "arguments": {
                    "query": _extract_search_query(last_user_raw) or "",
                    "limit": limit,
                },
            }
    return None


def _fold_text(text: str) -> str:
    """Lowercase text and strip accents for route-keyword matching."""

    turkish_folded = text.translate(str.maketrans({"ı": "i", "İ": "I"}))
    return (
        unicodedata.normalize("NFKD", turkish_folded)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )


def _last_role(history: Sequence[Message]) -> str:
    if not history:
        return ""
    return str(getattr(history[-1], "role", "") or "")


def _last_user_text(history: Sequence[Message]) -> str:
    for message in reversed(history):
        if str(getattr(message, "role", "") or "") == "user":
            return str(getattr(message, "text", "") or "")
    return ""


def _tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, Mapping):
        return str(tool.get("name") or tool.get("tool_name") or "")
    return str(getattr(tool, "name", "") or getattr(tool, "tool_name", ""))


def _extract_jira_project_key(text: str) -> str | None:
    ignored = {"JIRA", "TASK", "ISSUE", "OPEN", "SON", "GETIR", "GETİR"}
    patterns = (
        r"\bproject\s*[=:]?\s*([A-Z][A-Z0-9_]{1,9})\b",
        r"\b([A-Z][A-Z0-9_]{1,9})\s+proje(?:si|sinde|sindeki|de|den|leri|ler)?\b",
        r"\b([A-Z][A-Z0-9_]{1,9})['’](?:da|de|daki|deki|dan|den)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        key = match.group(1).upper()
        if key not in ignored:
            return key
    return None


def _extract_requested_limit(text: str) -> int:
    for pattern in (
        r"(?:ilk|first)\s+(\d+)",
        r"(?:en son|son|latest|last|recent)\s+(\d+)",
        r"(\d+)\s+(?:task|tasks|issue|issues|gorev|görev|repo|repos|repository|repositories)",
    ):
        match = re.search(pattern, text)
        if match:
            return max(1, min(int(match.group(1)), 50))
    if any(marker in text for marker in ("son task", "latest task", "last task")):
        return 1
    return 10


def _extract_search_query(text: str) -> str | None:
    quoted = re.search(r"[\"`“”‘’]([^\"`“”‘’]{1,80})[\"`“”‘’]", text)
    if quoted:
        return quoted.group(1).strip()
    folded = _fold_text(text)
    match = re.search(r"\b([a-z0-9_.-]{2,80})\s+kelimesi(?:ni|ne|yle|nde)?\b", folded)
    if match:
        return match.group(1)
    return None
