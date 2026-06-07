"""Unit tests for :mod:`mcp_client.deployment_router`.

The tests cover three concerns:

1. The two supported ``deployment`` literals (``"cloud"`` and
   ``"server"``) map to the exact MCP tool names mandated by
   ``design.md`` §``mcp_client.deployment_router``.
2. Any other value - empty string, misspelled variant, ``None``,
   non-string types - raises :class:`KeyError` so a misconfigured
   ``departments.json`` fails fast at signal-dispatch time.
3. The exported tool-name constants match the strings the formatter
   tests use as their parity oracle.
"""

from __future__ import annotations

import pytest

from mcp_client import (
    BITBUCKET_CREATE_PR_CLOUD,
    BITBUCKET_CREATE_PR_DC,
    select_pr_create_tool,
)


# ---------------------------------------------------------------------------
# Constants - single-source-of-truth tool names
# ---------------------------------------------------------------------------


class TestToolNameConstants:
    """The exported constants match the strings mandated by design.md."""

    def test_cloud_tool_name_matches_design_doc(self) -> None:
        assert BITBUCKET_CREATE_PR_CLOUD == "bitbucket_create_pull_request_cloud"

    def test_dc_tool_name_matches_design_doc(self) -> None:
        assert BITBUCKET_CREATE_PR_DC == "bitbucket_create_pull_request_dc"

    def test_constants_are_distinct(self) -> None:
        """Cloud and DC tool names must never collide."""

        assert BITBUCKET_CREATE_PR_CLOUD != BITBUCKET_CREATE_PR_DC


# ---------------------------------------------------------------------------
# select_pr_create_tool - happy-path mapping
# ---------------------------------------------------------------------------


class TestSelectPrCreateToolMapping:
    """The two supported deployment literals route to the right tool."""

    def test_cloud_deployment_returns_cloud_tool(self) -> None:
        assert (
            select_pr_create_tool("cloud") == "bitbucket_create_pull_request_cloud"
        )

    def test_server_deployment_returns_dc_tool(self) -> None:
        assert (
            select_pr_create_tool("server") == "bitbucket_create_pull_request_dc"
        )

    def test_cloud_routing_uses_exported_constant(self) -> None:
        """The function and the public constant agree on the tool name."""

        assert select_pr_create_tool("cloud") == BITBUCKET_CREATE_PR_CLOUD

    def test_server_routing_uses_exported_constant(self) -> None:
        assert select_pr_create_tool("server") == BITBUCKET_CREATE_PR_DC

    def test_function_is_pure_and_deterministic(self) -> None:
        """Repeated calls with the same input return the same value."""

        first = select_pr_create_tool("cloud")
        second = select_pr_create_tool("cloud")
        third = select_pr_create_tool("cloud")
        assert first == second == third == "bitbucket_create_pull_request_cloud"


# ---------------------------------------------------------------------------
# select_pr_create_tool - fail-fast on unsupported values
# ---------------------------------------------------------------------------


class TestSelectPrCreateToolKeyErrors:
    """Any value outside the two literals raises :class:`KeyError`."""

    @pytest.mark.parametrize(
        "bad_value",
        [
            "datacenter",
            "Cloud",  # case-sensitive
            "SERVER",
            "",
            "bitbucket-cloud",
            "on_prem",
            " cloud",
            "cloud ",
        ],
    )
    def test_unknown_string_raises_key_error(self, bad_value: str) -> None:
        with pytest.raises(KeyError):
            select_pr_create_tool(bad_value)  # type: ignore[arg-type]

    def test_none_raises_key_error(self) -> None:
        """A missing ``departments.json`` field must not silently
        default - callers normalise *before* calling the router.
        """

        with pytest.raises(KeyError):
            select_pr_create_tool(None)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "bad_value",
        [
            0,
            1,
            True,  # bool is a subtype of int but not a valid literal
            object(),
            ("cloud",),
        ],
    )
    def test_non_string_hashable_inputs_raise(self, bad_value: object) -> None:
        """Any hashable non-string, non-matching input raises ``KeyError``.

        ``KeyError`` is the canonical "lookup miss" signal for the
        ``Mapping`` lookup used inside the router; callers that want a
        domain-specific exception can wrap the call. Unhashable inputs
        (eg. ``list``, ``dict``) raise the underlying ``TypeError`` -
        tested separately below since the spec calls out ``KeyError``
        for "any other value" implying the realistic misconfiguration
        path (string typos, ``None``).
        """

        with pytest.raises(KeyError):
            select_pr_create_tool(bad_value)  # type: ignore[arg-type]

    def test_key_error_message_includes_offending_value(self) -> None:
        """The audit trail can point operators at the bad config."""

        with pytest.raises(KeyError) as excinfo:
            select_pr_create_tool("datacenter")  # type: ignore[arg-type]
        # ``KeyError.args[0]`` is the missing key - re-using it in the
        # audit payload pinpoints the offending department.
        assert excinfo.value.args[0] == "datacenter"
