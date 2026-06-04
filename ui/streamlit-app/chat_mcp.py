"""MCP transport and LLM summarization helpers for Streamlit chat."""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Sequence

import httpx

from config import Settings


CredentialGetter = Callable[[str], Any | None]

_MCP_TIMEOUT_SECONDS = 75.0


def _model_supports_reasoning_effort(model: str) -> bool:
    """True when *model* accepts a ``reasoning_effort`` knob.

    Covers OpenAI o-series + the gpt-5 family. Kept inline so the
    Streamlit app stays self-contained.
    """
    norm = (model or "").strip().lower()
    if not norm:
        return False
    for prefix in ("o1", "o3", "o4"):
        if norm == prefix or norm.startswith(prefix + "-"):
            return True
    return norm.startswith("gpt-5")


def _model_supports_verbosity(model: str) -> bool:
    """True when *model* accepts an output ``verbosity`` knob (gpt-5 family)."""
    return (model or "").strip().lower().startswith("gpt-5")


_MCP_MAX_ATTEMPTS = 3
_MCP_RETRY_DELAYS = (0.7, 1.6)

_AUTH_ERROR_MARKERS = ("401", "403", "unauthorized", "forbidden", "permission", "izin", "yetki")
_UNKNOWN_TOOL_MARKERS = ("unknown tool", "not listed")
_TRANSIENT_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "readtimeout",
    "connecttimeout",
    "ssleoferror",
    "unexpected_eof",
    "connection reset",
    "connection aborted",
    "remote protocol error",
    "temporarily unavailable",
    "too many requests",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "429",
    "500",
    "502",
    "503",
    "504",
)
_ERROR_TEXT_MARKERS = _AUTH_ERROR_MARKERS + _UNKNOWN_TOOL_MARKERS + _TRANSIENT_ERROR_MARKERS + (
    "not available",
    "error calling tool",
    "validation error",
)


def _deployment(credential: Any) -> str:
    return str(getattr(credential, "deployment", "cloud") or "cloud").lower()


def _mcp_headers(credential_for: CredentialGetter) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "X-Client-Source": "streamlit-app",
    }
    jira = credential_for("jira")
    if jira is not None:
        headers["X-Atlassian-Jira-Url"] = jira.url
        if _deployment(jira) == "server":
            headers["X-Atlassian-Jira-Personal-Token"] = jira.api_token
        else:
            headers["X-Atlassian-Jira-Username"] = jira.email
            headers["X-Atlassian-Jira-Api-Token"] = jira.api_token
    confluence = credential_for("confluence")
    if confluence is not None:
        headers["X-Atlassian-Confluence-Url"] = confluence.url
        if _deployment(confluence) == "server":
            headers["X-Atlassian-Confluence-Personal-Token"] = confluence.api_token
        else:
            headers["X-Atlassian-Confluence-Username"] = confluence.email
            headers["X-Atlassian-Confluence-Api-Token"] = confluence.api_token
    bitbucket = credential_for("bitbucket")
    if bitbucket is not None:
        headers["X-Atlassian-Bitbucket-Url"] = bitbucket.url
        if _deployment(bitbucket) == "server":
            # Bitbucket Server/DC -> Personal Access Token (Bearer).
            headers["X-Atlassian-Bitbucket-Personal-Token"] = bitbucket.api_token
        else:
            # Bitbucket Cloud -> Atlassian API token (ATATT...) + email via Basic
            # auth, exactly like Jira/Confluence Cloud. App passwords and
            # workspace access tokens (ATCTT...) are NOT used: the MCP truth-table
            # discards a Personal-Token/Bearer header on a Cloud URL and returns 401.
            headers["X-Atlassian-Bitbucket-Username"] = bitbucket.email
            headers["X-Atlassian-Bitbucket-App-Password"] = bitbucket.api_token
            headers["X-Atlassian-Bitbucket-Api-Token"] = bitbucket.api_token
    return headers


def _parse_mcp_response(text: str) -> Any:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
        return payload.get("result", payload)

    payload = json.loads(text)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(json.dumps(payload["error"], ensure_ascii=False))
    return payload.get("result", payload) if isinstance(payload, dict) else payload


