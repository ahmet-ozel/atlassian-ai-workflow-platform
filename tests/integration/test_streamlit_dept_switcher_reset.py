"""Integration smoke — Streamlit dept switcher reset (`ops work` the implementation).

Drives the Streamlit page through ``streamlit.testing.v1.AppTest``
to confirm a dept change clears every session_state key except
``user`` + ``auth_token``. The shallow property-test counterpart
lives at ``tests/property/test_streamlit_dept_switcher_reset.py``;
this file is the live-AppTest variant.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


_STREAMLIT_ROOT = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
)
if str(_STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_ROOT))


def test_dept_change_clears_session_state(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker (Streamlit AppTest harness)")

    try:
        from streamlit.testing.v1 import AppTest  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("streamlit.testing.v1 unavailable")

    page = _STREAMLIT_ROOT / "pages" / "1_chat.py"
    if not page.is_file():
        pytest.skip("chat page not present")

    at = AppTest.from_file(str(page))
    at.session_state["user"] = {
        "id": "u-1",
        "dept_ids": ["alpha", "beta"],
        "default_dept_id": "alpha",
        "session_id": "s-1",
    }
    at.session_state["auth_token"] = "tok"
    at.session_state["chat_history"] = [{"role": "user", "text": "hi"}]
    at.run()

    assert at.session_state.get("active_dept_id") == "alpha"
    # Switch to beta — chat_history MUST drop.
    at.session_state["dept_select"] = "beta"
    at.run()

    assert at.session_state.get("active_dept_id") == "beta"
    assert "chat_history" not in at.session_state
    assert at.session_state["user"]["id"] == "u-1"
    assert at.session_state["auth_token"] == "tok"
