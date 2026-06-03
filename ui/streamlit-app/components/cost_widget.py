"""End-user cost / quota widget.

The Streamlit sidebar shows the running user / dept cost vs. weekly cap
so the user understands their remaining budget before triggering an
expensive workflow.

The widget reads from ``GET /api/costs/me`` (assistant-service) via
an injected client on ``st.session_state["_costs_api"]``. The shape
returned MUST be::

    {
        "user_weekly_usd": "1.23",
        "user_weekly_cap_usd": "20.00",
        "dept_weekly_usd": "45.67",
        "dept_weekly_cap_usd": "100.00",
        "currency": "USD",
        "as_of": "2025-01-15T12:00:00Z",
    }

A missing client surfaces a single small caption rather than blocking
the page; this widget is informational only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping

import streamlit as st

__all__ = ["render_cost_widget"]


def _to_decimal(value: object) -> Decimal | None:
    """Best-effort convert any wire value to :class:`Decimal`.

    The API serialises Decimal as a string (preserves precision); we
    accept ints and floats too in case a future schema variant ships
    numeric values.
    """

    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _format_usd(value: Decimal | None) -> str:
    """Render a USD amount as ``$X.YY`` with two-decimal precision."""

    if value is None:
        return "—"
    return f"${value.quantize(Decimal('0.01'))}"


def _bar(used: Decimal | None, cap: Decimal | None) -> float:
    """Return a 0..1 progress value for ``st.progress``.

    Returns 0.0 when either value is missing or the cap is zero;
    clips at 1.0 so the bar visually saturates instead of overflowing.
    """

    if used is None or cap is None or cap <= 0:
        return 0.0
    ratio = float(used / cap)
    return max(0.0, min(1.0, ratio))


def render_cost_widget() -> None:
    """Render the cost / quota widget in the Streamlit sidebar.

    Pages that want the widget call this function once near the top
    of their body, after :func:`render_dept_switcher`. The widget is
    self-contained and does not return anything.
    """

    api = st.session_state.get("_costs_api")
    if api is None:
        st.sidebar.caption("Cost widget: API yapılandırılmamış.")
        return

    try:
        data: Mapping[str, object] = api.get_me() or {}
    except Exception as exc:  # noqa: BLE001 — informational only
        st.sidebar.caption(f"Cost widget okunamadı: {exc}")
        return

    user_used = _to_decimal(data.get("user_weekly_usd"))
    user_cap = _to_decimal(data.get("user_weekly_cap_usd"))
    dept_used = _to_decimal(data.get("dept_weekly_usd"))
    dept_cap = _to_decimal(data.get("dept_weekly_cap_usd"))

    with st.sidebar.expander("Maliyet ve kota", expanded=False):
        # User-scoped row.
        st.caption(
            f"Kullanıcı (haftalık): {_format_usd(user_used)} / "
            f"{_format_usd(user_cap)}"
        )
        st.progress(_bar(user_used, user_cap))

        # Dept-scoped row.
        st.caption(
            f"Departman (haftalık): {_format_usd(dept_used)} / "
            f"{_format_usd(dept_cap)}"
        )
        st.progress(_bar(dept_used, dept_cap))

        as_of = data.get("as_of")
        if isinstance(as_of, str) and as_of:
            st.caption(f"Son güncelleme: {as_of}")