def _mcp_jsonrpc(
    method: str,
    credential_for: CredentialGetter,
    params: dict[str, Any] | None = None,
) -> Any:
    base_url = Settings().mcp_base_url.rstrip("/")
    payload = {
        "jsonrpc": "2.0",
        "id": method,
        "method": method,
        "params": params or {},
    }
    timeout = httpx.Timeout(_MCP_TIMEOUT_SECONDS, connect=10.0)
    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url}/mcp",
            json=payload,
            headers=_mcp_headers(credential_for),
        )
    response.raise_for_status()
    return _parse_mcp_response(response.text)


def _mcp_call(
    tool_name: str,
    credential_for: CredentialGetter,
    arguments: dict[str, Any] | None = None,
) -> Any:
    return _mcp_jsonrpc(
        "tools/call",
        credential_for,
        {"name": tool_name, "arguments": arguments or {}},
    )


def _looks_like_error_text(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ERROR_TEXT_MARKERS)


def _failure_message_from_payload(payload: Any) -> str | None:
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return None
        try:
            return _failure_message_from_payload(json.loads(stripped))
        except json.JSONDecodeError:
            return stripped if _looks_like_error_text(stripped) else None

    if not isinstance(payload, dict):
        return None

    if payload.get("success") is False:
        detail = payload.get("error") or payload.get("message") or payload
        return str(detail)

    if payload.get("error"):
        detail = payload["error"]
        if isinstance(detail, dict):
            return str(detail.get("message") or detail)
        return str(detail)

    content = payload.get("content")
    if isinstance(content, list):
        text_parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        for text in text_parts:
            message = _failure_message_from_payload(text)
            if message:
                return message
        if payload.get("isError") is True and text_parts:
            return "\n".join(text_parts)

    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        message = _failure_message_from_payload(structured.get("result"))
        if message:
            return message

    if payload.get("isError") is True:
        return "MCP tool hata dondurdu."

    return None


def _is_authorization_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _AUTH_ERROR_MARKERS)


def _is_unknown_tool_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _UNKNOWN_TOOL_MARKERS)


