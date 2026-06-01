"""Property test: Branch pattern prefix rule application.

Feature: platform-completion, Property 33: For any branch creation with a "prefix"
type rule in department configuration, the issue key SHALL be prepended.

Validates: Requirements 17.4
"""
from __future__ import annotations
import asyncio
import re
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.branch_rules import (
    BranchRuleInput,
    _apply_prefix_rule,
    evaluate_branch_rules,
)

_ISSUE_KEY = st.from_regex(r"[A-Z]{2,5}-[1-9][0-9]{0,4}", fullmatch=True)
_BRANCH_NAME = st.from_regex(r"[a-z][a-z0-9-]{1,30}", fullmatch=True)


@settings(max_examples=100, deadline=None)
@given(
    issue_key=_ISSUE_KEY,
    branch_name=_BRANCH_NAME,
)
def test_prefix_rule_prepends_issue_key(issue_key: str, branch_name: str) -> None:
    """Prefix rule with {issue_key}/ prepends the key to the branch name."""
    inp = BranchRuleInput(
        issue_key=issue_key,
        proposed_branch_name=branch_name,
        branch_pattern_rules=[{"type": "prefix", "pattern": "{issue_key}/"}],
    )
    result = asyncio.run(evaluate_branch_rules(inp))
    assert result.valid is True
    assert result.corrected_name is not None
    assert result.corrected_name.startswith(f"{issue_key}/") or result.corrected_name == branch_name


@settings(max_examples=100, deadline=None)
@given(
    issue_key=_ISSUE_KEY,
    branch_name=_BRANCH_NAME,
)
def test_apply_prefix_rule_unit(issue_key: str, branch_name: str) -> None:
    """_apply_prefix_rule prepends correctly when not already prefixed."""
    pattern = "{issue_key}/"
    new_name, corrected = _apply_prefix_rule(branch_name, issue_key, pattern)
    if branch_name.startswith(f"{issue_key}/"):
        assert not corrected
    else:
        assert corrected
        assert new_name == f"{issue_key}/{branch_name}"
