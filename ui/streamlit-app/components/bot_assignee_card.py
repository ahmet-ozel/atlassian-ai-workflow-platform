"""Bot assignee info card for the Task Creator page (R7.1-R7.4).

Validates Requirements:
    * R7.1 - Dept switcher altında bot assignee bilgi kartı render edilir;
      display_name, bot_username, account_id (kısaltılmış), probe rozeti.
    * R7.2 - Kart başlığı: "Bu task açıldığında atayın:"; account_id
      üzerine tıklandığında pano'ya tam değer kopyalanır.
    * R7.3 - Hiç credential yoksa kırmızı uyarı + Credentials page link.
    * R7.4 - Credential var ama probe fail ise sarı uyarı + admin-dashboard
      /security deep link.

The component fetches data from ``GET /api/dept/{id}/bot-info`` via an
injected API client on ``st.session_state["_bot_info_api"]``. The client
must expose a ``get_bot_info(dept_id: str) -> dict | None`` method.

Response shape expected::

    {
        "display_name": "Payment Team",
        "bots": [
            {
                "service": "jira",
                "username": "payment-ai-bot",
                "account_id": "5fc9e78dabcdef1234567890",
                "probe_status": "ok",
                "probed_at": "2024-01-15T10:30:00Z"
            },
            ...
        ]
    }

Tri-state rendering:
  (a) All credentials present + probe ok → green badges.
  (b) No credentials at all → red warning + Credentials page link.
  (c) Credentials exist but probe failed → yellow warning + admin-dashboard
      /security deep link.
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st

__all__ = ["render_bot_assignee_card"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Services that the card checks for probe status.
_KNOWN_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")

#: How many characters of account_id to show before truncation.
_ACCOUNT_ID_DISPLAY_LEN: int = 8

#: Admin dashboard security page deep link (configurable via env).
_ADMIN_DASHBOARD_SECURITY_URL: str = "/security"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate_account_id(account_id: str | None) -> str:
    """Return first 8 chars + ellipsis, or placeholder if empty."""
    if not account_id:
        return "-"
    if len(account_id) <= _ACCOUNT_ID_DISPLAY_LEN:
        return account_id
    return f"{account_id[:_ACCOUNT_ID_DISPLAY_LEN]}…"


def _probe_badge(probe_status: str) -> str:
    """Return ✅ for ok, ❌ for anything else."""
    if probe_status == "ok":
        return "✅"
    return "❌"


def _fetch_bot_info(dept_id: str) -> dict[str, Any] | None:
    """Fetch bot info from the assistant-service API.

    Uses the injected client on session state. Returns None if the
    client is not configured or the request fails.
    """
    api = st.session_state.get("_bot_info_api")
    if api is None:
        return None
    try:
        return api.get_bot_info(dept_id)
    except Exception:  # noqa: BLE001 - best-effort; card is informational
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_bot_assignee_card(dept_id: str) -> dict[str, Any] | None:
    """Render the bot assignee info card below the dept switcher.

    Args:
        dept_id: The currently selected department ID.

    Returns:
        The bot info dict if successfully fetched (useful for prompt
        injection downstream), or None if unavailable.
    """
    data = _fetch_bot_info(dept_id)

    if data is None:
        st.warning(
            "Bot bilgisi yüklenemedi - assistant-service bağlantısını kontrol edin."
        )
        return None

    display_name: str = data.get("display_name", dept_id)
    bots: list[Mapping[str, Any]] = data.get("bots") or []

    # ------------------------------------------------------------------
    # State (c): No credentials at all → red warning
    # ------------------------------------------------------------------
    if not bots:
        st.error(
            "⚠️ Bu departmanda henüz bot credential'ı yapılandırılmamış. "
            "Önce **Credentials** sayfasına gidin."
        )
        st.page_link(
            "pages/0_credentials.py",
            label="🔑 Credentials sayfasına git",
        )
        return data

    # ------------------------------------------------------------------
    # Determine probe statuses
    # ------------------------------------------------------------------
    has_probe_failure = any(
        bot.get("probe_status") not in ("ok", "not_probed")
        for bot in bots
    )

    # ------------------------------------------------------------------
    # Render the card
    # ------------------------------------------------------------------
    with st.container():
        st.markdown("#### 🤖 Bu task açıldığında atayın:")

        # Pick the primary bot (prefer jira, fallback to first)
        primary_bot = next(
            (b for b in bots if b.get("service") == "jira"),
            bots[0],
        )

        bot_username: str = primary_bot.get("username", "-")
        account_id: str = primary_bot.get("account_id") or ""

        # Display name + username
        st.markdown(f"**{display_name}** - `{bot_username}`")

        # Account ID with copy button
        truncated_id = _truncate_account_id(account_id)
        st.markdown(f"Account ID: `{truncated_id}`")
        if account_id:
            st.code(account_id, language=None)

        # Probe badges per service
        badge_parts: list[str] = []
        for bot in bots:
            service = bot.get("service", "?")
            probe_status = bot.get("probe_status", "not_probed")
            badge = _probe_badge(probe_status)
            badge_parts.append(f"{badge} {service}")

        st.markdown(" · ".join(badge_parts))

        # ------------------------------------------------------------------
        # State (b): Credential exists but probe failed → yellow warning
        # ------------------------------------------------------------------
        if has_probe_failure:
            st.warning(
                "⚠️ Credential mevcut ama bağlantı testi başarısız - "
                "admin-dashboard'dan re-probe çalıştırın."
            )
            admin_url = st.session_state.get(
                "_admin_dashboard_url", _ADMIN_DASHBOARD_SECURITY_URL
            )
            security_url = (
                f"{admin_url}{_ADMIN_DASHBOARD_SECURITY_URL}"
                if not admin_url.endswith("/security")
                else admin_url
            )
            st.markdown(
                f"[🔗 Admin Dashboard Security]({security_url})"
            )

    return data
