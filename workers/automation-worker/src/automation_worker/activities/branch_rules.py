"""Branch Pattern Rules enforcement activity.

Validates and corrects branch names according to department
configuration rules.

Requirements: 17.1, 17.2, 17.3, 17.4, 17.5
"""
from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from typing import Any
from temporalio import activity

__all__ = ("evaluate_branch_rules", "BranchRuleInput", "BranchRuleResult")

_logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class BranchRuleInput:
    issue_key: str
    proposed_branch_name: str
    branch_pattern_rules: list[dict[str, str]]

@dataclass(frozen=True)
class BranchRuleResult:
    valid: bool
    corrected_name: str | None
    applied_corrections: list[str]
    error: str | None = None

def _apply_prefix_rule(branch_name: str, issue_key: str, pattern: str) -> tuple[str, bool]:
    prefix = pattern.replace("{issue_key}", issue_key)
    if branch_name.startswith(prefix):
        return branch_name, False
    return f"{prefix}{branch_name}", True

def _sanitize_branch_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9/_.-]", "-", name)
    name = re.sub(r"-{2,}", "-", name)
    name = name.strip("-")
    return name

@activity.defn(name="evaluate_branch_rules")
async def evaluate_branch_rules(input: BranchRuleInput) -> BranchRuleResult:
    if not input.branch_pattern_rules:
        return BranchRuleResult(valid=True, corrected_name=input.proposed_branch_name, applied_corrections=[])

    current_name = input.proposed_branch_name
    corrections: list[str] = []

    for rule in input.branch_pattern_rules:
        rule_type = rule.get("type", "")
        pattern = rule.get("pattern", "")

        if rule_type == "prefix":
            new_name, was_corrected = _apply_prefix_rule(current_name, input.issue_key, pattern)
            if was_corrected:
                corrections.append(f"Applied prefix rule: '{pattern}' -> '{new_name}'")
                current_name = new_name

    current_name = _sanitize_branch_name(current_name)

    if not current_name or len(current_name) > 255:
        return BranchRuleResult(
            valid=False, corrected_name=None, applied_corrections=corrections,
            error=f"Branch name '{current_name}' is invalid after corrections"
        )

    return BranchRuleResult(valid=True, corrected_name=current_name, applied_corrections=corrections)
