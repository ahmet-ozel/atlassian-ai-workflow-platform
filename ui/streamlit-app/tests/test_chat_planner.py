from __future__ import annotations

import pytest

from chat_planner import _is_jira_create_request, plan_and_call_mcp


def test_jira_created_date_is_not_create_request() -> None:
    text = "jira KAN-139 detail show issue type and created date"

    assert _is_jira_create_request(text) is False


def test_jira_create_task_is_create_request() -> None:
    text = "create jira task project KAN summary browser scenario"

    assert _is_jira_create_request(text) is True


def _capture_mcp(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def fake_call(candidates, credential_for):
        del credential_for
        captured["candidates"] = candidates
        return candidates[0][0], {"ok": True}

    monkeypatch.setattr("chat_planner.mcp_call_any", fake_call)
    return captured


def test_jira_create_extracts_turkish_project_and_inline_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_mcp(monkeypatch)

    tool_name, _ = plan_and_call_mcp(
        "Jira'da KAN projesinde yeni bir Task olustur. "
        "Baslik: Streamlit reset E2E browser test, "
        "Aciklama: Admin dashboard boot ve Streamlit chat testi. "
        "Olusturdugun issue key'ini soyle.",
        lambda service: None,
    )

    assert tool_name == "jira_create_issue"
    args = captured["candidates"][0][1]
    assert args["project_key"] == "KAN"
    assert args["summary"] == "Streamlit reset E2E browser test"
    assert args["description"] == "Admin dashboard boot ve Streamlit chat testi"


def test_jira_create_accepts_lowercase_explicit_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_mcp(monkeypatch)

    plan_and_call_mcp(
        "jira task olustur proje kan, baslik: lowercase proje, aciklama: test",
        lambda service: None,
    )

    args = captured["candidates"][0][1]
    assert args["project_key"] == "KAN"


def test_jira_create_accepts_project_suffix_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_mcp(monkeypatch)

    plan_and_call_mcp(
        "Jira'da KAN'da task olustur. Baslik: suffix test. Aciklama: test",
        lambda service: None,
    )

    args = captured["candidates"][0][1]
    assert args["project_key"] == "KAN"


def test_jira_create_trims_summary_to_jira_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_mcp(monkeypatch)
    long_title = "x" * 400

    plan_and_call_mcp(
        f"Jira task olustur project KAN, baslik: {long_title}, aciklama: test",
        lambda service: None,
    )

    args = captured["candidates"][0][1]
    assert len(args["summary"]) <= 255
    assert args["summary"].endswith("...")


def test_jira_create_requires_project_key() -> None:
    with pytest.raises(ValueError, match="project key eksik"):
        plan_and_call_mcp(
            "Jira task olustur, baslik: Eksik proje, aciklama: test",
            lambda service: None,
        )


def test_jira_create_does_not_read_project_from_summary_text() -> None:
    with pytest.raises(ValueError, match="project key eksik"):
        plan_and_call_mcp(
            "Jira da yeni task olustur. "
            "baslik: Browser smoke eksik proje testi. "
            "aciklama: Proje key bilincli olarak verilmedi.",
            lambda service: None,
        )


def test_bitbucket_explicit_repo_workspace_wins_over_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_mcp(monkeypatch)

    class Credential:
        workspace = "wrong_workspace"
        url = "https://bitbucket.org/wrong_workspace/other"

    plan_and_call_mcp(
        "Bitbucket Cloud johni_test/smoke-test reposunda son 3 commit listele.",
        lambda service: Credential() if service == "bitbucket" else None,
    )

    args = captured["candidates"][0][1]
    assert args["workspace"] == "johni_test"
    assert args["repo_slug"] == "smoke-test"
    assert args["max_results"] == 3


def test_bitbucket_pr_missing_repo_asks_for_repo() -> None:
    with pytest.raises(ValueError, match="repo belirtin"):
        plan_and_call_mcp("Bitbucket acik pull request listesini getir.", lambda service: None)


def test_confluence_space_and_limit_become_cql(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _capture_mcp(monkeypatch)

    plan_and_call_mcp(
        "Confluence E2ETEST space icindeki ilk 3 sayfayi listele.",
        lambda service: None,
    )

    args = captured["candidates"][0][1]
    assert args["limit"] == 3
    assert 'space = "E2ETEST"' in args["cql"]