def _is_transient_failure(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


def _permission_denied_message(detail: str | None = None) -> str:
    message = (
        "Yetkiniz yok. Bu islem icin token gecersiz, suresi dolmus "
        "veya gerekli Atlassian/Bitbucket izni yok."
    )
    if detail:
        return f"{message} Detay: {detail[:300]}"
    return message


def _raise_if_authorization_failure(message: str) -> None:
    if _is_authorization_failure(message):
        raise PermissionError(_permission_denied_message(message))


def friendly_http_error(exc: httpx.HTTPStatusError) -> str:
    status_code = exc.response.status_code
    if status_code in (401, 403):
        return _permission_denied_message(f"HTTP {status_code}")
    return str(exc)


def _exception_message(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return f"{exc.__class__.__name__}: {text}"
    return exc.__class__.__name__


def _retry_delay(attempt: int) -> float:
    if attempt < len(_MCP_RETRY_DELAYS):
        return _MCP_RETRY_DELAYS[attempt]
    return _MCP_RETRY_DELAYS[-1]


def _post_llm_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> httpx.Response:
    """POST to the active LLM provider with transient retry protection."""

    last_error: Exception | None = None
    retry_statuses = {429, 500, 502, 503, 504}
    for attempt in range(_MCP_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(url, headers=headers, json=payload)
            if response.status_code in retry_statuses:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if attempt < _MCP_MAX_ATTEMPTS - 1:
                        time.sleep(_retry_delay(attempt))
                        continue
                    raise
            return response
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if attempt < _MCP_MAX_ATTEMPTS - 1:
                time.sleep(_retry_delay(attempt))
                continue
            raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("LLM provider yanit vermedi.")


def mcp_call_any(
    candidates: Sequence[tuple[str, dict[str, Any]]],
    credential_for: CredentialGetter,
) -> tuple[str, Any]:
    errors: list[str] = []
    unknown_tool_errors: list[str] = []
    for name, args in candidates:
        for attempt in range(_MCP_MAX_ATTEMPTS):
            try:
                result = _mcp_call(name, credential_for, args)
                failure_message = _failure_message_from_payload(result)
                if not failure_message:
                    return name, result

                _raise_if_authorization_failure(failure_message)
                formatted = f"{name}: {failure_message}"
                if _is_unknown_tool_failure(failure_message):
                    unknown_tool_errors.append(formatted)
                    break
                if _is_transient_failure(failure_message) and attempt < _MCP_MAX_ATTEMPTS - 1:
                    time.sleep(_retry_delay(attempt))
                    continue
                errors.append(formatted)
                break
            except PermissionError:
                raise
            except httpx.HTTPStatusError as exc:
                error_message = friendly_http_error(exc)
                _raise_if_authorization_failure(error_message)
                if _is_transient_failure(error_message) and attempt < _MCP_MAX_ATTEMPTS - 1:
                    time.sleep(_retry_delay(attempt))
                    continue
                errors.append(f"{name}: {error_message}")
                break
            except Exception as exc:  # noqa: BLE001
                error_message = _exception_message(exc)
                _raise_if_authorization_failure(error_message)
                if _is_transient_failure(error_message) and attempt < _MCP_MAX_ATTEMPTS - 1:
                    time.sleep(_retry_delay(attempt))
                    continue
                if _is_unknown_tool_failure(error_message):
                    unknown_tool_errors.append(f"{name}: {error_message}")
                else:
                    errors.append(f"{name}: {error_message}")
                break
    final_errors = errors or unknown_tool_errors
    raise RuntimeError(" | ".join(final_errors[-3:]))


def _llm_chat_url(settings: Settings) -> tuple[str, str, str]:
    """Return (url, api_key, api_kind) for the active provider.

    ``api_kind`` is ``"responses"`` for OpenAI (Responses API),
    ``"chat"`` for vLLM (OpenAI-compatible Chat Completions), and
    ``"anthropic"`` for Anthropic Messages.
    """
    provider = settings.llm_provider
    if provider == "vllm":
        return (
            settings.vllm_base_url.rstrip("/") + "/chat/completions",
            settings.vllm_api_key,
            "chat",
        )
    if provider == "anthropic":
        return (
            settings.anthropic_base_url.rstrip("/") + "/messages",
            settings.anthropic_api_key,
            "anthropic",
        )
    return (
        settings.openai_base_url.rstrip("/") + "/responses",
        settings.openai_api_key,
        "responses",
    )


def _extract_responses_text(data: Any) -> str:
    """Pull assistant text out of an OpenAI Responses API payload."""
    if not isinstance(data, dict):
        return ""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    if isinstance(output_text, list):
        joined = "".join(p for p in output_text if isinstance(p, str))
        if joined:
            return joined
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


def _extract_anthropic_text(data: Any) -> str:
    """Pull assistant text out of an Anthropic Messages API payload."""
    if not isinstance(data, dict):
        return ""
    chunks: list[str] = []
    for part in data.get("content", []) or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def ask_llm(user_text: str, tool_name: str, tool_result: Any) -> str:
    settings = Settings()
    url, api_key, api_kind = _llm_chat_url(settings)
    if settings.llm_provider in ("openai", "anthropic") and not api_key:
        raw = json.dumps(tool_result, ensure_ascii=False, indent=2, default=str)[:4000]
        return (
            "LLM provider dashboard/env tarafinda bagli degil; MCP sonucu ham olarak donuyor:\n\n"
            "```json\n" + raw + "\n```"
        )

    model = settings.llm_model_name
    system_prompt = (
        "Sen Streamlit icinde calisan Atlassian asistanisin. "
        "Cevabi Turkce, kisa, net ver. MCP sonucuna dayan; uydurma. "
        "Kullanici kac kayit istediyse o kadarini yaz. "
        "Eksik bilgi varsa islemi tamamlamaya calisma; eksik alanlari "
        "madde madde soyle ve ornek format ver. "
        "Kullanici sormadikca 'eksik bilgi yok' gibi kapanis cumlesi ekleme."
    )
    user_prompt = (
        f"Kullanici sorusu:\n{user_text}\n\n"
        f"Kullanilan MCP tool: {tool_name}\n"
        f"MCP sonucu JSON:\n"
        f"{json.dumps(tool_result, ensure_ascii=False, default=str)[:12000]}"
    )
    headers = {"Content-Type": "application/json"}
    if api_kind == "anthropic" and api_key:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if api_kind == "responses":
        payload = {
            "model": model,
            "instructions": system_prompt,
            "input": user_prompt,
            "temperature": 0.1,
            "max_output_tokens": 900,
        }
        # Reasoning-capable models (gpt-5 family / o-series) reject an
        # explicit temperature, so drop it for them regardless of whether
        # a tuning knob was configured.
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
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }
    else:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 900,
        }

    response = _post_llm_with_retry(url, headers=headers, payload=payload)
    response.raise_for_status()
    data = response.json()
    if api_kind == "responses":
        return _extract_responses_text(data)
    if api_kind == "anthropic":
        return _extract_anthropic_text(data)
    return data["choices"][0]["message"]["content"]
