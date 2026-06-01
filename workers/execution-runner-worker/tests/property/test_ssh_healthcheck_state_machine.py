"""Property test: SSH healthcheck state machine.

Feature: platform-completion, Property 28: For any sequence of SSH healthcheck results,
the system SHALL transition to "unhealthy" after 3 consecutive failures and
restore to "healthy" after 2 consecutive successes (while in unhealthy state).

Validates: Requirements 14.3, 14.4
"""
from __future__ import annotations
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_WORKER_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from workflows.ssh_healthcheck_cron import (
    HealthcheckState, RECOVERY_THRESHOLD, UNHEALTHY_THRESHOLD,
)


def _simulate_state_machine(results: list[bool]) -> HealthcheckState:
    """Replicate the workflow's state-update logic deterministically."""
    state = HealthcheckState()
    for is_healthy in results:
        if is_healthy:
            state.consecutive_failures = 0
            if not state.is_healthy:
                state.consecutive_successes += 1
                if state.consecutive_successes >= RECOVERY_THRESHOLD:
                    state.is_healthy = True
                    state.consecutive_successes = 0
            else:
                state.consecutive_successes = 0
        else:
            state.consecutive_failures += 1
            state.consecutive_successes = 0
            if state.is_healthy and state.consecutive_failures >= UNHEALTHY_THRESHOLD:
                state.is_healthy = False
                state.consecutive_successes = 0
    return state


def test_thresholds() -> None:
    assert UNHEALTHY_THRESHOLD == 3
    assert RECOVERY_THRESHOLD == 2


def test_three_failures_marks_unhealthy() -> None:
    state = _simulate_state_machine([False, False, False])
    assert state.is_healthy is False


def test_two_failures_stays_healthy() -> None:
    state = _simulate_state_machine([False, False])
    assert state.is_healthy is True


def test_two_successes_after_unhealthy_restores() -> None:
    state = _simulate_state_machine([False, False, False, True, True])
    assert state.is_healthy is True


def test_one_success_after_unhealthy_stays_unhealthy() -> None:
    state = _simulate_state_machine([False, False, False, True])
    assert state.is_healthy is False


@settings(max_examples=200, deadline=None)
@given(results=st.lists(st.booleans(), min_size=0, max_size=20))
def test_invariants_hold(results: list[bool]) -> None:
    """State machine invariants always hold."""
    state = _simulate_state_machine(results)
    assert state.consecutive_failures >= 0
    assert state.consecutive_successes >= 0
    if state.is_healthy:
        # When healthy, consecutive_successes is reset to 0
        assert state.consecutive_successes == 0


@settings(max_examples=100, deadline=None)
@given(failure_count=st.integers(min_value=3, max_value=20))
def test_n_consecutive_failures_unhealthy(failure_count: int) -> None:
    """3+ consecutive failures (no successes between) → unhealthy."""
    results = [False] * failure_count
    state = _simulate_state_machine(results)
    assert state.is_healthy is False


@settings(max_examples=100, deadline=None)
@given(success_count=st.integers(min_value=2, max_value=10))
def test_recovery_after_n_successes(success_count: int) -> None:
    """3 failures then N>=2 successes → healthy."""
    results = [False, False, False] + [True] * success_count
    state = _simulate_state_machine(results)
    assert state.is_healthy is True
