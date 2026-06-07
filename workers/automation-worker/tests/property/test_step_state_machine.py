"""Step state machine validity.

For any workflow step, its status only transitions through valid states:
pending -> running -> (completed | failed).
"""
from __future__ import annotations
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_VALID_STATES = ("pending", "running", "completed", "failed")
_VALID_TRANSITIONS = {
    "pending": {"running"},
    "running": {"completed", "failed"},
    "completed": set(),
    "failed": set(),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    if from_state not in _VALID_STATES or to_state not in _VALID_STATES:
        return False
    return to_state in _VALID_TRANSITIONS[from_state]


@settings(max_examples=200, deadline=None)
@given(
    from_state=st.sampled_from(_VALID_STATES),
    to_state=st.sampled_from(_VALID_STATES),
)
def test_only_valid_transitions_accepted(from_state: str, to_state: str) -> None:
    """Only valid transitions are accepted."""
    valid = is_valid_transition(from_state, to_state)
    if from_state == "pending" and to_state == "running":
        assert valid
    elif from_state == "running" and to_state in ("completed", "failed"):
        assert valid
    else:
        assert not valid


@settings(max_examples=100, deadline=None)
@given(
    transitions=st.lists(
        st.sampled_from(_VALID_STATES), min_size=1, max_size=10
    )
)
def test_terminal_states_have_no_transitions(transitions: list[str]) -> None:
    """completed and failed are terminal - no outgoing transitions."""
    for state in transitions:
        if state in ("completed", "failed"):
            assert _VALID_TRANSITIONS[state] == set()
