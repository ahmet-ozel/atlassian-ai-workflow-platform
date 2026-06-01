"""Unit tests for assistant-service LLM stream adapter routing."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_ROOT = _SERVICE_ROOT.parents[1]
for _path in (
    _ROOT / "libs" / "messages" / "src",
    _ROOT / "libs" / "llm-orchestrator" / "src",
    _SERVICE_ROOT,
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from messages import Message
from llm_orchestrator.orchestrator import LlmOrchestrator
from src.llm_stream_adapter import StreamingProviderAdapter


class _Provider:
    def __init__(self, text: str = "final answer") -> None:
        self.text = text
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.text


async def _collect(adapter: StreamingProviderAdapter, history: list[Message]) -> list[Any]:
    return await _collect_with_tools(adapter, history, [{"name": "jira_search"}])


async def _collect_with_tools(
    adapter: StreamingProviderAdapter,
    history: list[Message],
    tools: list[dict[str, str]],
) -> list[Any]:
    chunks: list[Any] = []
    async for chunk in adapter.stream(
        system="system",
        history=history,
        tools=tools,
    ):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_assigned_open_jira_task_question_routes_to_jira_search() -> None:
    provider = _Provider("I cannot access Jira")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect(
        adapter,
        [Message(role="user", text="List my open Jira tasks assigned to me.")],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "jira_search"
    assert "assignee = currentUser()" in chunks[0].call["arguments"]["jql"]
    assert chunks[0].call["arguments"]["limit"] == 10


@pytest.mark.asyncio
async def test_turkish_assigned_open_jira_task_routes_to_jira_search() -> None:
    provider = _Provider("I cannot access Jira")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect_with_tools(
        adapter,
        [
            Message(
                role="user",
                text=(
                    "Jira'da bana atanmış açık taskları listele. "
                    "Key, summary ve status bilgilerini kısa yaz."
                ),
            )
        ],
        [{"name": "jira_search"}, {"name": "jira_get_project_issues"}],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "jira_search"
    assert "assignee = currentUser()" in chunks[0].call["arguments"]["jql"]


@pytest.mark.asyncio
async def test_turkish_project_latest_task_routes_to_jira_search_issues() -> None:
    provider = _Provider("I cannot access Jira")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect_with_tools(
        adapter,
        [Message(role="user", text="Jira'da ABC projesindeki son 1 taskı getir.")],
        [{"name": "jira_search_issues"}, {"name": "jira_get_project_issues"}],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "jira_search_issues"
    assert chunks[0].call["arguments"] == {
        "jql": "project = ABC ORDER BY created DESC",
        "fields": "key,summary,status,assignee,updated",
        "limit": 1,
    }


@pytest.mark.asyncio
async def test_confluence_search_question_routes_to_confluence_search() -> None:
    provider = _Provider("I cannot access Confluence")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect_with_tools(
        adapter,
        [Message(role="user", text='Confluence\'ta "AI" kelimesini ara, ilk 3 sonucu yaz.')],
        [{"name": "confluence_search"}],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "confluence_search"
    assert chunks[0].call["arguments"] == {"query": "AI", "limit": 3}


@pytest.mark.asyncio
async def test_bitbucket_repo_list_question_routes_to_list_repos() -> None:
    provider = _Provider("I cannot access Bitbucket")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect_with_tools(
        adapter,
        [Message(role="user", text="Bitbucket'ta erişebildiğim repo listesini getir; ilk 3 repo adını yaz.")],
        [{"name": "bitbucket_list_repos"}, {"name": "bitbucket_search_repos"}],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "bitbucket_list_repos"
    assert chunks[0].call["arguments"] == {"limit": 3}


@pytest.mark.asyncio
async def test_project_latest_task_falls_back_to_project_issues_tool() -> None:
    provider = _Provider("I cannot access Jira")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect_with_tools(
        adapter,
        [Message(role="user", text="Jira project ABC latest task")],
        [{"name": "jira_get_project_issues"}],
    )

    assert provider.calls == []
    assert chunks[0].kind == "tool_call"
    assert chunks[0].call["tool_name"] == "jira_get_project_issues"
    assert chunks[0].call["arguments"] == {"project_key": "ABC", "limit": 1}


@pytest.mark.asyncio
async def test_tool_result_turn_is_answered_by_provider() -> None:
    provider = _Provider("ABC-1 is open.")
    adapter = StreamingProviderAdapter(provider)

    chunks = await _collect(
        adapter,
        [
            Message(role="user", text="List my open Jira tasks assigned to me."),
            Message(role="tool", text='{"issues":[{"key":"ABC-1"}]}'),
        ],
    )

    assert provider.calls
    assert chunks[0].kind == "token"
    assert chunks[0].text == "ABC-1 is open."


@pytest.mark.asyncio
async def test_orchestrator_runs_second_provider_turn_after_tool_result() -> None:
    provider = _Provider("ABC-1 is open.")
    adapter = StreamingProviderAdapter(provider)
    orchestrator = LlmOrchestrator(primary=adapter)

    async def dispatch(_call: Any) -> dict[str, Any]:
        return {"issues": [{"key": "ABC-1"}]}

    events = [
        event
        async for event in orchestrator.stream_with_tool_loop(
            system="system",
            history=[Message(role="user", text="List my open Jira tasks assigned to me.")],
            tools=[{"name": "jira_search"}],
            on_tool_call=dispatch,
            token_cap=1000,
        )
    ]

    assert [event.type for event in events] == [
        "tool_call",
        "tool_result",
        "token",
        "done",
    ]
    assert events[2].payload["text"] == "ABC-1 is open."
