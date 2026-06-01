from __future__ import annotations

from chat_planner import _is_jira_create_request


def test_jira_created_date_is_not_create_request() -> None:
    text = "jira KAN-139 detail show issue type and created date"

    assert _is_jira_create_request(text) is False


def test_jira_create_task_is_create_request() -> None:
    text = "create jira task project KAN summary browser scenario"

    assert _is_jira_create_request(text) is True
