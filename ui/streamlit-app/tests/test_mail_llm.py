from __future__ import annotations

import mail_llm


def test_mail_system_prompt_is_mail_specific_and_read_only() -> None:
    prompt = mail_llm._mail_system_prompt()

    assert "mail asistanisin" in prompt
    assert "Atlassian" not in prompt
    assert "MCP sonucuna dayan" in prompt
    assert "uydurma" in prompt
    assert "Hassas veri" in prompt
    assert "tam mail govdesini" in prompt
    assert "read-only" in prompt


def test_ask_mail_llm_uses_mail_prompt(monkeypatch) -> None:
    captured = {}

    class FakeSettings:
        llm_provider = "openai"
        openai_api_key = "key"
        openai_base_url = "https://llm.example/v1"
        vllm_base_url = "https://vllm.example/v1"
        vllm_api_key = ""
        anthropic_base_url = "https://anthropic.example/v1"
        anthropic_api_key = ""
        llm_model_name = "gpt-4o-mini"
        llm_reasoning_effort = ""
        llm_verbosity = ""

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"output_text": "Kisa cevap"}

    def fake_post(url, *, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return FakeResponse()

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)
    monkeypatch.setattr(mail_llm, "_post_llm_with_retry", fake_post)

    answer = mail_llm.ask_mail_llm(
        "son mailleri ozetle",
        "gmail",
        "gmail_list_messages",
        {"messages": [{"subject": "Hello"}]},
    )

    assert answer == "Kisa cevap"
    assert captured["url"] == "https://llm.example/v1/responses"
    assert "mail asistanisin" in captured["payload"]["instructions"]
    assert "tam mail govdesini" in captured["payload"]["instructions"]
    assert "Mail provider: gmail" in captured["payload"]["input"]
    assert "gmail_list_messages" in captured["payload"]["input"]


def test_ask_mail_llm_returns_raw_result_without_openai_key(monkeypatch) -> None:
    class FakeSettings:
        llm_provider = "openai"
        openai_api_key = ""
        openai_base_url = "https://llm.example/v1"
        vllm_base_url = "https://vllm.example/v1"
        vllm_api_key = ""
        anthropic_base_url = "https://anthropic.example/v1"
        anthropic_api_key = ""
        llm_model_name = "gpt-4o-mini"
        llm_reasoning_effort = ""
        llm_verbosity = ""

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)

    answer = mail_llm.ask_mail_llm(
        "son mail",
        "outlook",
        "outlook_list_messages",
        {"messages": [{"subject": "Secret"}]},
    )

    assert "Mail MCP sonucu ham olarak donuyor" in answer
    assert "Secret" in answer
