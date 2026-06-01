"""Streamlit shared components for the platform-mimari-ops UI.

Public surface (re-exported here so pages can ``from components import ...``):

* :func:`render_dept_switcher` — mandatory dept dropdown in the sidebar
  (R3.11, R3.12, R7.1, R7.2, R7.3 / Property 12).
* :func:`render_credential_form` — per-user Atlassian credential form
  with ``clear_on_submit=True`` and opt-in PIN-encrypted persistence
  (R3.4, R8.4).
* :func:`render_cost_widget` — end-user cost / quota sidebar widget
  (R5.8, N4 backlog).
* :class:`CredentialManager` / :func:`render_credential_manager` —
  in-memory-only Atlassian credential lifecycle manager with 60-minute
  inactivity timeout, MCP validation, and explicit logout
  (platform-gap-fill task 12.1, R13.1–R13.6).
* :func:`render_bot_assignee_card` — bot assignee info card for the
  Task Creator page (R7.1–R7.4).
* :func:`render_bot_identity_card` — bot identity card with probe
  status, badge colors, and graceful degradation (R8.2–R8.9).
"""

from .bot_assignee_card import render_bot_assignee_card
from .bot_identity_card import render_bot_identity_card
from .cost_widget import render_cost_widget
from .credential_form import CredentialFormResult, render_credential_form
from .credential_manager import (
    CREDENTIAL_WARNING_TEXT,
    CredentialManager,
    StoredCredential,
    render_credential_manager,
    render_credential_warning,
    render_logout_button,
)
from .dept_switcher import render_dept_switcher
from .theme import apply_theme, kpi_row, page_hero, section_header

__all__ = [
    "CREDENTIAL_WARNING_TEXT",
    "CredentialFormResult",
    "CredentialManager",
    "StoredCredential",
    "apply_theme",
    "kpi_row",
    "page_hero",
    "render_bot_assignee_card",
    "render_bot_identity_card",
    "render_cost_widget",
    "render_credential_form",
    "render_credential_manager",
    "render_credential_warning",
    "render_dept_switcher",
    "render_logout_button",
    "section_header",
]
