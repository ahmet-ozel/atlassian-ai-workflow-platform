"""Streamlit Task Auto-Assign Bot component.

Provides the "Bot'a ata" checkbox and auto-assignment logic for
the task creation form.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class AutoAssignResult:
    assigned_to_bot: bool
    bot_account_id: str | None
    warning: str | None = None

def resolve_bot_account_id(dept_config: dict[str, Any]) -> str | None:
    return dept_config.get("bot_account_id")

def get_auto_assign_decision(
    assign_to_bot_checked: bool,
    dept_config: dict[str, Any],
) -> AutoAssignResult:
    if not assign_to_bot_checked:
        return AutoAssignResult(assigned_to_bot=False, bot_account_id=None)

    bot_id = resolve_bot_account_id(dept_config)
    if bot_id is None:
        _logger.warning("Bot account_id not configured for department")
        return AutoAssignResult(
            assigned_to_bot=False,
            bot_account_id=None,
            warning="Bu departman için bot tanımı bulunamadı. Görev assignee alanı boş olarak oluşturulacak."
        )

    return AutoAssignResult(assigned_to_bot=True, bot_account_id=bot_id)
