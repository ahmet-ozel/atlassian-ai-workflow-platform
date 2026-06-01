"""Unit tests for DC guard pre-check helpers (Requirements 41.1, 43.1-43.4,
45.1, 45.3, 46.1, 46.2, 47.2).

Covers the guard functions in ``mcp_atlassian.utils.dc_guards``:

- ``check_read_only`` — belt-and-suspenders read-only enforcement.
- ``check_project_filter`` — per-product allow-list enforcement.
- ``parse_dc_version`` / ``compare_dc_versions`` — DC semver-lite helpers.
- ``require_owner`` — owner-scoped delete identity check.
- ``build_receipt`` — reversible-receipt construction.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mcp_atlassian.utils.dc_guards import (
    ERROR_CODES,
    StructuredError,
    build_receipt,
    check_mode_supported,
    check_project_filter,
    check_read_only,
    compare_dc_versions,
    parse_dc_version,
    require_owner,
)


# ---------------------------------------------------------------------------
# check_read_only
# ---------------------------------------------------------------------------


class TestCheckReadOnlyWriteTools:
    """Write-tagged tools are blocked only when READ_ONLY_MODE is truthy."""

    def test_write_tool_with_env_unset_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset READ_ONLY_MODE leaves writes enabled."""
        monkeypatch.delenv("READ_ONLY_MODE", raising=False)

        result = check_read_only({"bitbucket", "write", "toolset:bitbucket_webhooks"})

        assert result is None

    def test_write_tool_with_env_true_returns_structured_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """READ_ONLY_MODE=true blocks a write-tagged tool with the expected code."""
        monkeypatch.setenv("READ_ONLY_MODE", "true")

        result = check_read_only({"bitbucket", "write", "toolset:bitbucket_webhooks"})

        assert isinstance(result, StructuredError)
        assert result.error_code == "read_only_mode"
        assert result.error_code in ERROR_CODES
        # The error must be JSON-serializable via to_dict() so tools can splat
        # it directly into their {"success": False, ...} response.
        payload = result.to_dict()
        assert payload["error_code"] == "read_only_mode"
        assert payload["details"] == {"read_only_mode": True}

    @pytest.mark.parametrize(
        "truthy_value",
        ["true", "TRUE", "True", "1", "yes", "YES", "y", "Y", "on", "ON"],
    )
    def test_truthy_variations_block_writes(
        self, monkeypatch: pytest.MonkeyPatch, truthy_value: str
    ) -> None:
        """All extended-truthy variants block writes (case-insensitive)."""
        monkeypatch.setenv("READ_ONLY_MODE", truthy_value)

        result = check_read_only({"jira", "write", "toolset:jira_filters"})

        assert isinstance(result, StructuredError)
        assert result.error_code == "read_only_mode"

    @pytest.mark.parametrize(
        "falsy_value",
        ["false", "FALSE", "0", "", "no", "off", "nope", "disabled"],
    )
    def test_falsy_variations_leave_writes_enabled(
        self, monkeypatch: pytest.MonkeyPatch, falsy_value: str
    ) -> None:
        """Only the extended-truthy set blocks writes; everything else passes."""
        monkeypatch.setenv("READ_ONLY_MODE", falsy_value)

        result = check_read_only({"confluence", "write", "toolset:confluence_pages"})

        assert result is None


class TestCheckReadOnlyReadTools:
    """Read-tagged tools always proceed, regardless of READ_ONLY_MODE."""

    def test_read_tool_with_env_unset_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("READ_ONLY_MODE", raising=False)

        result = check_read_only({"bitbucket", "read", "toolset:bitbucket_webhooks"})

        assert result is None

    def test_read_tool_with_env_true_still_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-only mode is a write-blocker; read tools must always pass."""
        monkeypatch.setenv("READ_ONLY_MODE", "true")

        result = check_read_only({"bitbucket", "read", "toolset:bitbucket_webhooks"})

        assert result is None

    @pytest.mark.parametrize(
        "truthy_value", ["true", "1", "yes", "y", "on", "YES"]
    )
    def test_read_tool_passes_for_any_truthy_env(
        self, monkeypatch: pytest.MonkeyPatch, truthy_value: str
    ) -> None:
        monkeypatch.setenv("READ_ONLY_MODE", truthy_value)

        result = check_read_only({"jira", "read", "toolset:jira_filters"})

        assert result is None

    def test_tool_with_neither_read_nor_write_tag_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absence of the ``write`` tag short-circuits to ``None``."""
        monkeypatch.setenv("READ_ONLY_MODE", "true")

        result = check_read_only({"bitbucket", "toolset:bitbucket_default_reviewers"})

        assert result is None


