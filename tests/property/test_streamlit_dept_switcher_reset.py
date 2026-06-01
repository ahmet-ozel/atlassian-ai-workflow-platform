"""Property test 12 — Streamlit dept switcher full session reset.

**Validates: Requirements 7.2, 3.11, 3.12**

Hypothesis-driven exercise of
``components.dept_switcher.clear_session_except_user``: for any
randomly-generated initial ``session_state`` dict (must contain
``user`` + ``auth_token`` plus arbitrary extra keys), the helper
MUST keep ``user`` and ``auth_token`` and drop **every** other
key. Property 12's full handler-level invariant (cookie write +
probe rerun) is exercised by the integration test
``tests/integration/test_streamlit_dept_switcher_reset.py``; the
pure-state-machine slice is what we pin here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_STREAMLIT_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
)
if str(_STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_ROOT))


try:  # pragma: no cover - guarded import
    from components.dept_switcher import (  # type: ignore[import-not-found]
        clear_session_except_user,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    clear_session_except_user = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    clear_session_except_user is None,
    reason=(
        "components.dept_switcher not yet importable "
        f"(task 9.6 still in flight); error: {_IMPORT_ERROR!r}"
    ),
)


_KEY = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E),
    min_size=1,
    max_size=12,
)
_VALUE = st.one_of(
    st.text(max_size=20),
    st.integers(),
    st.booleans(),
    st.lists(st.text(max_size=8), max_size=4),
)
_EXTRA = st.dictionaries(_KEY, _VALUE, max_size=20)


@settings(max_examples=200, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(extra=_EXTRA)
def test_clear_session_keeps_only_user_and_auth(extra: dict) -> None:
    state: dict = {
        "user": {"id": "u-1", "dept_ids": ["payment"]},
        "auth_token": "tk-1",
        **extra,
    }
    expected_user = state["user"]
    expected_token = state["auth_token"]

    clear_session_except_user(state)

    assert state["user"] == expected_user
    assert state["auth_token"] == expected_token
    extras_after = set(state.keys()) - {"user", "auth_token"}
    assert extras_after == set()
