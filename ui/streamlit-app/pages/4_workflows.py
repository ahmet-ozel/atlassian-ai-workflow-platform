"""Streamlit Workflows page (`platform-mimari-ops` task 9.4).

**Validates: Requirements 3.1**

Lists the active department's Temporal workflows; the user can:

* cancel a running workflow (``automation-service`` signal);
* reply inline to a PO/QA review question (``po_review_request``
  workflow surface);
* see an ``iter≥3`` banner when the workflow has bounced back from
  PO review three or more times — the cap signal that suggests
  the human should re-scope the task instead of pushing another
  iteration;
* re-run a workflow with environment variable overrides (Feature 13);
* lead-role users can reply to any workflow in their department,
  not just reporter/assignee (Feature 16).

The page reads from the admin-dashboard-api proxy at
``/admin/workflows`` (BFF). The client is injected on
``st.session_state["_admin_api_client"]`` by the app boot.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from app import _inject_session_state, render_user_navigation
from components import render_cost_widget, render_dept_switcher
from components.theme import apply_theme, page_hero


_inject_session_state()
st.set_page_config(page_title="Workflows", page_icon="🔁", layout="wide")
render_user_navigation()
apply_theme()
page_hero(
    "Workflows",
    "Departmanın aktif Temporal workflow'ları. Çalışan iş akışlarını "
    "iptal edebilir, PO/QA review yorumu yazabilir veya environment "
    "override ile yeniden çalıştırabilirsiniz.",
    icon="🔁",
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
# Feature 16: Lead role detection
# ---------------------------------------------------------------------------

_user = st.session_state.get("user", {})
_user_role: str = _user.get("role", "viewer")
_is_lead: bool = _user_role == "lead" or "lead" in _user.get("roles", [])
_user_id: str = _user.get("account_id", _user.get("sub", ""))


# ---------------------------------------------------------------------------
# Feature 13: Sensitive env key rejection
# ---------------------------------------------------------------------------

#: Environment variable keys that are rejected in override forms.
_SENSITIVE_ENV_KEYS = frozenset({
    "SECRET_KEY",
    "DATABASE_URL",
    "DB_PASSWORD",
    "VAULT_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "PRIVATE_KEY",
    "API_SECRET",
    "ENCRYPTION_KEY",
    "JWT_SECRET",
    "OAUTH_CLIENT_SECRET",
})


def _validate_env_overrides(env_pairs: list[tuple[str, str]]) -> list[str]:
    """Validate env override pairs, returning list of error messages."""
    errors: list[str] = []
    for key, _value in env_pairs:
        normalized_key = key.strip().upper()
        if normalized_key in _SENSITIVE_ENV_KEYS:
            errors.append(
                f"'{key}' hassas bir ortam değişkenidir ve override edilemez."
            )
    return errors


# ---------------------------------------------------------------------------
# Workflow list
# ---------------------------------------------------------------------------

status_filter = st.selectbox(
    "Durum",
    ["all", "running", "completed", "failed", "partial"],
    index=0,
)

if st.button("Yenile", key="wf_refresh"):
    try:
        rows = admin_client.list_workflows(
            dept_id=dept_id, status=status_filter
        )
    except Exception as exc:  # noqa: BLE001
        st.error(f"Workflow listesi alınamadı: {exc}")
        rows = []
    st.session_state["_wf_rows"] = rows


rows: list[dict[str, Any]] = st.session_state.get("_wf_rows", []) or []
if not rows:
    st.info("Henüz workflow yok ya da yenilenmedi.")
else:
    for row in rows:
        wf_id = row.get("workflow_id", "?")
        status = row.get("status", "?")
        wf_type = row.get("workflow_type", "?")
        iter_count = int(row.get("iteration_count", 0))

        header = f"`{wf_id}`  •  {wf_type}  •  `{status}`"
        with st.expander(header):
            if iter_count >= 3:
                st.warning(
                    f"⚠️ iter≥3 ({iter_count}) — bu workflow yeniden "
                    "scope'lanması gerekebilir; kapatıp Task Creator'dan "
                    "daha küçük bir görev olarak açmayı düşünün."
                )

            st.write(row.get("summary", ""))
            if row.get("jira_issue_url"):
                st.markdown(f"[Jira issue]({row['jira_issue_url']})")

            col_cancel, col_reply, col_rerun = st.columns(3)

            # --- Cancel button ---
            if status == "running" and col_cancel.button(
                "İptal et", key=f"cancel_{wf_id}"
            ):
                try:
                    admin_client.cancel_workflow(workflow_id=wf_id)
                    st.success("İptal sinyali gönderildi.")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"İptal başarısız: {exc}")

            # --- Reply form (Feature 16: lead role override) ---
            # Original: only show if awaiting_reply (reporter/assignee)
            # Feature 16: leads can reply to any workflow in their dept
            can_reply = bool(row.get("awaiting_reply"))
            if not can_reply and _is_lead:
                # Lead role override: can reply to any workflow in dept
                can_reply = True

            if can_reply:
                with col_reply.form(f"reply_{wf_id}", clear_on_submit=True):
                    reply_text = st.text_area(
                        "Cevap",
                        height=80,
                        key=f"reply_text_{wf_id}",
                    )
                    if st.form_submit_button("Gönder"):
                        try:
                            admin_client.reply_to_workflow(
                                workflow_id=wf_id, text=reply_text
                            )
                            st.success("Cevap gönderildi.")
                        except Exception as exc:  # noqa: BLE001
                            st.error(f"Cevap gönderilemedi: {exc}")

            # --- Feature 13: Re-run with environment override ---
            if col_rerun.button(
                "🔄 Re-run with override", key=f"rerun_btn_{wf_id}"
            ):
                st.session_state[f"_show_rerun_form_{wf_id}"] = True

            if st.session_state.get(f"_show_rerun_form_{wf_id}", False):
                st.markdown("---")
                st.markdown("**Test Environment Override**")
                with st.form(f"rerun_form_{wf_id}", clear_on_submit=True):
                    env_text = st.text_area(
                        "Ortam değişkenleri (her satır KEY=VALUE)",
                        height=100,
                        key=f"env_override_text_{wf_id}",
                        help=(
                            "Her satıra bir KEY=VALUE çifti yazın. "
                            "Hassas anahtarlar (SECRET_KEY, DATABASE_URL vb.) "
                            "reddedilir."
                        ),
                    )
                    submitted = st.form_submit_button("Re-run")

                    if submitted:
                        # Parse key=value pairs
                        env_pairs: list[tuple[str, str]] = []
                        for line in env_text.strip().splitlines():
                            line = line.strip()
                            if not line or "=" not in line:
                                continue
                            key, _, value = line.partition("=")
                            env_pairs.append((key.strip(), value.strip()))

                        # Validate sensitive keys
                        errors = _validate_env_overrides(env_pairs)
                        if errors:
                            for err in errors:
                                st.error(err)
                        elif not env_pairs:
                            st.warning(
                                "En az bir KEY=VALUE çifti girilmelidir."
                            )
                        else:
                            env_overrides = {k: v for k, v in env_pairs}
                            try:
                                admin_client.rerun_workflow_with_env(
                                    workflow_id=wf_id,
                                    env_overrides=env_overrides,
                                )
                                st.success(
                                    "Workflow override ile yeniden başlatıldı."
                                )
                                # Audit: test_env_override_applied
                                try:
                                    admin_client.emit_audit_event(
                                        action="test_env_override_applied",
                                        resource=f"workflow:{wf_id}",
                                        payload={
                                            "workflow_id": wf_id,
                                            "env_keys": list(
                                                env_overrides.keys()
                                            ),
                                        },
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                st.session_state[
                                    f"_show_rerun_form_{wf_id}"
                                ] = False
                            except Exception as exc:  # noqa: BLE001
                                st.error(
                                    f"Re-run başarısız: {exc}"
                                )
