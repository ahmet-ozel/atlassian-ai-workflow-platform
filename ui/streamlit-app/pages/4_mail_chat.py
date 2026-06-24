"""Isolated Streamlit Mail Chat page.

This page is intentionally separate from the Atlassian Chat page. It owns
its own session history and routes read-only mail requests through
assistant-service mail mode, with the direct Mail MCP path as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import streamlit as st

from app import _inject_session_state, render_user_navigation
from config import Settings
from components.theme import apply_theme, page_hero, section_header
from mail_auth import mail_auth_statuses
from mail_llm import ask_mail_llm
from mail_mcp import MailMcpError, list_mail_tools
from mail_planner import plan_and_call_mail_mcp


Provider = Literal["gmail", "outlook"]


@dataclass(frozen=True, slots=True)
class MailMcpStatus:
    provider: Provider
    label: str
    base_url: str
    ok: bool
    detail: str
    tool_count: int = 0


def _settings() -> Settings:
    cached = st.session_state.get("_settings")
    return cached if isinstance(cached, Settings) else Settings()


def _user_session_id() -> str:
    user = st.session_state.get("user") or {}
    if isinstance(user, dict):
        session_id = str(user.get("session_id") or "").strip()
        if session_id:
            return session_id
    return "mail-chat"


def _mail_credential_refs(providers: list[Provider]) -> dict[Provider, str]:
    refs: dict[Provider, str] = {}
    user = st.session_state.get("user") or {}
    session_id = str(user.get("session_id") or "").strip() if isinstance(user, dict) else ""
    bound = set(st.session_state.get("bound_credentials") or set())
    for provider in providers:
        credential = st.session_state.get(f"credential_{provider}")
        vault_path = str(getattr(credential, "vault_path", "") or "").strip()
        if not vault_path and session_id:
            vault_path = f"vault:atlassian/_user_session/{session_id}/{provider}"
        if vault_path:
            refs[provider] = vault_path
            bound.add(provider)
            st.session_state[f"credential_{provider}"] = SimpleNamespace(vault_path=vault_path)
    if bound:
        st.session_state["bound_credentials"] = bound
    return refs


def _assistant_mail_answer(
    user_message: str,
    history: list[dict[str, str]],
) -> str:
    assistant_client = st.session_state.get("_assistant_client")
    stream = getattr(assistant_client, "stream", None)
    if not callable(stream):
        raise RuntimeError("assistant-service client is not configured")

    # Exclude the just-appended current user message from history sent upstream.
    prior_history = history[:-1] if history and history[-1].get("role") == "user" else history
    events = stream(
        dept_id="mail",
        session_id=_user_session_id(),
        text=user_message,
        history=prior_history,
        mode="mail",
    )
    chunks: list[str] = []
    last_error = ""
    for event in events:
        event_type = event.get("type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type in {"token", "assistant_delta"}:
            chunks.append(str(payload.get("text") or payload.get("delta") or ""))
        elif event_type in {"assistant_message", "message"}:
            chunks.append(str(payload.get("text") or payload.get("content") or ""))
        elif event_type == "error":
            last_error = str(payload.get("error") or payload.get("reason") or payload)
    answer = "".join(chunks).strip()
    if answer:
        return answer
    if last_error:
        raise RuntimeError(last_error)
    raise RuntimeError("assistant-service mail mode returned no answer")


def _asks_for_message_id(answer: str) -> bool:
    lowered = answer.lower()
    return (
        "message id" in lowered
        and any(marker in lowered for marker in ("gerekli", "required", "mail id"))
    )


def _direct_mail_mcp_answer(
    user_message: str,
    providers: list[Provider],
) -> str:
    provider, tool_name, result = plan_and_call_mail_mcp(
        user_message,
        providers,
        credential_refs=_mail_credential_refs(providers),
    )
    return ask_mail_llm(user_message, provider, tool_name, result)


@st.cache_data(ttl=20, show_spinner=False)
def _probe_mail_mcp_cached(
    provider: Provider,
    label: str,
    base_url: str,
) -> dict[str, Any]:
    clean_url = base_url.strip().rstrip("/")
    if not clean_url:
        return {
            "provider": provider,
            "label": label,
            "base_url": "",
            "ok": False,
            "detail": "Base URL yapilandirilmadi.",
            "tool_count": 0,
        }

    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"{clean_url}/healthz")
    except httpx.RequestError as exc:
        return {
            "provider": provider,
            "label": label,
            "base_url": clean_url,
            "ok": False,
            "detail": f"Ulasilamadi: {exc.__class__.__name__}",
            "tool_count": 0,
        }

    if 200 <= response.status_code < 300:
        try:
            tool_count = len(list_mail_tools(provider))
        except MailMcpError:
            tool_count = 0
        return {
            "provider": provider,
            "label": label,
            "base_url": clean_url,
            "ok": True,
            "detail": f"{tool_count} read-only tool",
            "tool_count": tool_count,
        }
    return {
        "provider": provider,
        "label": label,
        "base_url": clean_url,
        "ok": False,
        "detail": f"HTTP {response.status_code}",
        "tool_count": 0,
    }


def _probe_mail_mcp(provider: Provider, label: str, base_url: str) -> MailMcpStatus:
    return MailMcpStatus(**_probe_mail_mcp_cached(provider, label, base_url))


def _mail_statuses(settings: Settings) -> list[MailMcpStatus]:
    return [
        _probe_mail_mcp("gmail", "Gmail MCP", settings.gmail_mcp_base_url),
        _probe_mail_mcp("outlook", "Outlook MCP", settings.outlook_mcp_base_url),
    ]


def _render_status(statuses: list[MailMcpStatus]) -> None:
    section_header("MCP durumu", "Gmail ve Outlook endpoint kontrolleri")
    cols = st.columns(len(statuses))
    for col, status in zip(cols, statuses):
        state = "Hazir" if status.ok else "Bekliyor"
        col.metric(status.label, state, status.detail)
        if status.base_url:
            col.caption(status.base_url)


def _render_auth_status() -> None:
    section_header("OAuth modeli", "mail credential'lari Streamlit'te tutulmaz")
    for auth_status in mail_auth_statuses():
        with st.expander(auth_status.label, expanded=False):
            st.markdown(
                "Kullanici mail credential'i Credentials ekranindan Vault'a yazilir. "
                "Mail MCP servisi bu kullaniciya ait credential ref ile okur; "
                "mail token'i veya provider client secret'i `.env` dosyasindan beklenmez."
            )
            st.caption(f"Token modeli: `{auth_status.token_storage}`")
            if auth_status.required_env_keys:
                st.caption(
                    "Beklenen MCP env anahtarlari: "
                    + ", ".join(f"`{key}`" for key in auth_status.required_env_keys)
                )
            else:
                st.caption("Mail provider icin zorunlu MCP env secret'i yok.")


def _initial_message(statuses: list[MailMcpStatus]) -> str:
    ready = [status.label for status in statuses if status.ok]
    if ready:
        return (
            "Mail Chat ayri bir oturum olarak hazir. "
            f"Bagli servisler: {', '.join(ready)}. "
            "Son mail, okunmamis mail, gonderen/konu aramasi ve mail detayi "
            "read-only olarak denenebilir."
        )
    return (
        "Mail Chat ayri bir oturum olarak hazir, ancak Gmail/Outlook MCP "
        "endpointleri su an hazir gorunmuyor. Servisler ayaga kalkinca bu "
        "ekran otomatik olarak durumlarini gosterecek."
    )


_inject_session_state()
st.set_page_config(page_title="Mail Chat", page_icon="M", layout="wide")
render_user_navigation()
apply_theme()
page_hero(
    "Mail Chat",
    "Gmail ve Outlook MCP baglantilari icin ayrilmis sohbet ekrani.",
)

settings = _settings()
statuses = _mail_statuses(settings)
_render_status(statuses)
_render_auth_status()

history: list[dict[str, str]] = st.session_state.setdefault(
    "mail_chat_history",
    [{"role": "assistant", "text": _initial_message(statuses)}],
)

for item in history:
    with st.chat_message(item.get("role", "assistant")):
        st.markdown(item.get("text", ""))

user_message = st.chat_input(
    "Mail hakkinda sorunuzu yazin...",
    key="mail_chat_input",
)

if user_message:
    history.append({"role": "user", "text": user_message})
    with st.chat_message("user"):
        st.markdown(user_message)

    ready_providers = [status.provider for status in statuses if status.ok]
    if not ready_providers:
        answer = (
            "Mail Chat mesaji ayri `mail_chat_history` oturumunda saklandi. "
            "Gmail/Outlook MCP henuz erisilebilir olmadigi icin mail sorgusu "
            "calistiramiyorum."
        )
        history.append({"role": "assistant", "text": answer})
        with st.chat_message("assistant"):
            st.markdown(answer)
    else:
        with st.chat_message("assistant"):
            placeholder = st.empty()
            try:
                placeholder.markdown("Assistant-service mail mode calistiriliyor...")
                try:
                    answer = _assistant_mail_answer(user_message, history)
                    if _asks_for_message_id(answer):
                        raise RuntimeError("assistant-service asked for message id")
                except Exception:
                    placeholder.markdown("Read-only mail MCP sorgusu calistiriliyor...")
                    placeholder.markdown("Mail cevabi hazirlaniyor...")
                    answer = _direct_mail_mcp_answer(user_message, ready_providers)
                placeholder.markdown(answer)
            except ValueError as exc:
                answer = str(exc)
                placeholder.error(answer)
            except MailMcpError as exc:
                answer = str(exc)
                placeholder.error(answer)

        history.append({"role": "assistant", "text": answer})
