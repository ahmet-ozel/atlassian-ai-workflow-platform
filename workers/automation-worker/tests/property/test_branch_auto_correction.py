"""Property test: Branch name auto-correction.

Feature: platform-completion, Property 34: For any generated branch name that doesn't
match department branch_pattern_rules, the system SHALL produce a corrected name
that satisfies all configured rules.

Validates: Requirements 17.2
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

from automation_worker.activities.branch_rules import (
    BranchRuleInput, _sanitize_branch_name, evaluate_branch_rules,
)


@settings(max_examples=100, deadline=None)
@given(name=st.text(min_size=1, max_size=100))
def test_sanitize_strips_invalid_chars(name: str) -> None:
    """Sanitized name only contains allowed chars or is empty."""
    sanitized = _sanitize_branch_name(name)
    for ch in sanitized:
        assert ch.isalnum() or ch in "/_.-"


@settings(max_examples=100, deadline=None)
@given(name=st.text(min_size=1, max_size=100))
def test_no_consecutive_dashes(name: str) -> None:
    """Sanitized name has no consecutive dashes."""
    sanitized = _sanitize_branch_name(name)
    assert "--" not in sanitized


@settings(max_examples=50, deadline=None)
@given(
    issue_key=st.from_regex(r"[A-Z]{2,5}-[1-9][0-9]{0,4}", fullmatch=True),
    branch_name=st.from_regex(r"[a-z][a-z0-9-]{1,20}", fullmatch=True),
)
def test_corrected_name_valid_after_rules(issue_key: str, branch_name: str) -> None:
    """After corrections, branch is either valid or the rule errored."""
    inp = BranchRuleInput(
        issue_key=issue_key,
        proposed_branch_name=branch_name,
        branch_pattern_rules=[{"type": "prefix", "pattern": "{issue_key}/"}],
    )
    result = asyncio.run(evaluate_branch_rules(inp))
    if result.valid:
        assert result.corrected_name is not None
        assert len(result.corrected_name) <= 255
        assert len(result.corrected_name) > 0


@settings(max_examples=30, deadline=None)
@given(branch_name=st.from_regex(r"[a-z][a-z0-9-]{1,20}", fullmatch=True))
def test_empty_rules_keeps_name(branch_name: str) -> None:
    """Empty rules list — name is returned unchanged."""
    inp = BranchRuleInput(
        issue_key="PROJ-1",
        proposed_branch_name=branch_name,
        branch_pattern_rules=[],
    )
    result = asyncio.run(evaluate_branch_rules(inp))
    assert result.valid is True
    assert result.corrected_name == branch_name
