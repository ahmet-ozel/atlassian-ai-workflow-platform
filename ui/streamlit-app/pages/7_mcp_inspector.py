"""Streamlit MCP Inspector page (Feature 12).

Provides an interactive interface for inspecting and testing MCP
(Model Context Protocol) tools:

* Shows available MCP tools (after banned-tool filter).
* Tool selection → JSON params form (schema-driven) → "Execute" button.
* Displays request/response/latency for each invocation.

Audit event: ``mcp_inspector_tool_invoked``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

import streamlit as st

from app import _inject_session_state
from components import render_cost_widget, render_dept_switcher
from components.theme import apply_theme, page_hero

_inject_session_state()
st.set_page_config(page_title="MCP Inspector", page_icon="🔍", layout="wide")
apply_theme()
page_hero(
    "MCP Inspector",
    "Banned-tool listesi uygulandıktan sonra kalan Model Context Protocol "
    "araçlarını schema-driven formla test edin. Latency ve istek/yanıt "
    "JSON'ı her çağrıda görüntülenir.",
    icon="🔬",
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
# Load available MCP tools (after banned filter)
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60)
def _load_mcp_tools() -> list[dict[str, Any]]:
    """Fetch available MCP tools from the admin API."""
    try:
        return admin_client.list_mcp_tools(dept_id=dept_id) or []
    except Exception:  # noqa: BLE001
        return []


tools = _load_mcp_tools()

if not tools:
    st.info(
        "Kullanılabilir MCP tool bulunamadı. "
        "Tool listesi yüklenemedi veya tüm tool'lar banned listesinde."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Tool selection
# ---------------------------------------------------------------------------

st.subheader("Kullanılabilir Tool'lar")

tool_names = [t.get("name", "unknown") for t in tools]
selected_tool_name = st.selectbox(
    "Tool seçin",
    tool_names,
    key="mcp_tool_select",
)

selected_tool = next(
    (t for t in tools if t.get("name") == selected_tool_name), None
)

if selected_tool is None:
    st.warning("Seçilen tool bulunamadı.")
    st.stop()

# Display tool info
st.markdown(f"**Açıklama:** {selected_tool.get('description', 'N/A')}")

# ---------------------------------------------------------------------------
# Schema-driven parameter form
# ---------------------------------------------------------------------------

st.subheader("Parametreler")

input_schema = selected_tool.get("input_schema", {})
properties = input_schema.get("properties", {})
required_fields = set(input_schema.get("required", []))

params: dict[str, Any] = {}

if properties:
    with st.form("mcp_params_form", clear_on_submit=False):
        for field_name, field_schema in properties.items():
            field_type = field_schema.get("type", "string")
            field_desc = field_schema.get("description", "")
            is_required = field_name in required_fields
            label = f"{field_name}{'*' if is_required else ''}"

            if field_type == "boolean":
                params[field_name] = st.checkbox(
                    label, help=field_desc, key=f"mcp_param_{field_name}"
                )
            elif field_type == "integer":
                params[field_name] = st.number_input(
                    label,
                    step=1,
                    help=field_desc,
                    key=f"mcp_param_{field_name}",
                )
            elif field_type == "number":
                params[field_name] = st.number_input(
                    label,
                    step=0.1,
                    help=field_desc,
                    key=f"mcp_param_{field_name}",
                )
            elif field_type in ("object", "array"):
                raw = st.text_area(
                    label,
                    help=f"{field_desc} (JSON formatında girin)",
                    key=f"mcp_param_{field_name}",
                )
                if raw.strip():
                    try:
                        params[field_name] = json.loads(raw)
                    except json.JSONDecodeError:
                        st.warning(f"'{field_name}' geçerli JSON değil.")
                        params[field_name] = raw
                else:
                    params[field_name] = None
            else:
                # Default: string input
                enum_values = field_schema.get("enum")
                if enum_values:
                    params[field_name] = st.selectbox(
                        label,
                        enum_values,
                        help=field_desc,
                        key=f"mcp_param_{field_name}",
                    )
                else:
                    params[field_name] = st.text_input(
                        label,
                        help=field_desc,
                        key=f"mcp_param_{field_name}",
                    )

        execute_clicked = st.form_submit_button("🚀 Execute")
else:
    execute_clicked = st.button("🚀 Execute (parametresiz)")

# ---------------------------------------------------------------------------
# Execute tool
# ---------------------------------------------------------------------------

if execute_clicked:
    # Filter out empty optional params
    filtered_params = {
        k: v for k, v in params.items() if v is not None and v != ""
    }

    st.subheader("Sonuç")

    # Show request
    with st.expander("📤 Request", expanded=True):
        st.json({
            "tool": selected_tool_name,
            "params": filtered_params,
        })

    # Execute
    start_time = time.time()
    try:
        result = admin_client.invoke_mcp_tool(
            dept_id=dept_id,
            tool_name=selected_tool_name,
            params=filtered_params,
        )
        latency_ms = (time.time() - start_time) * 1000
        success = True
        error_msg = None
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        result = None
        success = False
        error_msg = str(exc)

    # Show latency
    st.metric("Latency", f"{latency_ms:.1f} ms")

    # Show response
    with st.expander("📥 Response", expanded=True):
        if success:
            if isinstance(result, (dict, list)):
                st.json(result)
            else:
                st.code(str(result))
        else:
            st.error(f"Hata: {error_msg}")

    # Audit event: mcp_inspector_tool_invoked
    try:
        admin_client.emit_audit_event(
            action="mcp_inspector_tool_invoked",
            resource=f"mcp_tool:{selected_tool_name}",
            payload={
                "tool_name": selected_tool_name,
                "params": filtered_params,
                "success": success,
                "latency_ms": round(latency_ms, 2),
            },
        )
    except Exception:  # noqa: BLE001
        pass  # Best-effort audit