# ---------------------------------------------------------------------------
# check_project_filter
# ---------------------------------------------------------------------------


PRODUCTS = ["bitbucket", "jira", "confluence"]


class TestCheckProjectFilterUnset:
    """An unset or empty filter env means "no filter configured"."""

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_filter_env_none_returns_none(self, product: str) -> None:
        assert check_project_filter(product, "TEST", None) is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_filter_env_empty_returns_none(self, product: str) -> None:
        assert check_project_filter(product, "TEST", "") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_filter_env_whitespace_only_returns_none(self, product: str) -> None:
        assert check_project_filter(product, "TEST", "   ") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_filter_env_only_blank_tokens_returns_none(self, product: str) -> None:
        """``",,"`` and ``",  ,"`` collapse to an empty allow-list."""
        assert check_project_filter(product, "TEST", ",,") is None
        assert check_project_filter(product, "TEST", ",  ,") is None


class TestCheckProjectFilterAllowed:
    """Keys present in the allow-list pass through."""

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_single_key_allowed(self, product: str) -> None:
        assert check_project_filter(product, "TEST", "TEST") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_key_in_multi_token_allow_list(self, product: str) -> None:
        assert check_project_filter(product, "TEST", "TEST,DEMO") is None
        assert check_project_filter(product, "DEMO", "TEST,DEMO") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_whitespace_tolerated_in_tokens(self, product: str) -> None:
        """Whitespace inside the env var is stripped per token."""
        assert check_project_filter(product, "TEST", " TEST , DEMO ") is None
        assert check_project_filter(product, "DEMO", " TEST , DEMO ") is None


class TestCheckProjectFilterCaseInsensitive:
    """Matching is case-insensitive on both sides."""

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_lowercase_key_matches_uppercase_allow_list(self, product: str) -> None:
        assert check_project_filter(product, "test", "TEST,DEMO") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_uppercase_key_matches_lowercase_allow_list(self, product: str) -> None:
        assert check_project_filter(product, "TEST", "test,demo") is None

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_mixed_case_key_and_allow_list(self, product: str) -> None:
        assert check_project_filter(product, "TeSt", "tEsT,DeMo") is None


class TestCheckProjectFilterDenied:
    """Keys outside the allow-list produce a ``filtered_out`` error."""

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_key_not_in_allow_list_returns_structured_error(
        self, product: str
    ) -> None:
        result = check_project_filter(product, "OTHER", "TEST,DEMO")

        assert isinstance(result, StructuredError)
        assert result.error_code == "filtered_out"
        assert result.error_code in ERROR_CODES
        assert result.details["product"] == product
        assert result.details["key"] == "OTHER"
        assert result.details["allowed"] == ["TEST", "DEMO"]

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_structured_error_to_dict_is_json_shaped(self, product: str) -> None:
        """The serialized shape is what tool responses splat into ``success=False``."""
        result = check_project_filter(product, "OTHER", "TEST,DEMO")

        assert isinstance(result, StructuredError)
        payload = result.to_dict()
        assert payload["error_code"] == "filtered_out"
        assert payload["details"] == {
            "product": product,
            "key": "OTHER",
            "allowed": ["TEST", "DEMO"],
        }
        assert isinstance(payload["message"], str)
        assert payload["message"]  # non-empty

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_denied_with_whitespace_padded_allow_list(self, product: str) -> None:
        """Denial path still normalizes and reports the cleaned allow-list."""
        result = check_project_filter(product, "OTHER", " TEST , DEMO ")

        assert isinstance(result, StructuredError)
        assert result.error_code == "filtered_out"
        assert result.details["allowed"] == ["TEST", "DEMO"]


