"""Direct Streamlit assistant backed by OpenAI and stateless Atlassian MCP."""

from __future__ import annotations

from typing import Any

import httpx
import streamlit as st
import streamlit.components.v1 as components

from app import _inject_session_state, render_user_navigation
from chat_runtime import ask_llm, friendly_http_error, plan_and_call_mcp
from components.credential_manager import CredentialManager, restore_cached_credentials
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(page_title="Chat", page_icon=":speech_balloon:", layout="wide")
render_user_navigation()
apply_theme()
page_hero(
    "Chat",
    "Jira, Confluence ve Bitbucket sorularini girilen credential'larla MCP'ye "
    "sorar; yaniti dashboard/env ile secilen LLM provider ile sade cevaba cevirir.",
)
components.html(
    """
    <script>
    (function () {
      const params = new URLSearchParams(window.parent.location.search);
      if (params.has("credential_session")) return;
      const name = "streamlit_credential_session=";
      const raw = document.cookie.split("; ").find((item) => item.startsWith(name));
      if (!raw) return;
      const signedValue = raw.slice(name.length);
      if (!signedValue) return;
      const url = new URL(window.parent.location.href);
      url.searchParams.set("credential_session", signedValue);
      window.parent.location.replace(url.toString());
    })();
    </script>
    """,
    height=0,
)
restore_cached_credentials(st.session_state)


def _manager() -> CredentialManager:
    return CredentialManager(state=st.session_state)


def _credential(service: str) -> Any | None:
    return _manager().get(service)


def _has_required_credentials() -> bool:
    return any(
        _credential(service) is not None
        for service in ("jira", "confluence", "bitbucket")
    )


if not _has_required_credentials():
    st.warning(
        "Chat icin once Credentials sayfasinda en az bir Atlassian credential "
        "girin. LLM provider secimi Admin Dashboard > LLM Providers uzerinden yapilir."
    )

history: list[dict[str, str]] = st.session_state.setdefault("chat_history", [])
for item in history:
    with st.chat_message(item.get("role", "assistant")):
        st.markdown(item.get("text", ""))

user_message = st.chat_input("Jira, Confluence veya Bitbucket icin sorunuzu yazin...")

if user_message:
    history.append({"role": "user", "text": user_message})
    with st.chat_message("user"):
        st.markdown(user_message)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        try:
            restore_cached_credentials(st.session_state)
            placeholder.markdown("MCP sorgusu calistiriliyor...")
            tool_name, tool_result = plan_and_call_mcp(user_message, _credential)
            placeholder.markdown("LLM cevabi hazirlaniyor...")
            answer = ask_llm(user_message, tool_name, tool_result)
            placeholder.markdown(answer)
        except (PermissionError, httpx.HTTPStatusError) as exc:
            answer = (
                friendly_http_error(exc)
                if isinstance(exc, httpx.HTTPStatusError)
                else str(exc)
            )
            placeholder.error(answer)
        except Exception as exc:  # noqa: BLE001
            answer = f"Istek tamamlanamadi: `{exc}`"
            placeholder.error(answer)

    history.append({"role": "assistant", "text": answer})
