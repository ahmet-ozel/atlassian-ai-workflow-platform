"""Property test: Task auto-assign bot resolution.

Feature: platform-completion, Property 27: For any task creation with "Bot'a ata"
active, the Streamlit_Assistant SHALL resolve the bot account_id from the
selected department's configuration.

Validates: Requirements 13.2
"""
from __future__ import annotations
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SERVICE_ROOT))

from components.task_auto_assign import (
    AutoAssignResult,
    get_auto_assign_decision,
    resolve_bot_account_id,
)


@settings(max_examples=200, deadline=None)
@given(
    bot_id=st.one_of(st.none(), st.from_regex(r"[a-z0-9]{8,32}", fullmatch=True)),
)
def test_resolve_returns_configured_id(bot_id: str | None) -> None:
    """resolve_bot_account_id returns the configured value or None."""
    config = {} if bot_id is None else {"bot_account_id": bot_id}
    result = resolve_bot_account_id(config)
    assert result == bot_id


@settings(max_examples=200, deadline=None)
@given(
    bot_id=st.from_regex(r"[a-z0-9]{8,32}", fullmatch=True),
)
def test_active_with_bot_id_assigns(bot_id: str) -> None:
    """When checked + bot_id present → assigned to bot."""
    config = {"bot_account_id": bot_id}
    result = get_auto_assign_decision(True, config)
    assert result.assigned_to_bot is True
    assert result.bot_account_id == bot_id
    assert result.warning is None


@settings(max_examples=100, deadline=None)
@given(
    has_bot=st.booleans(),
)
def test_unchecked_never_assigns(has_bot: bool) -> None:
    """When unchecked → never assigned regardless of config."""
    config = {"bot_account_id": "abc123"} if has_bot else {}
    result = get_auto_assign_decision(False, config)
    assert result.assigned_to_bot is False
    assert result.bot_account_id is None


@settings(max_examples=50, deadline=None)
@given(extra=st.text(max_size=20))
def test_active_without_bot_id_warns(extra: str) -> None:
    """Checked + no bot_id → warning, no assignment."""
    config = {"other_field": extra}
    result = get_auto_assign_decision(True, config)
    assert result.assigned_to_bot is False
    assert result.bot_account_id is None
    assert result.warning is not None
