"""Streamlit PO Review Inbox page (`platform-mimari-ops` task 9.5).

**Validates: Requirements 3.1**

Lists the bot-opened draft PRs that are pending PO / QA review.
Each row links out to the Jira issue and the Bitbucket PR. Inline
actions:

* **Approve** — signal the workflow to mark the iteration accepted
  and proceed to merge.
* **Request changes** — signal the workflow to bounce back to the
  agent with the supplied comment text.
* **Reject** — signal the workflow to abandon the task and emit a
  ``rejected`` audit event.

The page does NOT call Bitbucket / Jira directly — every action is
a Temporal signal sent through ``automation-service``, which keeps
audit / RBAC / capability gating consistent with the rest of the
workflow surface.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components import render_cost_widget, render_dept_switcher
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(
    page_title="PO Review Inbox", page_icon="📥", layout="wide"
)
render_user_navigation()
apply_theme()
page_hero(
    "PO Review Inbox",
    "Bot tarafından açılan draft PR'lar burada incelemenizi bekler. "
    "Onay, değişiklik talebi ve red işlemleri Temporal workflow "
    "sinyallerine dönüşür; tüm aksiyonlar audit log'a yazılır.",
    icon="📥",
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


if st.button("Yenile", key="po_refresh"):
    try:
        rows = admin_client.list_po_review_requests(dept_id=dept_id)
    except Exception as exc:  # noqa: BLE001
        st.error(f"İnceleme listesi alınamadı: {exc}")
        rows = []
    st.session_state["_po_rows"] = rows


rows: list[dict[str, Any]] = (
    st.session_state.get("_po_rows", []) or []
)

if not rows:
    st.info("Bekleyen PO inceleme talebi yok ya da listelenmedi.")
else:
    for row in rows[:100]:
        wf_id = row.get("workflow_id", "?")
        title = row.get("title", "")
        jira_url = row.get("jira_issue_url", "")
        pr_url = row.get("pr_url", "")
        iter_count = int(row.get("iteration_count", 0))

        header = f"`{wf_id}`  •  {title}"
        with st.expander(header):
            cols = st.columns(2)
            if jira_url:
                cols[0].markdown(f"[Jira issue]({jira_url})")
            if pr_url:
                cols[1].markdown(f"[Bitbucket PR]({pr_url})")

            if iter_count >= 3:
                st.warning(
                    f"⚠️ iter≥3 ({iter_count}) — task yeniden scope'lansın mı?"
                )

            st.markdown("**İncele:**")

            with st.form(f"po_decision_{wf_id}", clear_on_submit=True):
                comment = st.text_area(
                    "Yorum / değişiklik talebi",
                    height=100,
                    key=f"po_comment_{wf_id}",
                )
                col_a, col_b, col_r = st.columns(3)
                approve_clicked = col_a.form_submit_button("✅ Onayla")
                changes_clicked = col_b.form_submit_button("✏️ Değişiklik iste")
                reject_clicked = col_r.form_submit_button("❌ Reddet")

            if approve_clicked:
                try:
                    admin_client.po_decision(
                        workflow_id=wf_id, decision="approve", comment=comment
                    )
                    st.success("Onaylandı; merge sinyali gönderildi.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Onay başarısız: {exc}")
            elif changes_clicked:
                if not comment.strip():
                    st.error("Değişiklik talebi için yorum gerekli.")
                else:
                    try:
                        admin_client.po_decision(
                            workflow_id=wf_id,
                            decision="changes_requested",
                            comment=comment,
                        )
                        st.success("Değişiklik talebi iletildi.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"İletim başarısız: {exc}")
            elif reject_clicked:
                try:
                    admin_client.po_decision(
                        workflow_id=wf_id, decision="reject", comment=comment
                    )
                    st.success("Reddedildi; workflow kapatıldı.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Red başarısız: {exc}")
