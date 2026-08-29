"""Shared Streamlit theme + chrome injector.

Applies a consistent visual layer across every page in the
Streamlit app:

* design tokens (colors, spacing, radius);
* a polished base layer (typography, focus rings, scrollbars);
* opinionated styling for native Streamlit primitives
  (``st.button``, ``st.metric``, ``st.tabs``, ``st.progress``,
  ``st.expander``, ``st.dataframe``, ``st.alert``);
* helper functions for page hero, section header and KPI rows.

Pages call :func:`apply_theme` once near the top - typically right
after ``st.set_page_config`` - and the rest of the page renders
with the same look-and-feel as the admin dashboard.

The CSS is injected via ``st.markdown(..., unsafe_allow_html=True)``
which is the canonical Streamlit pattern for custom theming.
"""

from __future__ import annotations

from textwrap import dedent

import streamlit as st

__all__ = [
    "apply_theme",
    "page_hero",
    "section_header",
    "kpi_row",
]


_THEME_CSS = dedent("""
<style>
:root {
  --bg: #f6f7fb;
  --bg-elev: #ffffff;
  --bg-muted: #f1f3f9;
  --bg-subtle: #fafbfd;

  --border: #e4e7ef;
  --border-strong: #d3d8e3;

  --fg: #0f172a;
  --fg-muted: #475569;
  --fg-subtle: #64748b;
  --fg-faint: #94a3b8;

  --brand-50: #eef2ff;
  --brand-100: #e0e7ff;
  --brand-500: #6366f1;
  --brand-600: #4f46e5;
  --brand-700: #4338ca;

  --success-50: #ecfdf5;
  --success-500: #10b981;
  --success-700: #047857;

  --warn-50: #fffbeb;
  --warn-500: #f59e0b;
  --warn-700: #b45309;

  --danger-50: #fef2f2;
  --danger-500: #ef4444;
  --danger-700: #b91c1c;

  --radius: 10px;
  --radius-lg: 14px;
  --shadow-sm: 0 1px 3px rgba(15, 23, 42, 0.06);
  --shadow: 0 4px 12px rgba(15, 23, 42, 0.08);
}

html, body, .stApp, .main, [data-testid="stAppViewContainer"] {
  background: var(--bg) !important;
  color: var(--fg) !important;
}

.stApp,
[data-testid="stAppViewContainer"] {
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI",
    Roboto, "Helvetica Neue", Arial, sans-serif;
  -webkit-font-smoothing: antialiased;
}

/* Reduce default heavy padding */
.main .block-container {
  padding-top: 2rem;
  padding-bottom: 4rem;
  max-width: 1200px;
}

/* Headings */
h1, h2, h3, h4 {
  color: var(--fg) !important;
  font-weight: 600 !important;
  letter-spacing: -0.01em !important;
}
h1 { font-size: 1.85rem !important; letter-spacing: -0.02em !important; }
h2 { font-size: 1.3rem !important; }
h3 { font-size: 1.05rem !important; }

/* Caption / small text */
.stCaption, [data-testid="stCaptionContainer"] {
  color: var(--fg-subtle) !important;
}

/* Sidebar */
[data-testid="stSidebar"] {
  background: linear-gradient(180deg, #0f172a 0%, #111c34 100%) !important;
  border-right: 1px solid #1e293b !important;
}

[data-testid="stSidebar"] * {
  color: #cbd5e1 !important;
}

[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] strong {
  color: #f8fafc !important;
}

[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] a {
  color: #c7d2fe !important;
}

[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] a:hover {
  color: #ffffff !important;
}

[data-testid="stSidebarNav"] {
  padding-top: 1rem;
}

[data-testid="stSidebarNav"] li a {
  border-radius: 8px !important;
  margin: 2px 8px !important;
  padding: 0.45rem 0.7rem !important;
  font-weight: 500 !important;
  font-size: 0.88rem !important;
  transition: background 160ms, color 160ms !important;
}

[data-testid="stSidebarNav"] li a:hover {
  background: rgba(99, 102, 241, 0.12) !important;
  color: #ffffff !important;
}

[data-testid="stSidebarNav"] li a[aria-current="page"] {
  background: rgba(99, 102, 241, 0.20) !important;
  color: #ffffff !important;
  box-shadow: inset 3px 0 0 var(--brand-500) !important;
}

/* Buttons */
.stButton > button,
.stDownloadButton > button,
.stFormSubmitButton > button {
  background: var(--brand-600) !important;
  color: white !important;
  border: 1px solid var(--brand-600) !important;
  border-radius: 8px !important;
  font-weight: 500 !important;
  padding: 0.45rem 1rem !important;
  font-size: 0.88rem !important;
  box-shadow: 0 1px 2px rgba(79, 70, 229, 0.18),
              0 4px 10px rgba(79, 70, 229, 0.18) !important;
  transition: background 140ms, transform 140ms, box-shadow 140ms !important;
}

.stButton > button:hover,
.stDownloadButton > button:hover,
.stFormSubmitButton > button:hover {
  background: var(--brand-700) !important;
  border-color: var(--brand-700) !important;
  transform: translateY(-1px);
}

.stButton > button:active,
.stDownloadButton > button:active,
.stFormSubmitButton > button:active {
  transform: translateY(0);
}

.stButton > button:disabled,
.stDownloadButton > button:disabled,
.stFormSubmitButton > button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* Secondary button styling - Streamlit kind="secondary" */
.stButton > button[kind="secondary"] {
  background: var(--bg-elev) !important;
  color: var(--fg) !important;
  border: 1px solid var(--border-strong) !important;
  box-shadow: none !important;
}

.stButton > button[kind="secondary"]:hover {
  background: var(--bg-muted) !important;
  border-color: var(--fg-faint) !important;
}

/* Inputs / selects / textarea */
.stTextInput input,
.stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div,
.stMultiSelect div[data-baseweb="select"] > div,
.stNumberInput input,
.stDateInput input,
.stTimeInput input {
  background: var(--bg-elev) !important;
  border: 1px solid var(--border-strong) !important;
  border-radius: 8px !important;
  color: var(--fg) !important;
  font-size: 0.88rem !important;
}

.stTextInput input:focus,
.stTextArea textarea:focus,
.stNumberInput input:focus {
  border-color: var(--brand-500) !important;
  box-shadow: 0 0 0 3px var(--brand-100) !important;
  outline: none !important;
}

label {
  color: var(--fg-muted) !important;
  font-weight: 500 !important;
  font-size: 0.85rem !important;
}

/* Tabs */
.stTabs [data-baseweb="tab-list"] {
  border-bottom: 1px solid var(--border) !important;
  gap: 4px !important;
}

.stTabs [data-baseweb="tab"] {
  padding: 0.6rem 1rem !important;
  color: var(--fg-muted) !important;
  font-weight: 500 !important;
  border-bottom: 2px solid transparent !important;
  background: transparent !important;
}

.stTabs [data-baseweb="tab"]:hover {
  color: var(--fg) !important;
}

.stTabs [aria-selected="true"] {
  color: var(--brand-700) !important;
  border-bottom-color: var(--brand-600) !important;
  font-weight: 600 !important;
}

/* Expanders */
[data-testid="stExpander"] {
  background: var(--bg-elev) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius) !important;
  box-shadow: var(--shadow-sm) !important;
  margin-bottom: 0.5rem;
}

[data-testid="stExpander"] summary {
  padding: 0.85rem 1rem !important;
  font-weight: 500 !important;
  color: var(--fg) !important;
}

[data-testid="stExpander"] summary:hover {
  background: var(--bg-subtle) !important;
}

[data-testid="stExpander"] > div:nth-child(2) {
  padding: 0 1rem 1rem !important;
}

/* Alerts (info / success / warn / error) */
[data-testid="stAlert"] {
  border-radius: var(--radius) !important;
  border: 1px solid var(--border) !important;
  font-size: 0.88rem !important;
  padding: 0.75rem 1rem !important;
}

[data-baseweb="notification"][kind="info"],
.stAlert > div:has(> div [data-testid="stMarkdownContainer"]) {
  /* default: keep */
}

/* Metric */
[data-testid="stMetric"] {
  background: var(--bg-elev) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-lg) !important;
  padding: 1rem 1.1rem !important;
  box-shadow: var(--shadow-sm) !important;
  position: relative;
  overflow: hidden;
}

[data-testid="stMetric"]::before {
  content: "";
  position: absolute;
  inset: 0 auto 0 0;
  width: 4px;
  background: linear-gradient(135deg, var(--brand-500), #7c3aed);
  opacity: 0.85;
}

[data-testid="stMetricLabel"] {
  color: var(--fg-subtle) !important;
  font-size: 0.78rem !important;
  font-weight: 600 !important;
  text-transform: uppercase !important;
  letter-spacing: 0.06em !important;
}

[data-testid="stMetricValue"] {
  color: var(--fg) !important;
  font-weight: 700 !important;
  letter-spacing: -0.02em !important;
  font-size: 1.7rem !important;
}

/* Progress */
.stProgress > div > div > div {
  background: linear-gradient(90deg, var(--brand-500), var(--brand-700)) !important;
  border-radius: 999px !important;
}

.stProgress > div > div {
  background: var(--bg-muted) !important;
  border-radius: 999px !important;
}

/* Dataframes / tables */
[data-testid="stTable"] table,
[data-testid="stDataFrame"] table {
  border-radius: var(--radius-lg) !important;
  overflow: hidden !important;
  box-shadow: var(--shadow-sm) !important;
  border: 1px solid var(--border) !important;
}

/* Code blocks */
code {
  background: var(--bg-muted) !important;
  border-radius: 4px;
  padding: 0.1em 0.3em;
  color: var(--fg) !important;
  font-size: 0.86em;
}

pre {
  background: #0f172a !important;
  color: #e2e8f0 !important;
  border-radius: var(--radius) !important;
  padding: 1rem !important;
  border: 1px solid #1e293b !important;
}

/* Chat message bubbles */
[data-testid="stChatMessageContent"] {
  background: var(--bg-elev) !important;
  color: var(--fg) !important;
  border: 1px solid var(--border) !important;
  border-radius: var(--radius-lg) !important;
  padding: 0.85rem 1rem !important;
  box-shadow: var(--shadow-sm) !important;
}

[data-testid="stChatMessageContent"] [data-testid="stMarkdownContainer"],
[data-testid="stChatMessageContent"] p,
[data-testid="stChatMessageContent"] li,
[data-testid="stChatMessageContent"] span {
  color: var(--fg) !important;
}

[data-testid="stChatMessageContent"] a {
  color: var(--brand-700) !important;
}

[data-testid="stChatMessage"][data-testid*="user"] [data-testid="stChatMessageContent"] {
  background: var(--brand-50) !important;
  border-color: var(--brand-100) !important;
  color: var(--fg) !important;
}

[data-testid="stChatMessage"] [data-testid="stChatMessageAvatar"] {
  background: linear-gradient(135deg, var(--brand-500), #7c3aed) !important;
}

/* Custom helper components */
.ya-hero {
  background: linear-gradient(135deg, #6366f1 0%, #4f46e5 60%, #7c3aed 100%);
  color: #fff;
  padding: 1.6rem 1.8rem;
  border-radius: 18px;
  margin-bottom: 1.5rem;
  position: relative;
  overflow: hidden;
  box-shadow: 0 12px 28px rgba(79, 70, 229, 0.18);
}

.ya-hero::after {
  content: "";
  position: absolute;
  inset: -50% -20% auto auto;
  width: 320px; height: 320px;
  background: radial-gradient(circle at center, rgba(255,255,255,0.16) 0%, transparent 60%);
  pointer-events: none;
}

.ya-hero h1 {
  color: #ffffff !important;
  margin: 0 !important;
  font-size: 1.6rem !important;
}

.ya-hero p {
  color: rgba(255, 255, 255, 0.88);
  margin: 0.4rem 0 0;
  max-width: 60ch;
  font-size: 0.95rem;
  position: relative;
  z-index: 1;
}

.ya-section {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  margin: 1.25rem 0 0.6rem;
  padding-bottom: 0.4rem;
  border-bottom: 1px solid var(--border);
}

.ya-section h3 {
  margin: 0 !important;
  font-size: 1rem !important;
}

.ya-section .ya-sub {
  color: var(--fg-subtle);
  font-size: 0.82rem;
}

.ya-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.18rem 0.55rem;
  border-radius: 999px;
  background: var(--bg-muted);
  color: var(--fg-muted);
  border: 1px solid var(--border);
  font-size: 0.72rem;
  font-weight: 600;
}

.ya-pill--success { background: var(--success-50); color: var(--success-700); border-color: rgba(16, 185, 129, 0.2); }
.ya-pill--warn    { background: var(--warn-50);    color: var(--warn-700);    border-color: rgba(245, 158, 11, 0.2); }
.ya-pill--danger  { background: var(--danger-50);  color: var(--danger-700);  border-color: rgba(239, 68, 68, 0.2); }
.ya-pill--brand   { background: var(--brand-50);   color: var(--brand-700);   border-color: var(--brand-100); }

/* Reduce default emoji icon size in titles */
[data-testid="stHeader"] {
  background: transparent !important;
}

/* Tooltip & focus polish */
*:focus-visible {
  outline: 2px solid var(--brand-500) !important;
  outline-offset: 2px !important;
}

::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: var(--border-strong);
  border-radius: 999px;
  border: 2px solid transparent;
  background-clip: content-box;
}
::-webkit-scrollbar-thumb:hover { background: var(--fg-faint); background-clip: content-box; }
</style>
""")


