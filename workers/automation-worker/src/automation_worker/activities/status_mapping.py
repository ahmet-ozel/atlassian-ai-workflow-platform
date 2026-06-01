"""Status Mapping resolver activity.

Resolves logical status values to Jira-specific status names using
department configuration. Implements fallback transformation when
no mapping is found.

Requirements: 19.1, 19.2, 19.3, 19.4, 19.5
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any
from temporalio import activity

__all__ = ("resolve_jira_status", "StatusMappingResult", "SUPPORTED_LOGICAL_STATES")

_logger = logging.getLogger(__name__)

SUPPORTED_LOGICAL_STATES: frozenset[str] = frozenset({
    "todo", "in_progress", "review", "done", "out_of_scope"
})

@dataclass(frozen=True)
class StatusMappingResult:
    resolved: bool
    jira_status: str | None
    used_fallback: bool
    error: str | None = None

def _fallback_transform(logical_status: str) -> str:
    return logical_status.replace("_", " ").title()

@activity.defn(name="resolve_jira_status")
async def resolve_jira_status(
    logical_status: str,
    dept_config_status_mapping: dict[str, str] | None,
) -> StatusMappingResult:
    if logical_status not in SUPPORTED_LOGICAL_STATES:
        _logger.warning("Invalid logical status: %s", logical_status)
        return StatusMappingResult(
            resolved=False, jira_status=None, used_fallback=False,
            error=f"Invalid logical status '{logical_status}'. Must be one of: {sorted(SUPPORTED_LOGICAL_STATES)}"
        )

    if dept_config_status_mapping:
        mapping_lower = {k.lower(): v for k, v in dept_config_status_mapping.items()}
        jira_status = mapping_lower.get(logical_status.lower())
        if jira_status:
            return StatusMappingResult(resolved=True, jira_status=jira_status, used_fallback=False)

    fallback = _fallback_transform(logical_status)
    _logger.info("Using fallback transform for '%s' -> '%s'", logical_status, fallback)
    return StatusMappingResult(resolved=True, jira_status=fallback, used_fallback=True)
