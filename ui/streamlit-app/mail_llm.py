"""LLM summarisation helper for Streamlit Mail Chat."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

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

_BODY_CHAR_LIMIT = 4000
_TEXT_FIELD_LIMIT = 700
_LLM_JSON_LIMIT = 8000
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[^\s'\"&]{8,}"
    ),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{12,}"),
    re.compile(r"\b1//[A-Za-z0-9._-]{12,}"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)


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


def _raw_mail_result(tool_result: Any, *, reason: str = "") -> str:
    items = _extract_mail_items(tool_result)
    if not items:
        return "Bu sorgu icin mail sonucu bulunamadi."
    heading = "Mail MCP sonucu kisa olarak:"
    if reason:
        heading = f"{reason}; {heading}"
    lines = [heading]
    for index, item in enumerate(items[:10], start=1):
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "(konu yok)")
        sender = str(item.get("from") or "(gonderen yok)")
        date = str(item.get("date") or "")
        snippet = str(item.get("snippet") or "")
        detail = f"{index}. {subject} - {sender}"
        if date:
            detail += f" - {date}"
        if snippet:
            detail += f"\n   {snippet}"
        lines.append(detail)
    return "\n".join(lines)


def _extract_mail_items(tool_result: Any) -> list[Any]:
    if not isinstance(tool_result, dict):
        return []
    structured = tool_result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("items"), list):
        return structured["items"]
    for key in ("items", "messages", "emails"):
        value = tool_result.get(key)
        if isinstance(value, list):
            return value
    return []


def _empty_mail_result(tool_result: Any) -> bool:
    if not isinstance(tool_result, dict):
        return False
    structured = tool_result.get("structuredContent")
    if isinstance(structured, dict) and structured.get("items") == []:
        return True
    return any(tool_result.get(key) == [] for key in ("items", "messages", "emails"))


def _raw_mail_json(tool_result: Any) -> str:
    raw = _safe_mail_json(tool_result, limit=4000)
    return (
        "LLM provider dashboard/env tarafinda bagli degil; Mail MCP sonucu ham olarak donuyor:\n\n"
        "```json\n" + raw + "\n```"
    )


def _api_key_available(settings: Settings) -> bool:
    if settings.llm_provider == "openai":
        key = settings.openai_api_key.strip()
        return bool(key) and key not in {"openai_key", "your-openai-api-key", "changeme"}
    if settings.llm_provider == "anthropic":
        key = settings.anthropic_api_key.strip()
        return bool(key) and key not in {"anthropic_key", "your-anthropic-api-key", "changeme"}
    return True


def _safe_mail_json(value: Any, *, limit: int = _LLM_JSON_LIMIT) -> str:
    raw = json.dumps(_safe_mail_value(value), ensure_ascii=False, indent=2, default=str)
    if len(raw) <= limit:
        return raw
    return raw[:limit].rstrip() + "\n...[truncated]"


def _safe_mail_value(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): _safe_mail_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_mail_value(item, key=key) for item in value]
    if isinstance(value, str):
        field_limit = _BODY_CHAR_LIMIT if key.lower() == "body" else _TEXT_FIELD_LIMIT
        return _safe_mail_text(value, field_limit)
    return value


def _safe_mail_text(value: str, limit: int) -> str:
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redaction_replacement, text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[truncated]"


def _redaction_replacement(match: re.Match[str]) -> str:
    first_group = match.group(1) if match.groups() else ""
    if first_group:
        return f"{first_group}=[REDACTED_SECRET]"
    if match.group(0).lower().startswith("bearer "):
        return "Bearer [REDACTED_SECRET]"
    return "[REDACTED_SECRET]"


def ask_mail_llm(
    user_text: str,
    provider: MailProvider,
    tool_name: str,
    tool_result: Any,
) -> str:
    """Summarise a read-only Mail MCP result for the Streamlit Mail Chat page."""

    settings = Settings()
    url, api_key, api_kind = _llm_chat_url(settings)
    if _empty_mail_result(tool_result):
        return "Bu sorgu icin mail sonucu bulunamadi."
    if settings.llm_provider in ("openai", "anthropic") and not _api_key_available(settings):
        return _raw_mail_result(
            tool_result,
            reason=f"{settings.llm_provider.title()} API key eksik veya placeholder",
        )

    model = settings.llm_model_name
    system_prompt = _mail_system_prompt()
    user_prompt = (
        f"Kullanici sorusu:\n{user_text}\n\n"
        f"Mail provider: {provider}\n"
        f"Kullanilan MCP tool: {tool_name}\n"
        "MCP sonucu JSON:\n"
        f"{_safe_mail_json(tool_result)}"
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

    try:
        response = _post_llm_with_retry(url, headers=headers, payload=payload)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in {401, 403}:
            reason = f"{settings.llm_provider.title()} API key gecersiz veya yetkisiz"
        else:
            reason = f"LLM provider HTTP {status_code} dondu"
        return _raw_mail_result(tool_result, reason=reason)
    except (httpx.HTTPError, OSError):
        return _raw_mail_result(tool_result, reason="LLM provider'a ulasilamadi")
    data = response.json()
    if api_kind == "responses":
        return _extract_responses_text(data)
    if api_kind == "anthropic":
        return _extract_anthropic_text(data)
    return data["choices"][0]["message"]["content"]
