"""Property test: Test result structured parsing.

Feature: platform-completion, Property 38: For any valid JSON test output, each
test case SHALL be parsed into a structured record containing test name,
pass/fail status, duration in milliseconds, and error message (max 500 chars).

Validates: Requirements 7.2
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SERVICE_ROOT))

from routers.test_results import MAX_ERROR_LENGTH, _parse_test_output


_TEST_CASE = st.fixed_dictionaries({
    "name": st.text(min_size=1, max_size=50),
    "passed": st.booleans(),
    "duration_ms": st.integers(min_value=0, max_value=600_000),
    "error": st.text(max_size=2000),
})


@settings(max_examples=100, deadline=None)
@given(tests=st.lists(_TEST_CASE, min_size=0, max_size=20))
def test_valid_json_parses_each_case(tests: list[dict]) -> None:
    """Valid JSON: each test produces a TestCaseResult."""
    output = json.dumps({"tests": tests})
    cases, structured = _parse_test_output(output)
    assert structured is True
    assert len(cases) == len(tests)
    for parsed, original in zip(cases, tests):
        assert parsed.name == original["name"]
        expected_status = "pass" if original["passed"] else "fail"
        assert parsed.status == expected_status
        assert parsed.duration_ms == original["duration_ms"]


@settings(max_examples=100, deadline=None)
@given(error_text=st.text(min_size=0, max_size=2000))
def test_error_message_truncated_to_max(error_text: str) -> None:
    """Error messages are truncated to MAX_ERROR_LENGTH."""
    output = json.dumps({
        "tests": [{
            "name": "t1",
            "passed": False,
            "duration_ms": 100,
            "error": error_text,
        }]
    })
    cases, _ = _parse_test_output(output)
    if cases and cases[0].error:
        assert len(cases[0].error) <= MAX_ERROR_LENGTH


@settings(max_examples=50, deadline=None)
@given(invalid=st.text(max_size=200).filter(lambda s: not s.startswith("{")))
def test_invalid_json_returns_unstructured(invalid: str) -> None:
    """Non-JSON output → empty cases + unstructured flag."""
    cases, structured = _parse_test_output(invalid)
    assert cases == []
    assert structured is False


def test_max_error_length_constant() -> None:
    assert MAX_ERROR_LENGTH == 500
