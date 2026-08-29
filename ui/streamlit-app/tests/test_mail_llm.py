from __future__ import annotations

import httpx

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


def test_ask_mail_llm_redacts_and_limits_mail_body_before_provider(monkeypatch) -> None:
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
        captured["payload"] = payload
        return FakeResponse()

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)
    monkeypatch.setattr(mail_llm, "_post_llm_with_retry", fake_post)

    mail_llm.ask_mail_llm(
        "detay getir",
        "gmail",
        "gmail_get_message",
        {
            "structuredContent": {
                "items": [
                    {
                        "subject": "access_token=supersecretvalue12345",
                        "body": "refresh_token=supersecretvalue12345 " + ("x" * 9000),
                    }
                ]
            }
        },
    )

    prompt = captured["payload"]["input"]
    assert "supersecretvalue12345" not in prompt
    assert "[REDACTED_SECRET]" in prompt
    assert len(prompt) < 9000


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

    assert "Openai API key eksik veya placeholder" in answer
    assert "Mail MCP sonucu kisa olarak" in answer
    assert "Secret" in answer


def test_ask_mail_llm_treats_placeholder_openai_key_as_unavailable(monkeypatch) -> None:
    class FakeSettings:
        llm_provider = "openai"
        openai_api_key = "openai_key"
        openai_base_url = "https://llm.example/v1"
        vllm_base_url = "https://vllm.example/v1"
        vllm_api_key = ""
        anthropic_base_url = "https://anthropic.example/v1"
        anthropic_api_key = ""
        llm_model_name = "gpt-4o-mini"
        llm_reasoning_effort = ""
        llm_verbosity = ""

    def fail_post(url, *, headers, payload):
        del url, headers, payload
        raise AssertionError("placeholder key should not call provider")

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)
    monkeypatch.setattr(mail_llm, "_post_llm_with_retry", fail_post)

    answer = mail_llm.ask_mail_llm(
        "son mail",
        "gmail",
        "gmail_get_latest_message",
        {"structuredContent": {"items": [{"subject": "Hello"}]}},
    )

    assert "Openai API key eksik veya placeholder" in answer
    assert "Hello" in answer


def test_ask_mail_llm_reports_unauthorized_provider(monkeypatch) -> None:
    class FakeSettings:
        llm_provider = "openai"
        openai_api_key = "sk-real-looking-but-invalid"
        openai_base_url = "https://llm.example/v1"
        vllm_base_url = "https://vllm.example/v1"
        vllm_api_key = ""
        anthropic_base_url = "https://anthropic.example/v1"
        anthropic_api_key = ""
        llm_model_name = "gpt-4o-mini"
        llm_reasoning_effort = ""
        llm_verbosity = ""

    class FakeResponse:
        status_code = 401

        def raise_for_status(self) -> None:
            request = httpx.Request("POST", "https://llm.example/v1/responses")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

    def fake_post(url, *, headers, payload):
        del url, headers, payload
        return FakeResponse()

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)
    monkeypatch.setattr(mail_llm, "_post_llm_with_retry", fake_post)

    answer = mail_llm.ask_mail_llm(
        "son mail",
        "gmail",
        "gmail_get_latest_message",
        {"structuredContent": {"items": [{"subject": "Hello"}]}},
    )

    assert "Openai API key gecersiz veya yetkisiz" in answer
    assert "Hello" in answer


def test_ask_mail_llm_returns_raw_result_when_llm_is_unreachable(monkeypatch) -> None:
    class FakeSettings:
        llm_provider = "vllm"
        openai_api_key = ""
        openai_base_url = "https://llm.example/v1"
        vllm_base_url = "http://host.docker.internal:8000/v1"
        vllm_api_key = "not-needed"
        anthropic_base_url = "https://anthropic.example/v1"
        anthropic_api_key = ""
        llm_model_name = "local-model"
        llm_reasoning_effort = ""
        llm_verbosity = ""

    def fake_post(url, *, headers, payload):
        raise httpx.ConnectError("[Errno 111] Connection refused")

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)
    monkeypatch.setattr(mail_llm, "_post_llm_with_retry", fake_post)

    answer = mail_llm.ask_mail_llm(
        "son 10 mailimi listele",
        "gmail",
        "gmail_list_messages",
        {"structuredContent": {"items": [{"subject": "Hello", "from": "a@example.com"}]}},
    )

    assert "LLM provider'a ulasilamadi" in answer
    assert "Mail MCP sonucu kisa olarak" in answer
    assert "Hello" in answer


def test_ask_mail_llm_returns_friendly_empty_result(monkeypatch) -> None:
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

    monkeypatch.setattr(mail_llm, "Settings", FakeSettings)

    answer = mail_llm.ask_mail_llm(
        "Ahmet'ten gelenleri bul",
        "gmail",
        "gmail_search_messages",
        {"structuredContent": {"items": []}},
    )

    assert answer == "Bu sorgu icin mail sonucu bulunamadi."
