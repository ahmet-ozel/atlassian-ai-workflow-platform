"""Streamlit Orphan Branches page (`platform-mimari-ops` task 9.5).

**Validates: Requirements 3.1**

Surfaces ``ai/{issue_key}`` branches whose parent Jira issue has
been closed or no longer exists — flagged as candidates for the
``BotBranchRetention`` cleanup workflow (Spec 2). The user can
trigger the deletion sweep from this page; the actual work runs
inside the existing Temporal workflow so audit + rollback are
preserved.

Also surfaces runner workspaces with ``cleanup_policy=never`` that
are candidates for manual cleanup (Q15 purge endpoint integration).
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components import render_cost_widget, render_dept_switcher
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(
    page_title="Orphan Branches", page_icon="🧹", layout="wide"
)
render_user_navigation()
apply_theme()
page_hero(
    "Orphan Branches & Workspace temizliği",
    "Kapanmış Jira issue'larına bağlı kalan ai/{issue_key} branch'leri ve "
    "cleanup_policy=never ile oluşturulmuş runner workspace'leri burada "
    "listelenir. Manuel temizlik audit log'una düşer.",
    icon="🧹",
)

dept_id = render_dept_switcher()
render_cost_widget()


admin_client = st.session_state.get("_admin_api_client")
if admin_client is None:
    st.error(
        "Admin API client yapılandırılmamış. "
        "(`session_state['_admin_api_client']` eksik.)"
    )
    st.stop()


# ---------------------------------------------------------------------------
# Tab 1: Orphan Branches
# ---------------------------------------------------------------------------

tab_branches, tab_workspaces = st.tabs(
    ["🌿 Orphan Branches", "📁 Runner Workspaces"]
)

with tab_branches:
    if st.button("Tara", key="orphan_scan"):
        try:
            rows = admin_client.list_orphan_branches(dept_id=dept_id)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Tarama başarısız: {exc}")
            rows = []
        st.session_state["_orphan_rows"] = rows

    rows: list[dict[str, Any]] = (
        st.session_state.get("_orphan_rows", []) or []
    )

    if not rows:
        st.info(
            "Henüz tarama yapılmadı ya da orphan branch bulunamadı. "
            "'Tara' düğmesi ile yeniden başlatabilirsiniz."
        )
    else:
        st.caption(
            f"{len(rows)} candidate branch found — closed Jira issues "
            "ya da silinmiş issue'larla eşleşen `ai/{issue_key}` dalları."
        )
        for row in rows[:200]:
            branch = row.get("branch_name", "?")
            repo = row.get("repo", "?")
            last_commit = row.get("last_commit_at", "")
            issue_key = row.get("issue_key", "?")
            issue_status = row.get("issue_status", "(unknown)")

            with st.expander(f"`{repo}` / `{branch}`  ({issue_key})"):
                st.write(
                    f"Last commit: `{last_commit}`  •  "
                    f"Issue status: `{issue_status}`"
                )
                if st.button(
                    "Sil (workflow ile)",
                    key=f"orphan_delete_{repo}_{branch}",
                ):
                    try:
                        admin_client.trigger_orphan_branch_deletion(
                            repo=repo, branch=branch
                        )
                        st.success(
                            "BotBranchRetention workflow'u tetiklendi; "
                            "audit log'unda durumu izleyebilirsiniz."
                        )
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Silme başlatılamadı: {exc}")

# ---------------------------------------------------------------------------
# Tab 2: Runner Workspaces (cleanup_policy=never candidates)
# ---------------------------------------------------------------------------

with tab_workspaces:
    st.subheader("Runner Workspaces (Manuel Temizlik)")
    st.caption(
        "Aşağıdaki workspace'ler `cleanup_policy=never` ile oluşturulmuş "
        "ve runner üzerinde hâlâ mevcut. İnceleme tamamlandıysa "
        "'Manuel Temizle' ile silebilirsiniz."
    )

    if st.button("Workspace'leri Listele", key="ws_list"):
        try:
            ws_data = admin_client.list_runner_workspaces()
            st.session_state["_ws_rows"] = ws_data.get("workspaces", [])
        except Exception as exc:  # noqa: BLE001
            st.error(f"Workspace listesi alınamadı: {exc}")
            st.session_state["_ws_rows"] = []

    ws_rows: list[dict[str, Any]] = (
        st.session_state.get("_ws_rows", []) or []
    )

    if not ws_rows:
        st.info(
            "Henüz workspace bulunamadı ya da listelenmedi. "
            "'Workspace'leri Listele' düğmesine tıklayın."
        )
    else:
        st.write(f"**{len(ws_rows)}** workspace bulundu:")
        for ws in ws_rows:
            issue_key = ws.get("issue_key", "?")
            size_mb = ws.get("size_mb", 0)
            last_modified = ws.get("last_modified", "?")

            col1, col2, col3 = st.columns([3, 2, 2])
            col1.write(f"**{issue_key}**")
            col2.write(f"{size_mb} MB • {last_modified}")
            if col3.button(
                "Manuel Temizle", key=f"ws_purge_{issue_key}"
            ):
                try:
                    result = admin_client.purge_runner_workspace(
                        issue_key=issue_key
                    )
                    freed = result.get("freed_bytes", 0)
                    freed_mb = freed // (1024 * 1024) if freed else 0
                    st.success(
                        f"✅ `{issue_key}` temizlendi "
                        f"({freed_mb} MB serbest bırakıldı)."
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Temizlik başarısız: {exc}")
