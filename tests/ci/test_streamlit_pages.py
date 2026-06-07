"""CI gate - Streamlit page catalog.

end-user pages (chat / task creator / workflows / po review inbox) plus
admin/ops-only pages (explorer / orphan branches / MCP inspector) and the
three shared components
(``dept_switcher``, ``credential_form``, ``cost_widget``) are all
present.

the canonical per-user credentials page lives at ``pages/0_credentials.py``
and re-uses ``render_credential_form`` via import. The legacy
``pages/7_session_credentials.py`` slot MUST NOT exist; its sole
sidebar replacement is ``0_credentials.py`` (the architecture document §16.17 Q6).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_STREAMLIT_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
)

_REQUIRED_PAGES: tuple[str, ...] = (
    "1_chat.py",
    "2_task_creator.py",
)

_ADMIN_ONLY_PAGES: tuple[str, ...] = (
    "3_explorer.py",
    "7_mcp_inspector.py",
)

#: Pages that moved OUT of Streamlit into the admin dashboard
#: (governance surfaces that must be admin-gated, not exposed to
#: every credential-holding chat user). They MUST NOT exist under
#: ``pages/`` anymore.
_MOVED_TO_ADMIN_DASHBOARD: tuple[str, ...] = (
    "4_workflows.py",
    "5_orphan_branches.py",
    "6_po_review_inbox.py",
)

_REQUIRED_COMPONENTS: tuple[str, ...] = (
    "dept_switcher.py",
    "credential_form.py",
    "cost_widget.py",
)


@pytest.mark.parametrize("page", _REQUIRED_PAGES)
def test_streamlit_page_exists(page: str) -> None:
    path = _STREAMLIT_ROOT / "pages" / page
    assert path.is_file(), (
        f"Missing Streamlit page {page!r}; the Streamlit app must ship all "
        "six pages."
    )


@pytest.mark.parametrize("page", _ADMIN_ONLY_PAGES)
def test_streamlit_admin_only_page_exists(page: str) -> None:
    path = _STREAMLIT_ROOT / "pages" / page
    assert path.is_file(), (
        f"Missing admin-only Streamlit debug page {page!r}; it is linked "
        "from admin-dashboard, not the normal user menu."
    )


def test_streamlit_default_sidebar_navigation_disabled() -> None:
    config = _STREAMLIT_ROOT / ".streamlit" / "config.toml"
    assert config.is_file(), (
        "Streamlit default navigation must be disabled so admin/debug pages "
        "do not appear in the normal user menu."
    )
    body = config.read_text(encoding="utf-8").replace(" ", "").lower()
    assert "showsidebarnavigation=false" in body


def test_user_navigation_excludes_admin_debug_pages() -> None:
    app_source = (_STREAMLIT_ROOT / "app.py").read_text(encoding="utf-8")
    assert "pages/3_explorer.py" not in app_source
    assert "pages/7_mcp_inspector.py" not in app_source
    assert "render_user_navigation" in app_source


@pytest.mark.parametrize("page", _MOVED_TO_ADMIN_DASHBOARD)
def test_governance_pages_moved_to_admin_dashboard(page: str) -> None:
    """Workflows / Orphan Branches / PO Review left the Streamlit app.

 These are governance surfaces that must be gated by admin auth in
 the admin dashboard, not exposed to every credential-holding chat
 user. The Streamlit ``pages/`` slot MUST be gone and the user nav
 MUST NOT reference them.
 """
    path = _STREAMLIT_ROOT / "pages" / page
    assert not path.is_file(), (
        f"Streamlit page {page!r} must be removed; it moved to the "
        "admin dashboard (admin-gated governance surface)."
    )
    app_source = (_STREAMLIT_ROOT / "app.py").read_text(encoding="utf-8")
    assert f"pages/{page}" not in app_source, (
        f"app.py still references pages/{page}; drop the nav/card entry."
    )


@pytest.mark.parametrize("component", _REQUIRED_COMPONENTS)
def test_streamlit_component_exists(component: str) -> None:
    path = _STREAMLIT_ROOT / "components" / component
    assert path.is_file(), (
        f"Missing Streamlit component {component!r}; tasks 9.6/9.7/9.8 "
        "ship these so every page can compose them."
    )
    body = path.read_text(encoding="utf-8")
    assert len(body) > 100, (
        f"Component {component!r} is too short ({len(body)} bytes) "
        "to be a real implementation."
    )


# --------------------------------------------------------------------------- #
# uyumluluk per-user credentials page #
# --------------------------------------------------------------------------- #

_CREDENTIALS_PAGE = _STREAMLIT_ROOT / "pages" / "0_credentials.py"
_LEGACY_CREDENTIALS_PAGE = _STREAMLIT_ROOT / "pages" / "7_session_credentials.py"


def test_zero_credentials_page_exists() -> None:
    """``pages/0_credentials.py`` is the canonical credentials entry point.

 The ``0_`` numeric prefix forces Streamlit to render this page first
 in the sidebar, so a freshly-onboarded user always lands on
 credential setup before any workflow page .
 """
    assert _CREDENTIALS_PAGE.is_file(), (
        f"Missing per-user credentials page {_CREDENTIALS_PAGE.name!r}; "
        "requires the canonical credentials page at "
        "pages/0_credentials.py (the architecture document §16.17 Q6)."
    )


def test_zero_credentials_page_imports_render_credential_form() -> None:
    """``0_credentials.py`` MUST reuse ``render_credential_form`` via import.

 forbids copying the legacy session-credential form logic; the
 page must consume the existing component so DOM scrubbing, Z7 PIN
 persistence and audit emission stay single-sourced.
 """
    body = _CREDENTIALS_PAGE.read_text(encoding="utf-8")
    assert "render_credential_form" in body, (
        "pages/0_credentials.py must import render_credential_form from "
        "components; mandates re-use rather than a copy."
    )
    # The import line itself must be present so a stray-string match
    # in a docstring doesn't satisfy the assertion.
    has_import = any(
        line.lstrip().startswith(("from components", "import components"))
        and "render_credential_form" in line
        for line in body.splitlines()
    )
    assert has_import, (
        "pages/0_credentials.py must contain a real import of "
        "render_credential_form (e.g. "
        "`from components import render_credential_form`)."
    )


def test_legacy_session_credentials_page_removed() -> None:
    """The pre-uyumluluk ``pages/7_session_credentials.py`` slot is gone.

 collapses credential management onto a single sidebar entry
 (``0_credentials.py``); leaving the legacy page in place would
 create two competing UX surfaces for the same Vault path.
 """
    assert not _LEGACY_CREDENTIALS_PAGE.exists(), (
        "Legacy pages/7_session_credentials.py must be removed; "
        "routes all per-user credential UX through "
        "pages/0_credentials.py."
    )
