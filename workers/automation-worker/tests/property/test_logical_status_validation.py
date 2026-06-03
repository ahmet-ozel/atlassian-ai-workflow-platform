"""Invariant test: Logical status input validation.

Feature:,: For any jira_transition target_status,
it SHALL be accepted only if it is one of {todo, in_progress, review, done, out_of_scope}.

"""
from __future__ import annotations
import asyncio
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.status_mapping import (
    SUPPORTED_LOGICAL_STATES,
    resolve_jira_status,
)


@settings(max_examples=300, deadline=None)
@given(logical_status=st.text(max_size=50))
def test_only_supported_states_resolve(logical_status: str) -> None:
    """Resolves iff logical_status is in SUPPORTED_LOGICAL_STATES."""
    result = asyncio.run(resolve_jira_status(logical_status, None))
    if logical_status in SUPPORTED_LOGICAL_STATES:
        assert result.resolved is True
        assert result.error is None
    else:
        assert result.resolved is False
        assert result.error is not None
        assert "Invalid logical status" in result.error


def test_supported_states_set() -> None:
    """SUPPORTED_LOGICAL_STATES matches the spec."""
    assert SUPPORTED_LOGICAL_STATES == frozenset({
        "todo", "in_progress", "review", "done", "out_of_scope"
    })
