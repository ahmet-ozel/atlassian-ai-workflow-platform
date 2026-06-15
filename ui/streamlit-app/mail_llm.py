"""LLM summarisation helper for Streamlit Mail Chat."""

from __future__ import annotations

import json
from typing import Any

from chat_mcp import (
    _anthropic_model_supports_reasoning_effort,
    _anthropic_model_uses_implicit_adaptive_thinking,
    _extract_anthropic_text,
    _extract_responses_text,
    _llm_chat_url,
    _model_supports_reasoning_effort,
    _model_supports_verbosity,
    _post_llm_with_retry,
)
from config import Settings
from mail_mcp import MailProvider


def _mail_system_prompt() -> str:
    return (
        "Sen Streamlit icinde calisan mail asistanisin. "
        "Cevabi Turkce, kisa ve net ver. Yalnizca MCP sonucuna dayan; "
        "MCP sonucunda mail icerigi veya ilgili alan yoksa uydurma, bunu acikca soyle. "
        "Hassas veri, kisisel bilgi, token, link veya kimlik bilgisi gorursen "
        "gerektigi kadar ozetle ve gereksiz ayrinti verme. "
        "Kullanici acikca istemedikce tam mail govdesini, uzun alintiyi veya ham header'lari basma. "
        "Liste istenirse konu, gonderen, tarih ve kisa neden/ozet alanlariyla sinirli kal. "
        "Mail gonderme, silme, arsivleme, tasima veya cevaplama islemi yapamazsin; "
        "bu isteklerde read-only oldugunu soyle."
    )


def _raw_mail_result(tool_result: Any) -> str:
    raw = json.dumps(tool_result, ensure_ascii=False, indent=2, default=str)[:4000]
    return (
        "LLM provider dashboard/env tarafinda bagli degil; Mail MCP sonucu ham olarak donuyor:\n\n"
        "```json\n" + raw + "\n```"
    )


def ask_mail_llm(
    user_text: str,
    provider: MailProvider,
    tool_name: str,
    tool_result: Any,
) -> str:
    """Summarise a read-only Mail MCP result for the Streamlit Mail Chat page."""

    settings = Settings()
    url, api_key, api_kind = _llm_chat_url(settings)
    if settings.llm_provider in ("openai", "anthropic") and not api_key:
        return _raw_mail_result(tool_result)

    model = settings.llm_model_name
    system_prompt = _mail_system_prompt()
    user_prompt = (
        f"Kullanici sorusu:\n{user_text}\n\n"
        f"Mail provider: {provider}\n"
        f"Kullanilan MCP tool: {tool_name}\n"
        "MCP sonucu JSON:\n"
        f"{json.dumps(tool_result, ensure_ascii=False, default=str)[:12000]}"
    )

    headers = {"Content-Type": "application/json"}
    if api_kind == "anthropic" and api_key:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if api_kind == "responses":
        payload: dict[str, Any] = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "temperature": 0.1,
            "max_output_tokens": 700,
        }
        reasoning_effort = getattr(settings, "llm_reasoning_effort", "")
        verbosity = getattr(settings, "llm_verbosity", "")
        if _model_supports_reasoning_effort(model):
            payload.pop("temperature", None)
            if reasoning_effort:
                payload["reasoning"] = {"effort": reasoning_effort}
        if verbosity and _model_supports_verbosity(model):
            payload["text"] = {"verbosity": verbosity}
    elif api_kind == "anthropic":
        payload = {
            "model": model,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "temperature": 0.1,
            "max_tokens": 700,
        }
        reasoning_effort = getattr(settings, "llm_reasoning_effort", "")
        if reasoning_effort and _anthropic_model_supports_reasoning_effort(model):
            if not _anthropic_model_uses_implicit_adaptive_thinking(model):
                payload["thinking"] = {"type": "adaptive"}
            payload["output_config"] = {"effort": reasoning_effort}
            payload.pop("temperature", None)
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 700,
        }

    response = _post_llm_with_retry(url, headers=headers, payload=payload)
    response.raise_for_status()
    data = response.json()
    if api_kind == "responses":
        return _extract_responses_text(data)
    if api_kind == "anthropic":
        return _extract_anthropic_text(data)
    return data["choices"][0]["message"]["content"]