def apply_theme() -> None:
    """Inject the global theme stylesheet into the current page.

    Idempotent - safe to call from every page module. Streamlit
    renders the ``<style>`` tag once per page render.
    """
    if st.session_state.get("_theme_applied_once") is True:
        # Streamlit re-runs the script on every interaction so we only
        # need to inject CSS once per session_state lifetime; subsequent
        # injections are no-ops thanks to identical content.
        pass
    st.markdown(_THEME_CSS, unsafe_allow_html=True)
    st.session_state["_theme_applied_once"] = True


def page_hero(title: str, subtitle: str | None = None, *, icon: str | None = None) -> None:
    """Render a hero header above a Streamlit page body.

    Use as the first visible element of a page (after
    :func:`apply_theme`) for a consistent landing impression.
    """
    icon_html = f"<span style='margin-right:0.5rem'>{icon}</span>" if icon else ""
    sub_html = f"<p>{subtitle}</p>" if subtitle else ""
    st.markdown(
        f"""
        <div class="ya-hero">
          <h1>{icon_html}{title}</h1>
          {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_header(title: str, sub: str | None = None) -> None:
    """Render a smaller section divider with optional sub-label."""
    sub_html = f"<span class='ya-sub'>{sub}</span>" if sub else ""
    st.markdown(
        f"""
        <div class="ya-section">
          <h3>{title}</h3>
          {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def kpi_row(items: list[tuple[str, str, str | None]]) -> None:
    """Render a row of metric cards via ``st.metric``.

    ``items`` is a list of ``(label, value, delta_or_help)`` tuples.
    The ``delta_or_help`` field is rendered as a delta when present,
    or as a small help hint when ``None``. Use blank string to hide.
    """
    if not items:
        return
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        if delta:
            col.metric(label, value, delta)
        else:
            col.metric(label, value)
