"""Unit tests for decision.loop_guard module.

Validates loop guard predicates: is_self_actor, is_bot_assignee,
assignee_changed_to_bot, and route.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the automation-service src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from decision.loop_guard import (
    assignee_changed_to_bot,
    is_bot_assignee,
    is_self_actor,
    route,
)


class TestIsSelfActor:
    """Tests for is_self_actor predicate."""

    def test_actor_in_registry_returns_true(self) -> None:
        bots = frozenset({"bot-001", "bot-002"})
        assert is_self_actor("bot-001", bots) is True

    def test_actor_not_in_registry_returns_false(self) -> None:
        bots = frozenset({"bot-001", "bot-002"})
        assert is_self_actor("user-123", bots) is False

    def test_none_actor_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        assert is_self_actor(None, bots) is False

    def test_empty_registry_returns_false(self) -> None:
        bots: frozenset[str] = frozenset()
        assert is_self_actor("bot-001", bots) is False

    def test_actor_matches_second_bot(self) -> None:
        bots = frozenset({"bot-001", "bot-002", "bot-003"})
        assert is_self_actor("bot-002", bots) is True


class TestIsBotAssignee:
    """Tests for is_bot_assignee predicate."""

    def test_assignee_in_registry_returns_true(self) -> None:
        bots = frozenset({"bot-001", "bot-002"})
        assert is_bot_assignee("bot-001", bots) is True

    def test_assignee_not_in_registry_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        assert is_bot_assignee("user-456", bots) is False

    def test_none_assignee_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        assert is_bot_assignee(None, bots) is False

    def test_empty_registry_returns_false(self) -> None:
        bots: frozenset[str] = frozenset()
        assert is_bot_assignee("bot-001", bots) is False


class TestAssigneeChangedToBot:
    """Tests for assignee_changed_to_bot predicate."""

    def test_assignee_changed_to_bot_returns_true(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "assignee", "from": "user-123", "to": "bot-001"}
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is True

    def test_assignee_changed_to_non_bot_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "assignee", "from": "user-123", "to": "user-456"}
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_no_assignee_field_in_changelog_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "status", "from": "Open", "to": "In Progress"}
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_none_changelog_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        assert assignee_changed_to_bot(None, bots) is False

    def test_empty_items_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {"items": []}
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_missing_items_key_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog: dict = {}
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_assignee_removed_to_none_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "assignee", "from": "bot-001", "to": None}
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_multi_field_changelog_with_assignee_to_bot(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "status", "from": "Open", "to": "In Progress"},
                {"field": "priority", "from": "Medium", "to": "High"},
                {"field": "assignee", "from": "user-123", "to": "bot-001"},
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is True

    def test_multi_field_changelog_without_assignee_returns_false(self) -> None:
        bots = frozenset({"bot-001"})
        changelog = {
            "items": [
                {"field": "status", "from": "Open", "to": "Done"},
                {"field": "priority", "from": "Low", "to": "High"},
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is False

    def test_assignee_changed_to_second_bot(self) -> None:
        bots = frozenset({"bot-001", "bot-002"})
        changelog = {
            "items": [
                {"field": "assignee", "from": "user-123", "to": "bot-002"}
            ]
        }
        assert assignee_changed_to_bot(changelog, bots) is True


class TestRoute:
    """Tests for route event-type classifier."""

    def test_jira_issue_created_accepted(self) -> None:
        assert route("jira:issue_created") == "accepted"

    def test_jira_issue_assigned_accepted(self) -> None:
        assert route("jira:issue_assigned") == "accepted"

    def test_jira_issue_updated_accepted(self) -> None:
        assert route("jira:issue_updated") == "accepted"

    def test_jira_comment_created_accepted(self) -> None:
        assert route("jira:comment_created") == "accepted"

    def test_pullrequest_reviewer_added_accepted(self) -> None:
        assert route("pullrequest:reviewer_added") == "accepted"

    def test_pullrequest_comment_created_accepted(self) -> None:
        assert route("pullrequest:comment_created") == "accepted"

    def test_unknown_event_type_ignored(self) -> None:
        assert route("jira:issue_deleted") == "ignored"

    def test_empty_string_ignored(self) -> None:
        assert route("") == "ignored"

    def test_arbitrary_string_ignored(self) -> None:
        assert route("some:random:event") == "ignored"

    def test_partial_match_ignored(self) -> None:
        assert route("jira:issue_create") == "ignored"
