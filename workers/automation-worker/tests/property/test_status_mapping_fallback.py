"""Status mapping resolution with fallback.

For any logical status, the resolver first checks department status_mapping
case-insensitively. If no mapping is found, it applies the fallback
transformation by replacing underscores with spaces and title-casing the result.
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
    _fallback_transform,
    resolve_jira_status,
)


@settings(max_examples=100, deadline=None)
@given(
    logical_status=st.sampled_from(sorted(SUPPORTED_LOGICAL_STATES)),
    mapping=st.dictionaries(
        keys=st.sampled_from(sorted(SUPPORTED_LOGICAL_STATES)),
        values=st.text(min_size=1, max_size=30),
        max_size=5,
    ),
)
def test_mapping_takes_priority(
    logical_status: str, mapping: dict[str, str]
) -> None:
    """When mapping has the status, it's used; else fallback."""
    result = asyncio.run(resolve_jira_status(logical_status, mapping))
    assert result.resolved is True
    if logical_status in mapping:
        assert result.jira_status == mapping[logical_status]
        assert result.used_fallback is False
    else:
        assert result.used_fallback is True
        assert result.jira_status == _fallback_transform(logical_status)


@settings(max_examples=50, deadline=None)
@given(logical_status=st.sampled_from(sorted(SUPPORTED_LOGICAL_STATES)))
def test_no_mapping_uses_fallback(logical_status: str) -> None:
    """No mapping at all  fallback transform applied."""
    result = asyncio.run(resolve_jira_status(logical_status, None))
    assert result.resolved is True
    assert result.used_fallback is True
    assert result.jira_status == _fallback_transform(logical_status)


@settings(max_examples=50, deadline=None)
@given(logical_status=st.sampled_from(sorted(SUPPORTED_LOGICAL_STATES)))
def test_case_insensitive_mapping(logical_status: str) -> None:
    """Mapping keys can be uppercase - match still works."""
    mapping = {logical_status.upper(): "Custom Status"}
    result = asyncio.run(resolve_jira_status(logical_status, mapping))
    assert result.resolved is True
    assert result.jira_status == "Custom Status"
    assert result.used_fallback is False


def test_fallback_transform_examples() -> None:
    """Fallback transform replaces _ with space and title-cases."""
    assert _fallback_transform("in_progress") == "In Progress"
    assert _fallback_transform("out_of_scope") == "Out Of Scope"
    assert _fallback_transform("done") == "Done"
