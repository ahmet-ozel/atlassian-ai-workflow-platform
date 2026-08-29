"""Per-session credentials for the Streamlit assistant."""

from __future__ import annotations

from types import SimpleNamespace

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components.credential_manager import render_credential_manager
from components.theme import apply_theme, page_hero, section_header


_inject_session_state()
st.set_page_config(page_title="Credentials", page_icon="Key")
render_user_navigation()
apply_theme()

page_hero(
    "Credentials",
    "Jira, Confluence ve Bitbucket bilgilerini sadece bu tarayici "
    "oturumu icin girin. LLM provider ayari Admin Dashboard uzerinden yapilir.",
)

render_credential_manager()


def _render_mail_oauth_form() -> None:
    section_header("Mail baglantisi", "Gmail ve Outlook kullanici bazli OAuth")
    st.caption(
        "Mail token'lari veya provider client secret'lari .env dosyasina yazilmaz. "
        "Submit sonrasi assistant-service kullanici oturumuna ait Vault path'ine "
        "yazar; MCP servisleri sadece bu credential ref ile mailbox okur."
    )
    user = st.session_state.get("user") or {}
    session_id = str(user.get("session_id") or "").strip()
    if not session_id:
        st.warning("Mail baglantisi icin aktif kullanici session_id gerekli.")
        return

    with st.form("mail_oauth_credential_form", clear_on_submit=True):
        provider = st.selectbox("Provider", ["gmail", "outlook"], format_func=str.title)
        email = st.text_input("Mail adresi", placeholder="user@company.com")
        refresh_token = st.text_input(
            "Refresh token",
            type="password",
            help="Read-only Gmail/Outlook OAuth refresh token.",
        )
        with st.expander("Provider client bilgileri", expanded=True):
            client_id = st.text_input(
                "Client ID",
                help="Refresh token yenilemesi icin kullanicinin OAuth client id degeri.",
            )
            client_secret = st.text_input(
                "Client secret",
                type="password",
                help="Refresh token yenilemesi icin kullanicinin OAuth client secret degeri.",
            )
            scopes = st.text_input(
                "Scopes",
                value=(
                    "https://www.googleapis.com/auth/gmail.readonly"
                    if provider == "gmail"
                    else "offline_access Mail.Read"
                ),
            )
        submitted = st.form_submit_button(f"{provider.title()} bagla")

    if not submitted:
        return
    if not refresh_token.strip():
        st.error("Refresh token gerekli.")
        return
    if not client_id.strip() or not client_secret.strip():
        st.error("Client ID ve client secret gerekli; mail tarafi .env'den okunmaz.")
        return
    api = st.session_state.get("_credential_api")
    if api is None:
        st.error("Credential API yapilandirilmamis.")
        return
    try:
        result = api.post_mail_oauth(
            session_id=session_id,
            service=provider,
            email=email.strip(),
            refresh_token=refresh_token.strip(),
            client_id=client_id.strip(),
            client_secret=client_secret.strip(),
            scopes=scopes.strip(),
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Mail credential yazilamadi: {exc}")
        return

    vault_path = str(result.get("vault_path") or "")
    st.session_state[f"credential_{provider}"] = SimpleNamespace(vault_path=vault_path)
    bound = set(st.session_state.get("bound_credentials") or set())
    bound.add(provider)
    st.session_state["bound_credentials"] = bound
    st.success(f"{provider.title()} bu oturum icin baglandi.")


st.divider()
_render_mail_oauth_form()

st.info(
    "OpenAI/vLLM secimi ve API key baglantisi Admin Dashboard > LLM Providers "
    "ekranindan yapilir; bu sayfada LLM credential tutulmaz."
)
