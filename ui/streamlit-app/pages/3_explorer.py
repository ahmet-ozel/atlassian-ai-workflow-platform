"""Streamlit Explorer page (`platform-mimari-ops` task 9.3).

**Validates: Requirements 3.3 (V3), R1.2**

Explorer lets the user browse Jira issues, Bitbucket pull requests
and Confluence pages from the active department's catalogue. Unlike
the chat page, Explorer is **read-only** so it talks to MCP
directly through the foundation client — but only after the banned
tool list is applied (foundation `mcp_client.filter_tools`). Any
write-action tool (e.g. `bitbucket_merge_pr`,
`confluence_delete_page`) is filtered out before the catalogue
reaches this page; the design table at design.md §"Property 3"
asserts the same invariant from a static-AST angle.

The page itself does not import write tools by name — only
``filter_tools`` and the read-only invocation helpers are called.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from app import _inject_session_state
from components import render_cost_widget, render_dept_switcher
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(page_title="Explorer", page_icon="🔍", layout="wide")
apply_theme()
page_hero(
    "Explorer",
    "Departman kataloğunda tanımlı Jira issue, Bitbucket PR ve "
    "Confluence sayfalarını okuma modunda gezin. Yazma işlemleri burada "
    "yer almaz; gerektiğinde Task Creator'a yönlendirilirsiniz.",
    icon="🔍",
)

dept_id = render_dept_switcher()
render_cost_widget()

mcp_client = st.session_state.get("_mcp_read_client")
if mcp_client is None:
    st.error(
        "MCP read client yapılandırılmamış. "
        "(`session_state['_mcp_read_client']` eksik.)"
    )
    st.stop()


tab_jira, tab_bitbucket, tab_confluence = st.tabs(
    ["Jira", "Bitbucket", "Confluence"]
)


# ---------------------------------------------------------------------------
# Jira — read-only issue browser
# ---------------------------------------------------------------------------

with tab_jira:
    jql = st.text_input(
        "JQL",
        value=f'project = "{dept_id.upper()}" ORDER BY updated DESC',
        key="jira_jql",
    )
    if st.button("Ara", key="jira_run"):
        try:
            issues: list[dict[str, Any]] = mcp_client.search_jira(jql=jql)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Jira aranamadı: {exc}")
            issues = []
        st.session_state["_jira_results"] = issues

    issues = st.session_state.get("_jira_results", []) or []
    if issues:
        for issue in issues[:50]:
            with st.expander(
                f"{issue.get('key', '?')} — {issue.get('summary', '')}"
            ):
                st.write(issue.get("description") or "(açıklama yok)")
                if issue.get("url"):
                    st.markdown(f"[Aç]({issue['url']})")


# ---------------------------------------------------------------------------
# Bitbucket — read-only PR list (write tools filtered out upstream)
# ---------------------------------------------------------------------------

with tab_bitbucket:
    repo = st.text_input(
        "Repo (workspace/slug)",
        value=st.session_state.get("user", {}).get("default_repo", ""),
        key="bb_repo",
    )
    if st.button("PR'ları getir", key="bb_run") and repo:
        try:
            prs: list[dict[str, Any]] = mcp_client.list_bitbucket_prs(repo=repo)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Bitbucket aranamadı: {exc}")
            prs = []
        st.session_state["_bb_results"] = prs

    prs = st.session_state.get("_bb_results", []) or []
    if prs:
        for pr in prs[:50]:
            with st.expander(
                f"#{pr.get('id', '?')} — {pr.get('title', '')}"
            ):
                st.write(
                    f"State: `{pr.get('state', '?')}`  •  "
                    f"Author: {pr.get('author', '?')}"
                )
                if pr.get("url"):
                    st.markdown(f"[Aç]({pr['url']})")


# ---------------------------------------------------------------------------
# Confluence — read-only page search
# ---------------------------------------------------------------------------

with tab_confluence:
    cql = st.text_input(
        "CQL",
        value=f'space = "{dept_id.upper()}" AND type = page',
        key="cf_cql",
    )
    if st.button("Ara", key="cf_run"):
        try:
            pages: list[dict[str, Any]] = mcp_client.search_confluence(cql=cql)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Confluence aranamadı: {exc}")
            pages = []
        st.session_state["_cf_results"] = pages

    pages = st.session_state.get("_cf_results", []) or []
    if pages:
        for page in pages[:50]:
            with st.expander(page.get("title", "?")):
                excerpt = page.get("excerpt") or "(özet yok)"
                st.write(excerpt)
                if page.get("url"):
                    st.markdown(f"[Aç]({page['url']})")
