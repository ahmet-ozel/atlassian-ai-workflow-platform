"""Per-session credentials for the Streamlit assistant."""

from __future__ import annotations

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components.credential_manager import render_credential_manager
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(page_title="Credentials", page_icon=":key:")
render_user_navigation()
apply_theme()

page_hero(
    "Credentials",
    "Jira, Confluence ve Bitbucket bilgilerini sadece bu tarayici "
    "oturumu icin girin. LLM provider ayari Admin Dashboard uzerinden yapilir.",
)

render_credential_manager()

st.info(
    "OpenAI/vLLM secimi ve API key baglantisi Admin Dashboard > LLM Providers "
    "ekranindan yapilir; bu sayfada LLM credential tutulmaz."
)
