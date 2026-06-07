"""Bot identity card component for the Task Creator page (R8.2-R8.9).

Validates Requirements:
    * R8.2 - Info card: dept display_name, bot username, account_id
      (kopyalanabilir kod bloğu), probe status badge.
    * R8.3 - Jira bot yoksa uyarı banner.
    * R8.4 - probe_status not_probed/failed → badge + "Probe çalıştır" butonu.
    * R8.5 - "Probe çalıştır" butonu POST /admin/departments/{id}/probe.
    * R8.8 - Graceful degradation: 503/timeout/network → warning + retry.
    * R8.9 - Diğer bot'lar (Bitbucket, Confluence) küçük tabloda listelenir.

This component makes a direct HTTP GET request to
``{api_base}/api/dept/{dept_id}/bot-info`` with a 5-second timeout.
On success it renders the identity card and returns the Jira bot's
``account_id`` for Assignee pre-fill. On any error it shows a
warning with a retry mechanism and returns None.
"""

from __future__ import annotations

from typing import Any

import httpx
import streamlit as st

__all__ = ["render_bot_identity_card"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: HTTP timeout for the bot-info endpoint (seconds).
_BOT_INFO_TIMEOUT_S: float = 5.0

#: Badge mapping for probe status.
_PROBE_BADGE: dict[str, str] = {
    "ok": "🟢",
    "failed": "🔴",
    "not_probed": "🟡",
}

#: Default badge for unknown probe statuses.
_PROBE_BADGE_DEFAULT: str = "⚪"

#: Warning message shown when the bot-info endpoint is unreachable.
_DEGRADATION_WARNING: str = "Bot bilgileri yüklenemedi (yeniden dene)"

#: Warning message shown when no Jira bot credential is bound.
_NO_JIRA_BOT_WARNING: str = (
    "Bu departman için Jira bot credential'ı eklenmemiş. "
    "Önce Credentials sayfasından bind edin."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_probe_badge(probe_status: str) -> str:
    """Return the emoji badge for a given probe status."""
    return _PROBE_BADGE.get(probe_status, _PROBE_BADGE_DEFAULT)


def _fetch_bot_info(dept_id: str, api_base: str) -> dict[str, Any] | None:
    """Fetch bot info from the assistant-service API.

    Makes a GET request to ``{api_base}/api/dept/{dept_id}/bot-info``
    with a 5-second timeout. Returns the parsed JSON on success,
    or None on any error (503, timeout, network error, non-200).
    """
    url = f"{api_base}/api/dept/{dept_id}/bot-info"
    try:
        resp = httpx.get(url, timeout=_BOT_INFO_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return None


def _run_probe(dept_id: str, api_base: str) -> dict[str, Any] | None:
    """POST to the probe endpoint and return the result.

    Calls ``{api_base}/admin/departments/{dept_id}/probe`` to trigger
    a connectivity probe for the department's Jira bot.
    """
    url = f"{api_base}/admin/departments/{dept_id}/probe"
    try:
        resp = httpx.post(url, timeout=_BOT_INFO_TIMEOUT_S)
        if resp.status_code == 200:
            return resp.json()
        return None
    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_bot_identity_card(dept_id: str, api_base: str) -> str | None:
    """Bot identity card render et. Jira bot varsa account_id döndür.

    Args:
        dept_id: The currently selected department ID.
        api_base: Base URL of the assistant-service (e.g.
            ``http://assistant-service:8081``).

    Returns:
        The Jira bot's ``account_id`` string if a Jira bot is found
        and the endpoint responds successfully, or ``None`` otherwise.
    """
    data = _fetch_bot_info(dept_id, api_base)

    # ------------------------------------------------------------------
    # Graceful degradation: endpoint unreachable / error
    # ------------------------------------------------------------------
    if data is None:
        st.warning(_DEGRADATION_WARNING)
        if st.button("🔄 Yeniden dene", key=f"retry_bot_info_{dept_id}"):
            st.rerun()
        return None

    display_name: str = data.get("display_name", dept_id)
    bots: list[dict[str, Any]] = data.get("bots") or []

    # ------------------------------------------------------------------
    # No Jira bot credential bound
    # ------------------------------------------------------------------
    jira_bot: dict[str, Any] | None = next(
        (b for b in bots if b.get("service") == "jira"), None
    )

    if jira_bot is None:
        st.warning(_NO_JIRA_BOT_WARNING)
        # Still show other bots if available
        _render_other_bots_table(bots)
        return None

    # ------------------------------------------------------------------
    # Render the identity card
    # ------------------------------------------------------------------
    account_id: str = jira_bot.get("account_id", "")
    username: str = jira_bot.get("username", "-")
    probe_status: str = jira_bot.get("probe_status", "not_probed")
    badge: str = _get_probe_badge(probe_status)

    st.info(f"🤖 **{display_name}** Bot Bilgileri")

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown(f"**Username:** {username}")
        st.code(account_id, language=None)
        st.markdown(f"**Probe:** {badge} {probe_status}")
    with col2:
        if probe_status in ("not_probed", "failed"):
            if st.button("Probe çalıştır", key=f"probe_{dept_id}"):
                result = _run_probe(dept_id, api_base)
                if result:
                    st.success("Probe tamamlandı - sayfa yenileniyor...")
                    st.rerun()
                else:
                    st.error("Probe başarısız oldu.")

    # ------------------------------------------------------------------
    # Other bots table (Bitbucket, Confluence) - reference only
    # ------------------------------------------------------------------
    other_bots = [b for b in bots if b.get("service") != "jira"]
    _render_other_bots_table(other_bots)

    return account_id


def _render_other_bots_table(bots: list[dict[str, Any]]) -> None:
    """Render a small reference table for non-Jira bots."""
    if not bots:
        return
    st.markdown("**Diğer Bot'lar:**")
    table_data = [
        {
            "Servis": b.get("service", "-"),
            "Username": b.get("username", "-"),
            "Durum": _get_probe_badge(b.get("probe_status", "not_probed")),
        }
        for b in bots
    ]
    st.table(table_data)
