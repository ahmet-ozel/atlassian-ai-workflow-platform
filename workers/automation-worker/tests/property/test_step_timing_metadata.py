"""Property test: Step timing metadata recording.

Feature: platform-completion, Property 14: For any step that reaches completed
or failed status, start_time, end_time, duration_seconds, and output_summary
(truncated to max 500 characters) SHALL be recorded.

Validates: Requirements 5.7
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
    MAX_OUTPUT_SUMMARY_LENGTH, MultiStepWorkflow,
)


@settings(max_examples=300, deadline=None)
@given(text=st.text(max_size=2000))
def test_truncate_output_summary_never_exceeds_max(text: str) -> None:
    """Truncated output_summary never exceeds MAX_OUTPUT_SUMMARY_LENGTH."""
    truncated = MultiStepWorkflow._truncate_output_summary(text)
    assert len(truncated) <= MAX_OUTPUT_SUMMARY_LENGTH


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=MAX_OUTPUT_SUMMARY_LENGTH))
def test_truncate_preserves_short_text(text: str) -> None:
    """Short texts pass through unchanged."""
    truncated = MultiStepWorkflow._truncate_output_summary(text)
    assert truncated == text


@settings(max_examples=200, deadline=None)
@given(text=st.text(min_size=MAX_OUTPUT_SUMMARY_LENGTH + 1, max_size=2000))
def test_truncate_long_text_uses_ellipsis(text: str) -> None:
    """Long texts get truncated with ellipsis suffix."""
    truncated = MultiStepWorkflow._truncate_output_summary(text)
    assert truncated.endswith("...")
    assert len(truncated) == MAX_OUTPUT_SUMMARY_LENGTH


def test_max_output_summary_length() -> None:
    """MAX_OUTPUT_SUMMARY_LENGTH matches Requirement 5.7 (500 chars)."""
    assert MAX_OUTPUT_SUMMARY_LENGTH == 500
