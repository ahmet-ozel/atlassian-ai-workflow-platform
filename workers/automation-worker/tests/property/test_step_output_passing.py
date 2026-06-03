"""Sequential step output passing.

For any pair of consecutive steps, the output of step N is passed as input to
step N+1.
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
    MultiStepWorkflow, StepResult,
)


@settings(max_examples=100, deadline=None)
@given(
    step_name=st.text(min_size=1, max_size=20),
    output_summary=st.text(max_size=400),
    duration=st.floats(min_value=0.0, max_value=600.0, allow_nan=False),
)
def test_extract_output_includes_summary(
    step_name: str, output_summary: str, duration: float
) -> None:
    """_extract_output preserves output_summary for next step."""
    result = StepResult(
        step_name=step_name,
        status="completed",
        output_summary=output_summary,
        duration_seconds=duration,
    )
    extracted = MultiStepWorkflow._extract_output(result)
    assert extracted["step_name"] == step_name
    assert extracted["output_summary"] == output_summary
    assert extracted["duration_seconds"] == duration


@settings(max_examples=50, deadline=None)
@given(
    chain=st.lists(
        st.text(min_size=1, max_size=20),
        min_size=2, max_size=10,
    )
)
def test_chain_preserves_each_step_output(chain: list[str]) -> None:
    """Each step's extracted output names match input."""
    results = [
        StepResult(step_name=name, status="completed", output_summary=f"out-{i}")
        for i, name in enumerate(chain)
    ]
    for i, result in enumerate(results):
        extracted = MultiStepWorkflow._extract_output(result)
        assert extracted["step_name"] == chain[i]
        assert extracted["output_summary"] == f"out-{i}"
