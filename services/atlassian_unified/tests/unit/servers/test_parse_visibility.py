"""Unit tests for ``servers.jira._parse_visibility`` (Requirements 27.1-27.4).

The helper is the single choke point for comment-visibility validation on
``jira_add_comment`` / ``jira_update_comment`` / ``jira_create_issue_link``.
The tests below cover the four acceptance criteria from Req 27:

* 27.1 — a well-formed ``{type, value}`` pair is returned unmodified.
* 27.2 — ``type`` without ``value`` returns ``invalid_visibility``.
* 27.3 — ``None`` (omitted visibility) still returns ``None`` (public comment).
* 27.4 — malformed input is rejected before any HTTP side effect; the
  helper's callers (``jira_add_comment`` / ``jira_update_comment``) receive
  a ``StructuredError`` they can return verbatim.

These are pure unit tests — no fetcher, no HTTP, no fastmcp context.
"""

from __future__ import annotations

import pytest

from mcp_atlassian.servers.jira import _parse_visibility
from mcp_atlassian.utils.dc_guards import ERROR_CODES, StructuredError


# ---------------------------------------------------------------------------
# 27.3 — omitted visibility passes through unchanged.
# ---------------------------------------------------------------------------


class TestParseVisibilityNone:
    """``visibility=None`` preserves current public-comment behaviour."""

    def test_none_returns_none(self) -> None:
        assert _parse_visibility(None) is None

    def test_none_with_custom_field_name_returns_none(self) -> None:
        assert _parse_visibility(None, field_name="comment_visibility") is None


# ---------------------------------------------------------------------------
# 27.1 — well-formed {type, value} pairs are returned unchanged.
# ---------------------------------------------------------------------------


class TestParseVisibilityValidPair:
    """Both ``type`` and ``value`` present and non-empty → dict returned."""

    @pytest.mark.parametrize(
        "visibility_json,expected",
        [
            (
                '{"type":"group","value":"jira-users"}',
                {"type": "group", "value": "jira-users"},
            ),
            (
                '{"type":"role","value":"Administrators"}',
                {"type": "role", "value": "Administrators"},
            ),
            (
                '{"type":"group","value":"a"}',
                {"type": "group", "value": "a"},
            ),
        ],
    )
    def test_valid_pair_returns_dict(
        self, visibility_json: str, expected: dict[str, str]
    ) -> None:
        result = _parse_visibility(visibility_json)
        assert result == expected

    def test_extra_keys_are_preserved(self) -> None:
        """Any non-{type,value} keys pass through — upstream decides.

        The helper is a validator, not a sanitizer: it rejects malformed
        visibility but does not strip unknown keys.
        """
        result = _parse_visibility(
            '{"type":"group","value":"jira-users","foo":"bar"}'
        )
        assert result == {"type": "group", "value": "jira-users", "foo": "bar"}


# ---------------------------------------------------------------------------
# 27.2 / 27.4 — structured invalid_visibility on half-specified input.
# ---------------------------------------------------------------------------


class TestParseVisibilityInvalidPair:
    """type without value (or vice versa) → StructuredError, zero HTTP."""

    def test_type_without_value_returns_structured_error(self) -> None:
        result = _parse_visibility('{"type":"group"}')

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.error_code in ERROR_CODES
        assert result.details["reason"] == "value_missing"
        assert result.details["field"] == "visibility"
        assert result.details["type"] == "group"

    def test_value_without_type_returns_structured_error(self) -> None:
        result = _parse_visibility('{"value":"jira-users"}')

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.details["reason"] == "type_missing"
        assert result.details["value"] == "jira-users"

    def test_type_with_empty_string_value_returns_structured_error(self) -> None:
        """Empty-string ``value`` is treated as missing (Req 27.2)."""
        result = _parse_visibility('{"type":"group","value":""}')

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.details["reason"] == "value_missing"

    def test_type_with_whitespace_only_value_returns_structured_error(
        self,
    ) -> None:
        """Whitespace-only ``value`` is treated as missing."""
        result = _parse_visibility('{"type":"role","value":"   "}')

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.details["reason"] == "value_missing"

    def test_empty_string_type_with_value_returns_structured_error(self) -> None:
        result = _parse_visibility('{"type":"","value":"jira-users"}')

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.details["reason"] == "type_missing"

    def test_custom_field_name_is_reflected_in_error(self) -> None:
        """``field_name`` threads through to the error message/details."""
        result = _parse_visibility(
            '{"type":"group"}', field_name="comment_visibility"
        )

        assert isinstance(result, StructuredError)
        assert "comment_visibility" in result.message
        assert result.details["field"] == "comment_visibility"

    def test_invalid_type_value_returns_structured_error(self) -> None:
        """``type`` must be one of ``{role, group}`` (Req 27.1)."""
        result = _parse_visibility(
            '{"type":"project","value":"jira-users"}'
        )

        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"
        assert result.details["reason"] == "invalid_type"
        assert result.details["type"] == "project"
        assert "role" in result.details["allowed_types"]
        assert "group" in result.details["allowed_types"]

    @pytest.mark.parametrize(
        "bogus_type",
        ["ROLE", "Role", "users", "everyone", "public", " group", "group "],
    )
    def test_type_vocabulary_is_case_sensitive_and_trimmed(
        self, bogus_type: str
    ) -> None:
        """Type values are compared case-sensitively to the Jira API contract."""
        result = _parse_visibility(
            f'{{"type":"{bogus_type}","value":"x"}}'
        )
        # " group" / "group " have leading/trailing whitespace and should
        # fail the strict vocabulary check after .strip() is performed by
        # the helper's presence check (which only tests emptiness, not
        # equality).
        assert isinstance(result, StructuredError)
        assert result.error_code == "invalid_visibility"

    def test_to_dict_produces_json_serializable_payload(self) -> None:
        """The error must serialize cleanly for tool responses."""
        import json

        result = _parse_visibility('{"type":"group"}')
        assert isinstance(result, StructuredError)
        # Round-trip through json.dumps with no default fallback.
        payload = json.dumps(result.to_dict())
        decoded = json.loads(payload)
        assert decoded["error_code"] == "invalid_visibility"
        assert decoded["details"]["reason"] == "value_missing"


# ---------------------------------------------------------------------------
# Legacy-compat edge cases — empty dict / missing both fields.
# ---------------------------------------------------------------------------


class TestParseVisibilityEmptyDict:
    """Empty or no-type/no-value dicts pass through (back-compat)."""

    def test_empty_dict_passes_through(self) -> None:
        """``{}`` is a legal no-op — upstream treats it as public."""
        assert _parse_visibility("{}") == {}

    def test_dict_with_only_unrelated_keys_passes_through(self) -> None:
        """A dict without ``type`` or ``value`` is not half-specified."""
        assert _parse_visibility('{"foo":"bar"}') == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Existing contract: malformed JSON still raises ValueError.
# ---------------------------------------------------------------------------


class TestParseVisibilityMalformedJSON:
    """``json.JSONDecodeError`` → ``ValueError`` (unchanged contract)."""

    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="visibility must be a valid JSON"):
            _parse_visibility("{not valid json}")

    def test_json_array_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="visibility must be a valid JSON"):
            _parse_visibility('["group", "jira-users"]')

    def test_json_scalar_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="visibility must be a valid JSON"):
            _parse_visibility('"just-a-string"')

    def test_invalid_json_uses_custom_field_name_in_error(self) -> None:
        with pytest.raises(
            ValueError, match="comment_visibility must be a valid JSON"
        ):
            _parse_visibility("{bad", field_name="comment_visibility")
