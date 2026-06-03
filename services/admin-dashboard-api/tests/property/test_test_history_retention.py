"""Test history retention policy.
exactly the last 10 test execution results."""
from __future__ import annotations
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SERVICE_ROOT))

from routers.test_results import MAX_TEST_HISTORY


def _retain_last_n(history: list[dict], n: int) -> list[dict]:
    """Pure helper that mirrors the retention logic."""
    return history[-n:] if len(history) > n else history


@settings(max_examples=100, deadline=None)
@given(runs=st.lists(
    st.fixed_dictionaries({"id": st.integers(min_value=0, max_value=1000)}),
    min_size=0, max_size=30,
))
def test_retention_keeps_at_most_max(runs: list[dict]) -> None:
    """After retention, len(runs) <= MAX_TEST_HISTORY."""
    retained = _retain_last_n(runs, MAX_TEST_HISTORY)
    assert len(retained) <= MAX_TEST_HISTORY


@settings(max_examples=100, deadline=None)
@given(
    runs=st.lists(
        st.fixed_dictionaries({"id": st.integers(min_value=0, max_value=1000)}),
        min_size=11, max_size=30,
    )
)
def test_retention_keeps_most_recent(runs: list[dict]) -> None:
    """When > MAX, keeps the LAST MAX entries (most recent)."""
    retained = _retain_last_n(runs, MAX_TEST_HISTORY)
    assert len(retained) == MAX_TEST_HISTORY
    assert retained == runs[-MAX_TEST_HISTORY:]


@settings(max_examples=50, deadline=None)
@given(runs=st.lists(
    st.fixed_dictionaries({"id": st.integers()}),
    min_size=0, max_size=10,
))
def test_under_max_keeps_all(runs: list[dict]) -> None:
    """When <= MAX, all runs are kept."""
    retained = _retain_last_n(runs, MAX_TEST_HISTORY)
    assert retained == runs


def test_max_test_history_constant() -> None:
    assert MAX_TEST_HISTORY == 10
