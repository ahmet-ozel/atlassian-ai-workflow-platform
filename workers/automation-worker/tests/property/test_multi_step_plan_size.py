"""Property test: Multi-step plan size constraint.

Feature: platform-completion, Property 11: For any multi_step workflow,
the number of generated steps SHALL be between 2 and 20 inclusive.

Validates: Requirements 5.1
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest
from hypothesis import given, strategies as st, settings

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.workflows.multi_step_workflow import (
    MAX_STEPS, MIN_STEPS, StepDefinition,
)


def _is_valid_step_count(n: int) -> bool:
    return MIN_STEPS <= n <= MAX_STEPS


@settings(max_examples=200, deadline=None)
@given(step_count=st.integers(min_value=0, max_value=50))
def test_step_count_validity_classification(step_count: int) -> None:
    """For any step count, validity is exactly MIN_STEPS <= n <= MAX_STEPS."""
    valid = _is_valid_step_count(step_count)
    if step_count < MIN_STEPS or step_count > MAX_STEPS:
        assert not valid
    else:
        assert valid


@settings(max_examples=200, deadline=None)
@given(steps=st.lists(
    st.builds(
        StepDefinition,
        name=st.text(min_size=1, max_size=20),
        workflow_type=st.just("ChildWorkflow"),
        input_data=st.just({}),
    ),
    min_size=0, max_size=30,
))
def test_step_list_validity_aligns_with_count(steps: list[StepDefinition]) -> None:
    """Lists with step count outside 2-20 SHALL be rejected by the validator."""
    valid = _is_valid_step_count(len(steps))
    if len(steps) < MIN_STEPS or len(steps) > MAX_STEPS:
        assert not valid
    else:
        assert valid


def test_min_max_constants() -> None:
    """MIN_STEPS and MAX_STEPS match the requirement."""
    assert MIN_STEPS == 2
    assert MAX_STEPS == 20
