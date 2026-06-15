"""Isolated Streamlit Mail Chat page.

This page is intentionally separate from the Atlassian Chat page. It owns
its own session history and routes read-only mail requests through the
mail planner + Mail MCP client.
"""

from __future__ import annotations

from dataclasses import dataclass
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
                "OAuth client config ve token yenileme mail MCP servisinin "
                "kendi `.env` dosyasinda yonetilir. Streamlit bu provider icin "
                "access token veya refresh token saklamaz."
            )
            st.caption(
                "Beklenen MCP env anahtarlari: "
                + ", ".join(f"`{key}`" for key in auth_status.required_env_keys)
            )


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
                placeholder.markdown("Read-only mail MCP sorgusu calistiriliyor...")
                provider, tool_name, result = plan_and_call_mail_mcp(
                    user_message,
                    ready_providers,
                )
                placeholder.markdown("Mail cevabi hazirlaniyor...")
                answer = ask_mail_llm(user_message, provider, tool_name, result)
                placeholder.markdown(answer)
            except ValueError as exc:
                answer = str(exc)
                placeholder.error(answer)
            except MailMcpError as exc:
                answer = f"Mail MCP istegi tamamlanamadi: {exc}"
                placeholder.error(answer)

        history.append({"role": "assistant", "text": answer})
