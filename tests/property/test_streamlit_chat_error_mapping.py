"""Streamlit chat MCP error mapping."""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

import pytest


_CHAT_PAGE = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
    / "pages"
    / "1_chat.py"
)


class _DummyHTTPStatusError(Exception):
    response = SimpleNamespace(status_code=401)


def _load_chat_helpers() -> dict[str, Any]:
    source = _CHAT_PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted_names = {
        "_AUTH_ERROR_MARKERS",
        "_ERROR_TEXT_MARKERS",
        "_SERVICE_RE",
        "_TOPIC_NOISE_RE",
        "_fold_text",
        "_extract_topic",
        "_extract_field",
        "_is_jira_create_request",
        "_extract_bitbucket_repo",
        "_bitbucket_workspace_from_credential",
        "_plan_and_call_mcp",
        "_looks_like_error_text",
        "_failure_message_from_payload",
        "_result_failure_message",
        "_is_authorization_failure",
        "_permission_denied_message",
        "_raise_if_authorization_failure",
        "_friendly_http_error",
        "_mcp_call_any",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            target_names = {
                target.id for target in node.targets if isinstance(target, ast.Name)
            }
            if target_names & wanted_names:
                body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in wanted_names:
            body.append(node)

    module = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {
        "Any": Any,
        "json": json,
        "re": re,
        "unicodedata": unicodedata,
        "urlparse": urlparse,
        "httpx": SimpleNamespace(HTTPStatusError=_DummyHTTPStatusError),
    }
    exec(compile(module, str(_CHAT_PAGE), "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Confluence'da Strict Live E2E sayfalarini ara ve kisa ozetle.", "strict live e2e"),
        ("Confluence'da strict-live-7e0d4e8b51 sayfasini getir.", "strict-live-7e0d4e8b51"),
        ("Confluence'da platform ile ilgili sayfalari ara.", "platform"),
        ("Confluence son guncellenen sayfalari listele.", ""),
    ],
)
def test_extract_topic_strips_confluence_command_words(
    text: str, expected: str
) -> None:
    helpers = _load_chat_helpers()

    assert helpers["_extract_topic"](text, "") == expected


def test_jira_issue_key_uses_get_issue_tool() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    selected, _result = helpers["_plan_and_call_mcp"]("Jira issue KAN-132 bilgilerini getir.")

    assert selected == "jira_get_issue"
    assert seen[0] == ("jira_get_issue", {"issue_key": "KAN-132"})


def test_jira_project_open_query_filters_by_project() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    helpers["_plan_and_call_mcp"]("Jira'da KAN projesindeki acik tasklari listele.")

    assert seen[0][1]["jql"] == "project = KAN AND statusCategory != Done ORDER BY updated DESC"


def test_jira_english_project_keyword_does_not_capture_jira() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    helpers["_plan_and_call_mcp"]("Jira project KAN acik issue listesini getir.")

    assert seen[0][1]["jql"] == "project = KAN AND statusCategory != Done ORDER BY updated DESC"


def test_bitbucket_workspace_field_wins_over_url() -> None:
    helpers = _load_chat_helpers()
    helpers["_credential"] = lambda service: (
        SimpleNamespace(url="https://bitbucket.org", workspace="example_workspace")
        if service == "bitbucket"
        else None
    )

    assert helpers["_bitbucket_workspace_from_credential"]() == "example_workspace"


def test_bitbucket_repo_list_uses_workspace_field() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []
    helpers["_credential"] = lambda service: (
        SimpleNamespace(url="https://bitbucket.org", workspace="example_workspace")
        if service == "bitbucket"
        else None
    )

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    selected, _result = helpers["_plan_and_call_mcp"]("Bitbucket repo listesini getir.")

    assert selected == "bitbucket_list_repositories"
    assert seen[0] == (
        "bitbucket_list_repositories",
        {"workspace": "example_workspace", "query": None, "max_results": 10},
    )


def test_bitbucket_open_prs_use_pull_request_tool() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []
    helpers["_credential"] = lambda service: (
        SimpleNamespace(url="https://bitbucket.org", workspace="example_workspace")
        if service == "bitbucket"
        else None
    )

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    selected, _result = helpers["_plan_and_call_mcp"](
        "Bitbucket example_workspace/smoke-test acik pull request listesini getir."
    )

    assert selected == "bitbucket_list_pull_requests"
    assert seen[0] == (
        "bitbucket_list_pull_requests",
        {
            "workspace": "example_workspace",
            "repo_slug": "smoke-test",
            "state": "OPEN",
            "max_results": 10,
        },
    )


def test_jira_create_wins_when_description_mentions_other_services() -> None:
    helpers = _load_chat_helpers()
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_call_any(candidates: list[tuple[str, dict[str, Any]]]) -> tuple[str, Any]:
        seen.extend(candidates)
        return candidates[0][0], {"ok": True}

    helpers["_mcp_call_any"] = fake_call_any

    selected, _result = helpers["_plan_and_call_mcp"](
        "Jira task olustur. project KAN, baslik: Planner smoke, "
        "aciklama: Confluence ve Bitbucket kelimeleri geciyor."
    )

    assert selected == "jira_create_issue"
    assert seen[0][0] == "jira_create_issue"
    assert seen[0][1]["project_key"] == "KAN"
    assert seen[0][1]["summary"] == "Planner smoke, aciklama: Confluence ve Bitbucket kelimeleri geciyor."


def test_unauthorized_mcp_result_returns_permission_error() -> None:
    helpers = _load_chat_helpers()
    calls: list[str] = []

    def fake_mcp_call(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"success": False, "error": "Unauthorized (401)"}
                    ),
                }
            ],
            "isError": False,
        }

    helpers["_mcp_call"] = fake_mcp_call

    with pytest.raises(PermissionError, match="Yetkiniz yok"):
        helpers["_mcp_call_any"](
            [("bitbucket_list_repos", {}), ("bitbucket_search_repos", {})]
        )

    assert calls == ["bitbucket_list_repos"]


def test_unknown_tool_falls_back_to_next_candidate() -> None:
    helpers = _load_chat_helpers()
    calls: list[str] = []

    def fake_mcp_call(name: str, _args: dict[str, Any]) -> dict[str, Any]:
        calls.append(name)
        if name == "missing_tool":
            return {
                "content": [{"type": "text", "text": "Unknown tool: missing_tool"}],
                "isError": True,
            }
        return {"content": [{"type": "text", "text": '{"success": true}'}]}

    helpers["_mcp_call"] = fake_mcp_call

    selected, result = helpers["_mcp_call_any"](
        [("missing_tool", {}), ("bitbucket_list_commits", {})]
    )

    assert selected == "bitbucket_list_commits"
    assert result["content"][0]["text"] == '{"success": true}'
    assert calls == ["missing_tool", "bitbucket_list_commits"]
