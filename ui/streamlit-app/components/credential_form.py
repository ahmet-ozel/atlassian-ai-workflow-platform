"""Per-user Atlassian credential form (`platform-mimari-ops` task 9.7).

Validates Requirements:
    * R3.4 — credential is collected through the UI and POSTed to
      assistant-service, which writes to
      ``vault:atlassian/_user_session/<session_id>/<service>``.
    * R8.4 — plain credential value never leaves the form's local
      scope: ``clear_on_submit=True`` scrubs the DOM, the function
      returns only the Vault reference (path), and PIN-encrypted
      persistence (Z7) is **opt-in** behind a checkbox.
    * Property 4 — credential session isolation: two distinct
      ``session_id`` values map to two distinct Vault paths so
      dept-A's user cannot read dept-B's user's session credential.

The form is small on purpose: heavy lifting (HTTP POST, Vault write,
PIN-derived encryption) lives behind a callable injected through
``st.session_state["_credential_api"]``. The seam keeps unit tests
hermetic and matches the scaffold pattern used by the dept switcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import streamlit as st

__all__ = ["CredentialFormResult", "render_credential_form"]


#: Atlassian services the form supports. Mirrors the
#: ``vault:atlassian/_user_session/<session_id>/<service>`` path
#: layout from design.md §"DeptSwitcher / CredentialForm".
_SERVICES: Final[tuple[str, ...]] = ("jira", "bitbucket", "confluence")


@dataclass(frozen=True, slots=True)
class CredentialFormResult:
    """Return value of :func:`render_credential_form`.

    Attributes:
        vault_path: Stable reference to where the secret was stored
            (``vault:atlassian/_user_session/<session_id>/<service>``).
            The plain credential is **never** returned — only this
            reference, which downstream MCP calls dereference at
            request time.
        service: Which Atlassian surface this credential targets.
        persisted_with_pin: ``True`` when the user opted into the
            PIN-encrypted persistent path (Z7). ``False`` for the
            default session-scoped path.
    """

    vault_path: str
    service: str
    persisted_with_pin: bool


def render_credential_form(
    dept_id: str,
    session_id: str,
    service: str | None = None,
) -> CredentialFormResult | None:
    """Render the credential form and POST on submit.

    The form uses ``clear_on_submit=True`` so the input fields are
    scrubbed from the DOM as soon as the user clicks the submit
    button — this is the technical mechanism that delivers R8.4 in
    Streamlit. The plain values flow into the injected
    ``_credential_api.post`` callable and never bind to a
    ``session_state`` key.

    Args:
        dept_id: Active department id (from
            :func:`render_dept_switcher`). Combined with
            ``session_id`` to scope the Vault path.
        session_id: Stable per-session token (typically a UUID
            minted at login). Determines the
            ``_user_session/<session_id>/<service>`` Vault path
            segment.
        service: Optional Atlassian service to bind this form to
            (one of :data:`_SERVICES`). When provided the inline
            "Servis" selectbox is omitted and widget keys are
            scoped per service so the form can be rendered multiple
            times on the same page (one per service tab) without
            triggering Streamlit's ``DuplicateWidgetID`` guard. When
            ``None`` the legacy single-form behaviour is preserved
            (selectbox shown, single set of widget keys) — keeps
            existing call sites such as ``pages/1_chat.py`` working
            unchanged.

    Returns:
        :class:`CredentialFormResult` on a successful submit;
        ``None`` while the user has not yet submitted.
    """

    if not dept_id or not session_id:
        st.warning(
            "Credential form için aktif departman ve geçerli oturum gerekli."
        )
        return None

    if service is not None and service not in _SERVICES:
        st.error(
            f"Bilinmeyen servis: {service!r}. Beklenen: {_SERVICES}."
        )
        return None

    api = st.session_state.get("_credential_api")
    if api is None:
        st.error(
            "Credential API yapılandırılmamış. "
            "(`session_state['_credential_api']` eksik.)"
        )
        return None

    # Per-service widget keys keep multiple form instances disjoint;
    # the single-form legacy behaviour reuses the original keys.
    key_suffix = f"_{service}" if service is not None else ""
    form_key = f"user_credential{key_suffix}"

    with st.form(form_key, clear_on_submit=True):
        st.markdown("##### Atlassian bağlantısı")
        if service is None:
            selected_service = st.selectbox(
                "Servis", _SERVICES, index=0, key="cred_service"
            )
        else:
            selected_service = service
        default_url = (
            "https://bitbucket.org"
            if selected_service == "bitbucket"
            else "https://firma.atlassian.net"
        )
        url = st.text_input(
            "Atlassian URL",
            placeholder=default_url,
            key=f"cred_url{key_suffix}",
        )
        email = st.text_input(
            "Atlassian e-posta",
            placeholder="ad.soyad@firma.com",
            key=f"cred_email{key_suffix}",
        )
        token = st.text_input(
            "API Token",
            type="password",
            help=(
                "Atlassian API token (kişisel). Oturum sonu Vault'tan "
                "silinir; PIN ile saklamak isterseniz aşağıdaki kutuyu "
                "işaretleyin."
            ),
            key=f"cred_token{key_suffix}",
        )
        persist = st.checkbox(
            "Bu cihazda PIN ile sakla (opsiyonel)",
            value=False,
            help=(
                "Açık tutulduğunda credential, oturum kapanmadan "
                "sonra da PIN-şifreli olarak saklanır (Z7). "
                "Varsayılan olarak yalnızca aktif oturumda geçerlidir."
            ),
            key=f"cred_persist{key_suffix}",
        )
        pin: str | None = None
        if persist:
            pin = st.text_input(
                "PIN (4-6 hane)",
                type="password",
                max_chars=6,
                key=f"cred_pin{key_suffix}",
            )
        submitted = st.form_submit_button("Bağla")

    if not submitted:
        return None

    if not url or not (url.startswith("https://") or url.startswith("http://")):
        st.error("GeÃ§erli bir Atlassian URL giriniz.")
        return None

    # Defensive validation — empty fields raise inline.
    if not email or "@" not in email:
        st.error("Geçerli bir e-posta giriniz.")
        return None
    if not token:
        st.error("API token boş olamaz.")
        return None
    if persist and (pin is None or not pin.isdigit() or not (4 <= len(pin) <= 6)):
        st.error("PIN 4-6 haneli sayı olmalıdır.")
        return None

    try:
        # ``api.post`` returns a Vault reference; the plain value is
        # passed positionally and never bound to a session_state key.
        ref = api.post(
            dept_id=dept_id,
            session_id=session_id,
            service=selected_service,
            url=url.strip(),
            email=email,
            api_token=token,
            persist_with_pin=pin if persist else None,
        )
    except Exception as exc:  # noqa: BLE001 — surface API errors inline
        st.error(f"Credential kaydedilemedi: {exc}")
        return None

    result = CredentialFormResult(
        vault_path=str(ref.get("vault_path", "")),
        service=selected_service,
        persisted_with_pin=bool(persist),
    )
    st.session_state[f"credential_{selected_service}"] = result
    st.success("Credential bağlandı.")
    return result