# ---------------------------------------------------------------------------
# parse_dc_version
# ---------------------------------------------------------------------------


class TestParseDcVersionValid:
    """Parseable version strings produce the expected integer-segment tuple."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Two- / three- / four-segment plain versions.
            ("9.4", (9, 4)),
            ("9.4.0", (9, 4, 0)),
            ("9.4.0.1", (9, 4, 0, 1)),
            # Tagged / pre-release variants: everything after the first
            # non-numeric, non-dot character is discarded.
            ("5.4-SNAPSHOT", (5, 4)),
            ("8.8.0-beta1", (8, 8, 0)),
            # Build-suffix style with a space boundary.
            ("9.4 (build 1)", (9, 4)),
            # Non-numeric segment after a dot terminates parsing at that
            # boundary rather than raising.
            ("9.4.x", (9, 4)),
        ],
    )
    def test_returns_expected_tuple(
        self, raw: str, expected: tuple[int, ...]
    ) -> None:
        assert parse_dc_version(raw) == expected


class TestParseDcVersionIndeterminate:
    """Missing / blank / non-numeric inputs short-circuit to ``None``."""

    def test_none_returns_none(self) -> None:
        assert parse_dc_version(None) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty string
            "   ",  # whitespace only
            "not-a-version",  # leading non-numeric
            "abc",  # no numeric content at all
        ],
    )
    def test_blank_or_non_numeric_returns_none(self, raw: str) -> None:
        assert parse_dc_version(raw) is None


# ---------------------------------------------------------------------------
# compare_dc_versions
# ---------------------------------------------------------------------------


class TestCompareDcVersionsEqual:
    """Versions that match after zero-padding compare equal (``0``)."""

    @pytest.mark.parametrize(
        ("detected", "required"),
        [
            ("9.4.0", "9.4"),  # longer detected, shorter required
            ("9.4", "9.4.0"),  # shorter detected, longer required
            ("9.4", "9.4"),  # identical two-segment
            ("5.4-SNAPSHOT", "5.4"),  # snapshot tag stripped
            ("8.8.0-beta1", "8.8"),  # pre-release tag stripped + padded
        ],
    )
    def test_equal_after_padding(self, detected: str, required: str) -> None:
        assert compare_dc_versions(detected, required) == 0


class TestCompareDcVersionsOrdered:
    """Detected vs required comparisons respect element-wise ordering."""

    def test_detected_greater_returns_one(self) -> None:
        """``"9.4.1" > "9.4"`` after padding required to ``(9, 4, 0)``."""
        assert compare_dc_versions("9.4.1", "9.4") == 1

    def test_detected_less_returns_minus_one(self) -> None:
        """``"9.2.1" < "9.4"`` on the second segment."""
        assert compare_dc_versions("9.2.1", "9.4") == -1

    def test_minor_version_below_required_returns_minus_one(self) -> None:
        """``"8.7" < "8.8"`` on the second segment."""
        assert compare_dc_versions("8.7", "8.8") == -1


class TestCompareDcVersionsIndeterminate:
    """Unparseable / missing operands produce ``None`` rather than raising."""

    def test_none_detected_short_circuits_to_none(self) -> None:
        """The ``None`` short-circuit lets the caller fall through to the
        upstream call and map 404/501 to ``dc_version_unknown``.
        """
        assert compare_dc_versions(None, "9.4") is None

    def test_bogus_detected_returns_none(self) -> None:
        assert compare_dc_versions("bogus", "9.4") is None

    def test_bogus_required_returns_none(self) -> None:
        """A malformed required minimum is treated as indeterminate rather
        than raising, so a future typo in a call site cannot crash a tool.
        """
        assert compare_dc_versions("9.4", "bogus") is None


# ---------------------------------------------------------------------------
# require_owner
# ---------------------------------------------------------------------------


def _make_fetcher(
    *, username: str | None = None, include_config: bool = True,
    current_user_name: str | None = None,
) -> SimpleNamespace:
    """Build a minimal fake fetcher for owner-check tests.

    The real product clients expose ``fetcher.config.username`` and
    (optionally) ``fetcher._current_user_name``. ``require_owner`` only
    reads those two attributes, so a ``SimpleNamespace`` stand-in is
    sufficient and avoids pulling the real client classes (and their HTTP
    dependencies) into a pure-logic unit test.
    """
    fetcher: SimpleNamespace = SimpleNamespace()
    if include_config:
        fetcher.config = SimpleNamespace(username=username)
    if current_user_name is not None:
        fetcher._current_user_name = current_user_name
    return fetcher


class TestRequireOwnerMatch:
    """Owner matches short-circuit to ``None`` (allow the destructive call)."""

    def test_exact_case_sensitive_match_returns_none(self) -> None:
        """Straightforward match: identical casing on both sides."""
        fetcher = _make_fetcher(username="alice")

        assert require_owner(fetcher, "alice") is None

    def test_case_insensitive_match_returns_none(self) -> None:
        """DC usernames are case-insensitive at the application level."""
        fetcher = _make_fetcher(username="Alice")

        assert require_owner(fetcher, "alice") is None

    def test_whitespace_is_stripped_before_compare(self) -> None:
        """Leading / trailing whitespace on either side does not block match."""
        fetcher = _make_fetcher(username=" alice ")

        assert require_owner(fetcher, "alice") is None

    def test_whitespace_on_owner_id_is_stripped(self) -> None:
        """The object-owner string is also stripped before compare."""
        fetcher = _make_fetcher(username="alice")

        assert require_owner(fetcher, "  alice  ") is None

    def test_fallback_to_current_user_name_when_config_username_missing(
        self,
    ) -> None:
        """When ``config.username`` is ``None`` the cached myself attr is used."""
        fetcher = _make_fetcher(
            username=None, current_user_name="alice"
        )

        assert require_owner(fetcher, "alice") is None


class TestRequireOwnerMismatch:
    """Owner mismatches produce a ``not_owner`` StructuredError."""

    def test_plain_mismatch_returns_structured_error(self) -> None:
        fetcher = _make_fetcher(username="alice")

        result = require_owner(fetcher, "bob")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"
        assert result.error_code in ERROR_CODES
        assert result.details["object_owner_id"] == "bob"
        assert result.details["authenticated_user"] == "alice"

    def test_mismatch_to_dict_is_json_shaped(self) -> None:
        fetcher = _make_fetcher(username="alice")

        result = require_owner(fetcher, "bob")

        assert isinstance(result, StructuredError)
        payload = result.to_dict()
        assert payload["error_code"] == "not_owner"
        assert payload["details"] == {
            "object_owner_id": "bob",
            "authenticated_user": "alice",
        }
        assert isinstance(payload["message"], str) and payload["message"]


class TestRequireOwnerUnresolvedAuthenticatedUser:
    """Fail closed when the authenticated user cannot be resolved."""

    def test_config_username_none_returns_not_owner(self) -> None:
        """``config.username`` is ``None`` and no fallback cache is set."""
        fetcher = _make_fetcher(username=None)

        result = require_owner(fetcher, "alice")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"
        assert result.details["authenticated_user"] is None
        assert result.details["object_owner_id"] == "alice"

    def test_no_config_attribute_returns_not_owner(self) -> None:
        """Fetcher has no ``config`` attribute at all."""
        fetcher = _make_fetcher(include_config=False)

        result = require_owner(fetcher, "alice")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"
        assert result.details["authenticated_user"] is None

    def test_empty_config_username_returns_not_owner(self) -> None:
        """Whitespace-only ``config.username`` is treated as unresolvable."""
        fetcher = _make_fetcher(username="   ")

        result = require_owner(fetcher, "alice")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"
        assert result.details["authenticated_user"] is None


class TestRequireOwnerMissingOwnerId:
    """Fail closed when the object owner id is missing or empty."""

    def test_empty_object_owner_id_returns_not_owner(self) -> None:
        """Empty owner id blocks the destructive call even with a valid user."""
        fetcher = _make_fetcher(username="alice")

        result = require_owner(fetcher, "")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"
        assert result.details["object_owner_id"] == ""
        assert result.details["authenticated_user"] == "alice"

    def test_whitespace_only_object_owner_id_returns_not_owner(self) -> None:
        """Whitespace-only owner id normalizes to empty and is rejected."""
        fetcher = _make_fetcher(username="alice")

        result = require_owner(fetcher, "   ")

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_owner"


# ---------------------------------------------------------------------------
# build_receipt
# ---------------------------------------------------------------------------


RECEIPT_KEYS: frozenset[str] = frozenset(
    {"object_id", "inverse_tool", "inverse_args", "note", "recipient_scope"}
)


class TestBuildReceiptRetractable:
    """Retractable receipts carry both the inverse tool and its arguments."""

    def test_full_receipt_with_inverse(self) -> None:
        """Webhook-style receipt: id + inverse_tool + inverse_args + scope."""
        receipt = build_receipt(
            "123",
            "bitbucket_delete_webhook",
            {"project_key": "TEST", "webhook_id": 123},
            None,
            {"url": "https://x.com"},
        )

        assert receipt == {
            "object_id": "123",
            "inverse_tool": "bitbucket_delete_webhook",
            "inverse_args": {"project_key": "TEST", "webhook_id": 123},
            "note": None,
            "recipient_scope": {"url": "https://x.com"},
        }
        assert set(receipt.keys()) == RECEIPT_KEYS

    def test_all_five_keys_present(self) -> None:
        """Even when values are ``None``, every receipt key must appear."""
        receipt = build_receipt(
            "123",
            "bitbucket_delete_webhook",
            {"project_key": "TEST", "webhook_id": 123},
            None,
            {"url": "https://x.com"},
        )

        for key in RECEIPT_KEYS:
            assert key in receipt


class TestBuildReceiptNonRetractable:
    """Non-retractable receipts carry a ``note`` instead of an inverse tool."""

    def test_non_retractable_note_only(self) -> None:
        """Notify-style receipt: ``inverse_*`` fields are ``None``, note explains."""
        receipt = build_receipt(
            "ISS-1",
            None,
            None,
            "Email sends are not retractable",
            {"recipient_count": 12},
        )

        assert receipt == {
            "object_id": "ISS-1",
            "inverse_tool": None,
            "inverse_args": None,
            "note": "Email sends are not retractable",
            "recipient_scope": {"recipient_count": 12},
        }
        assert set(receipt.keys()) == RECEIPT_KEYS

    def test_non_retractable_inverse_fields_are_none(self) -> None:
        """The two inverse fields are distinctly ``None`` (not just missing)."""
        receipt = build_receipt(
            "ISS-1",
            None,
            None,
            "Email sends are not retractable",
            {"recipient_count": 12},
        )

        assert receipt["inverse_tool"] is None
        assert receipt["inverse_args"] is None
        assert receipt["note"] == "Email sends are not retractable"


class TestBuildReceiptDefaults:
    """Optional ``recipient_scope`` defaults to ``None`` but is always emitted."""

    def test_default_recipient_scope_none(self) -> None:
        """Caller can omit recipient_scope; key still present with ``None``."""
        receipt = build_receipt("123", "tool", {"k": "v"}, None)

        assert "recipient_scope" in receipt
        assert receipt["recipient_scope"] is None
        assert set(receipt.keys()) == RECEIPT_KEYS

    def test_explicit_none_recipient_scope_matches_default(self) -> None:
        """Passing ``None`` explicitly produces the same shape as the default."""
        implicit = build_receipt("123", "tool", {"k": "v"}, None)
        explicit = build_receipt("123", "tool", {"k": "v"}, None, None)

        assert implicit == explicit


class TestBuildReceiptShape:
    """Stable, JSON-serializable shape across retractable + non-retractable."""

    def test_keys_are_exactly_the_five_expected(self) -> None:
        """No stray keys, no missing keys."""
        receipt = build_receipt(
            "123",
            "bitbucket_delete_webhook",
            {"project_key": "TEST", "webhook_id": 123},
            None,
            {"url": "https://x.com"},
        )

        assert set(receipt.keys()) == RECEIPT_KEYS

    def test_receipt_is_json_serializable(self) -> None:
        """The tool response serializes the receipt with ``json.dumps``."""
        receipt = build_receipt(
            "123",
            "bitbucket_delete_webhook",
            {"project_key": "TEST", "webhook_id": 123},
            None,
            {"url": "https://x.com"},
        )

        serialized = json.dumps(receipt)
        assert json.loads(serialized) == receipt

    def test_non_retractable_receipt_is_json_serializable(self) -> None:
        """The non-retractable branch also round-trips through JSON cleanly."""
        receipt = build_receipt(
            "ISS-1",
            None,
            None,
            "Email sends are not retractable",
            {"recipient_count": 12},
        )

        serialized = json.dumps(receipt)
        assert json.loads(serialized) == receipt

    def test_each_call_returns_a_fresh_dict(self) -> None:
        """Two calls must produce independent dicts, not a shared singleton."""
        first = build_receipt("123", "tool", {"k": "v"}, None)
        second = build_receipt("123", "tool", {"k": "v"}, None)

        assert first == second
        assert first is not second

    def test_mutating_returned_dict_does_not_affect_subsequent_calls(
        self,
    ) -> None:
        """A caller mutating their receipt must not bleed into the next call."""
        first = build_receipt("123", "tool", {"k": "v"}, None)
        first["object_id"] = "mutated"

        second = build_receipt("123", "tool", {"k": "v"}, None)

        assert second["object_id"] == "123"


# ---------------------------------------------------------------------------
# check_mode_supported (Requirements 14.10, 15.4, 15.5)
# ---------------------------------------------------------------------------


class TestCheckModeSupportedMatch:
    """Effective mode == required mode returns ``None`` (tool may proceed)."""

    def test_cloud_required_and_cloud_effective_returns_none(self) -> None:
        """CloudMode client invoking a Cloud-only tool proceeds unblocked."""
        assert (
            check_mode_supported(
                is_cloud=True,
                required_mode="cloud",
                tool_name="bitbucket_future_cloud_only_tool",
            )
            is None
        )

    def test_dc_required_and_dc_effective_returns_none(self) -> None:
        """DCMode client invoking a DC-only tool proceeds unblocked."""
        assert (
            check_mode_supported(
                is_cloud=False,
                required_mode="dc",
                tool_name="bitbucket_render_markup",
            )
            is None
        )


class TestCheckModeSupportedMismatch:
    """Effective mode != required mode produces a structured mode-mismatch error."""

    def test_cloud_effective_dc_required_returns_not_supported_on_cloud(
        self,
    ) -> None:
        """Cloud mode + DC-only tool -> ``not_supported_on_cloud`` pre-HTTP."""
        result = check_mode_supported(
            is_cloud=True,
            required_mode="dc",
            tool_name="bitbucket_render_markup",
        )

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_supported_on_cloud"
        assert result.error_code in ERROR_CODES
        assert result.details == {
            "tool": "bitbucket_render_markup",
            "effective_mode": "cloud",
            "required_mode": "dc",
        }
        # The error must serialize cleanly for ``{"success": False, ...}``
        # response splatting.
        payload = result.to_dict()
        assert payload["error_code"] == "not_supported_on_cloud"
        assert payload["details"] == {
            "tool": "bitbucket_render_markup",
            "effective_mode": "cloud",
            "required_mode": "dc",
        }
        assert isinstance(payload["message"], str) and payload["message"]

    def test_dc_effective_cloud_required_returns_not_supported_on_dc(
        self,
    ) -> None:
        """DC mode + Cloud-only tool -> ``not_supported_on_dc`` pre-HTTP."""
        result = check_mode_supported(
            is_cloud=False,
            required_mode="cloud",
            tool_name="bitbucket_future_cloud_only_tool",
        )

        assert isinstance(result, StructuredError)
        assert result.error_code == "not_supported_on_dc"
        assert result.error_code in ERROR_CODES
        assert result.details == {
            "tool": "bitbucket_future_cloud_only_tool",
            "effective_mode": "dc",
            "required_mode": "cloud",
        }

    def test_mismatch_message_names_tool_and_mode(self) -> None:
        """Human-readable message carries the tool name and the effective mode."""
        result = check_mode_supported(
            is_cloud=True,
            required_mode="dc",
            tool_name="bitbucket_fork_repository",
        )

        assert isinstance(result, StructuredError)
        assert "bitbucket_fork_repository" in result.message
        assert "cloud" in result.message
        assert "dc" in result.message


class TestCheckModeSupportedDetailsShape:
    """The ``details`` payload is fixed-shape across every mismatch branch."""

    @pytest.mark.parametrize(
        ("is_cloud", "required_mode", "expected_effective", "expected_code"),
        [
            (True, "dc", "cloud", "not_supported_on_cloud"),
            (False, "cloud", "dc", "not_supported_on_dc"),
        ],
    )
    def test_details_has_exactly_three_keys(
        self,
        is_cloud: bool,
        required_mode: str,
        expected_effective: str,
        expected_code: str,
    ) -> None:
        """Every mode-mismatch error details dict has exactly tool / effective_mode / required_mode."""
        result = check_mode_supported(
            is_cloud=is_cloud,
            required_mode=required_mode,  # type: ignore[arg-type]
            tool_name="bitbucket_some_tool",
        )

        assert isinstance(result, StructuredError)
        assert result.error_code == expected_code
        assert set(result.details.keys()) == {
            "tool",
            "effective_mode",
            "required_mode",
        }
        assert result.details["tool"] == "bitbucket_some_tool"
        assert result.details["effective_mode"] == expected_effective
        assert result.details["required_mode"] == required_mode


# ---------------------------------------------------------------------------
# ERROR_CODES allowlist membership (Requirements 15.1, 15.2, 15.4, 15.5)
# ---------------------------------------------------------------------------


class TestErrorCodesAllowlistModeCodes:
    """``not_supported_on_cloud`` and ``not_supported_on_dc`` are first-class entries."""

    def test_not_supported_on_cloud_in_allowlist(self) -> None:
        """Req 15.1: ``not_supported_on_cloud`` is a member of ERROR_CODES."""
        assert "not_supported_on_cloud" in ERROR_CODES

    def test_not_supported_on_dc_in_allowlist(self) -> None:
        """Req 15.2: ``not_supported_on_dc`` is a member of ERROR_CODES."""
        assert "not_supported_on_dc" in ERROR_CODES

    def test_allowlist_is_a_frozenset(self) -> None:
        """ERROR_CODES is a frozenset so membership tests are O(1) and immutable."""
        assert isinstance(ERROR_CODES, frozenset)


class TestStructuredErrorAllowlistConstruction:
    """Requirements 15.4, 15.5: allowlist membership is enforced at construction time."""

    def test_construct_with_not_supported_on_cloud_succeeds(self) -> None:
        """Req 15.4: a StructuredError with an allowlisted code constructs cleanly."""
        err = StructuredError(
            error_code="not_supported_on_cloud",
            message="Tool 'x' is not supported on cloud Bitbucket.",
            details={"tool": "x", "effective_mode": "cloud", "required_mode": "dc"},
        )

        assert err.error_code == "not_supported_on_cloud"
        assert err.error_code in ERROR_CODES

    def test_construct_with_not_supported_on_dc_succeeds(self) -> None:
        """Req 15.4: symmetric code for the reverse mismatch also constructs cleanly."""
        err = StructuredError(
            error_code="not_supported_on_dc",
            message="Tool 'y' is not supported on dc Bitbucket.",
            details={"tool": "y", "effective_mode": "dc", "required_mode": "cloud"},
        )

        assert err.error_code == "not_supported_on_dc"
        assert err.error_code in ERROR_CODES

    @pytest.mark.parametrize(
        "bogus_code",
        [
            "not_supported",  # truncated variant
            "not_supported_on_server",  # plausible typo
            "cloud_unsupported",  # alternate ordering
            "",  # empty string
            "NOT_SUPPORTED_ON_CLOUD",  # wrong casing
        ],
    )
    def test_construct_with_non_allowlist_code_raises_value_error(
        self, bogus_code: str
    ) -> None:
        """Req 15.5: any code outside the allowlist raises ``ValueError``."""
        with pytest.raises(ValueError, match="Unknown error_code"):
            StructuredError(
                error_code=bogus_code,
                message="irrelevant",
                details={},
            )
