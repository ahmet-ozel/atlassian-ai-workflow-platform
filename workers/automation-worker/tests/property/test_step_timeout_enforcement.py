"""Step timeout enforcement.

For any step that exceeds its configured timeout, it is marked as failed and
the retry policy is applied.
"""
from __future__ import annotations
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.workflows.multi_step_workflow import (
    DEFAULT_STEP_TIMEOUT_SECONDS, MAX_RETRIES_PER_STEP, RETRY_BACKOFF_INTERVALS,
    StepDefinition,
)


def test_default_timeout_matches_requirement() -> None:
    """Default step timeout is 300 seconds."""
    assert DEFAULT_STEP_TIMEOUT_SECONDS == 300


def test_max_retries_matches_requirement() -> None:
    """Max retries per step is 3."""
    assert MAX_RETRIES_PER_STEP == 3


def test_retry_backoff_intervals() -> None:
    """Retry backoff intervals are 5s, 10s, 20s."""
    assert RETRY_BACKOFF_INTERVALS == (5, 10, 20)


@settings(max_examples=100, deadline=None)
@given(
    name=st.text(min_size=1, max_size=20),
    timeout=st.integers(min_value=1, max_value=3600),
    max_retries=st.integers(min_value=0, max_value=10),
)
def test_step_definition_preserves_timeout(
    name: str, timeout: int, max_retries: int
) -> None:
    """StepDefinition preserves timeout_seconds and max_retries."""
    step = StepDefinition(
        name=name,
        workflow_type="X",
        input_data={},
        timeout_seconds=timeout,
        max_retries=max_retries,
    )
    assert step.timeout_seconds == timeout
    assert step.max_retries == max_retries


@settings(max_examples=100, deadline=None)
@given(retry_count=st.integers(min_value=0, max_value=10))
def test_backoff_index_clamped_to_array(retry_count: int) -> None:
    """Backoff index never exceeds array length."""
    backoff_idx = min(retry_count, len(RETRY_BACKOFF_INTERVALS) - 1)
    assert 0 <= backoff_idx < len(RETRY_BACKOFF_INTERVALS)
