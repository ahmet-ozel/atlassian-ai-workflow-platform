"""Tests for toolset utility functions."""

import pytest

from mcp_atlassian.utils.toolsets import (
    ALL_TOOLSETS,
    DEFAULT_TOOLSETS,
    TOOLSET_TAG_PREFIX,
    get_enabled_toolsets,
    get_toolset_tag,
    should_include_tool_by_toolset,
)


class TestGetEnabledToolsets:
    """Tests for get_enabled_toolsets() env var parsing."""

    @pytest.mark.parametrize(
        "env_value, expected",
        [
            pytest.param(None, set(ALL_TOOLSETS.keys()), id="unset_uses_all"),
            pytest.param("", set(ALL_TOOLSETS.keys()), id="empty_uses_all"),
            pytest.param(" , , ", set(ALL_TOOLSETS.keys()), id="whitespace_uses_all"),
            pytest.param("jira_agile", {"jira_agile"}, id="single_toolset"),
            pytest.param("typo_name", set(), id="unknown_name_fail_closed"),
        ],
    )
    def test_basic_parsing(self, env_value, expected, monkeypatch):
        """Test basic env var parsing cases."""
        monkeypatch.delenv("TOOLSETS", raising=False)
        if env_value is not None:
            monkeypatch.setenv("TOOLSETS", env_value)
        result = get_enabled_toolsets()
        assert result == expected

    def test_all_keyword(self, monkeypatch):
        """Test 'all' keyword returns every toolset name."""
        monkeypatch.setenv("TOOLSETS", "all")
        result = get_enabled_toolsets()
        assert result is not None
        assert result == set(ALL_TOOLSETS.keys())
        # 24 Jira + 17 Confluence + 14 Bitbucket (after atlassian-dc-tool-parity)
        assert len(result) == 55

    def test_all_keyword_case_insensitive(self, monkeypatch):
        """Test 'ALL' keyword is case-insensitive."""
        monkeypatch.setenv("TOOLSETS", "ALL")
        result = get_enabled_toolsets()
        assert result is not None
        assert result == set(ALL_TOOLSETS.keys())
        assert len(result) == 55

    def test_default_keyword(self, monkeypatch):
        """Test 'default' keyword returns the default toolset names."""
        monkeypatch.setenv("TOOLSETS", "default")
        result = get_enabled_toolsets()
        assert result is not None
        assert result == DEFAULT_TOOLSETS
        # 4 Jira defaults + 2 Confluence defaults + 2 Bitbucket defaults
        assert len(result) == 8

    def test_default_plus_extra(self, monkeypatch):
        """Test 'default,jira_agile' returns defaults + jira_agile."""
        monkeypatch.setenv("TOOLSETS", "default,jira_agile")
        result = get_enabled_toolsets()
        assert result is not None
        assert result == DEFAULT_TOOLSETS | {"jira_agile"}

    def test_mixed_valid_and_unknown(self, monkeypatch):
        """Test 'default,typo_name' returns defaults only (typo ignored)."""
        monkeypatch.setenv("TOOLSETS", "default, typo_name")
        result = get_enabled_toolsets()
        assert result is not None
        assert result == DEFAULT_TOOLSETS

    def test_whitespace_handling(self, monkeypatch):
        """Test whitespace around toolset names is stripped."""
        monkeypatch.setenv("TOOLSETS", " jira_issues , jira_fields ")
        result = get_enabled_toolsets()
        assert result == {"jira_issues", "jira_fields"}

    def test_default_toolsets_content(self):
        """Verify the default toolsets contain expected names."""
        expected_defaults = {
            "jira_issues",
            "jira_fields",
            "jira_comments",
            "jira_transitions",
            "confluence_pages",
            "confluence_comments",
            "bitbucket_repositories",
            "bitbucket_pull_requests",
        }
        assert DEFAULT_TOOLSETS == expected_defaults

    def test_all_toolsets_count(self):
        """Verify ALL_TOOLSETS has exactly 55 entries (Jira + Confluence + Bitbucket)."""
        assert len(ALL_TOOLSETS) == 55

    def test_all_toolsets_contains_jira_confluence_and_bitbucket(self):
        """Verify ALL_TOOLSETS has Jira, Confluence, and Bitbucket toolsets."""
        jira_toolsets = {k for k in ALL_TOOLSETS if k.startswith("jira_")}
        confluence_toolsets = {k for k in ALL_TOOLSETS if k.startswith("confluence_")}
        bitbucket_toolsets = {k for k in ALL_TOOLSETS if k.startswith("bitbucket_")}
        assert len(jira_toolsets) == 24  # 15 base + 9 added by dc-parity
        assert len(confluence_toolsets) == 17  # 7 base + 10 added by dc-parity
        assert len(bitbucket_toolsets) == 14  # 8 base + 6 added by dc-parity


class TestShouldIncludeToolByToolset:
    """Tests for should_include_tool_by_toolset() tag-based filtering."""

    @pytest.mark.parametrize(
        "tool_tags, enabled_toolsets, expected",
        [
            pytest.param(
                {"jira", "read", "toolset:jira_issues"},
                {"jira_issues"},
                True,
                id="matching_toolset",
            ),
            pytest.param(
                {"jira", "read", "toolset:jira_agile"},
                {"jira_issues"},
                False,
                id="non_matching_toolset",
            ),
            pytest.param(
                {"jira", "read", "toolset:jira_issues"},
                None,
                True,
                id="none_means_all_pass",
            ),
            pytest.param(
                {"jira", "read"},
                {"jira_issues"},
                True,
                id="no_toolset_tag_passes",
            ),
            pytest.param(
                {"jira", "read", "toolset:jira_issues"},
                set(),
                False,
                id="empty_set_blocks_all",
            ),
        ],
    )
    def test_filtering(self, tool_tags, enabled_toolsets, expected):
        """Test tool filtering by toolset tags."""
        result = should_include_tool_by_toolset(tool_tags, enabled_toolsets)
        assert result is expected

    def test_multiple_enabled_toolsets(self):
        """Test tool matches when multiple toolsets are enabled."""
        tool_tags = {"jira", "read", "toolset:jira_agile"}
        enabled = {"jira_issues", "jira_agile", "jira_fields"}
        assert should_include_tool_by_toolset(tool_tags, enabled) is True

    def test_tool_with_non_matching_multiple_enabled(self):
        """Test tool excluded when its toolset is not in enabled set."""
        tool_tags = {"jira", "read", "toolset:jira_worklog"}
        enabled = {"jira_issues", "jira_agile", "jira_fields"}
        assert should_include_tool_by_toolset(tool_tags, enabled) is False


class TestGetToolsetTag:
    """Tests for get_toolset_tag() helper."""

    def test_extracts_toolset_tag(self):
        """Test extraction of toolset tag from tag set."""
        tags = {"jira", "read", "toolset:jira_issues"}
        assert get_toolset_tag(tags) == "jira_issues"

    def test_no_toolset_tag(self):
        """Test returns None when no toolset tag exists."""
        tags = {"jira", "read"}
        assert get_toolset_tag(tags) is None

    def test_empty_tags(self):
        """Test returns None for empty tag set."""
        assert get_toolset_tag(set()) is None


class TestToolsetTagCompleteness:
    """Verify every registered tool has exactly one valid toolset tag."""

    @pytest.fixture()
    def jira_tools(self):
        """Get all registered Jira tools."""
        import asyncio

        from mcp_atlassian.servers.jira import jira_mcp

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(jira_mcp.get_tools())
        finally:
            loop.close()

    @pytest.fixture()
    def confluence_tools(self):
        """Get all registered Confluence tools."""
        import asyncio

        from mcp_atlassian.servers.confluence import confluence_mcp

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(confluence_mcp.get_tools())
        finally:
            loop.close()

    def test_jira_tools_have_toolset_tag(self, jira_tools):
        """Every Jira tool must have exactly one toolset:* tag."""
        for name, tool in jira_tools.items():
            tags = tool.tags if hasattr(tool, "tags") else set()
            toolset_tags = [t for t in tags if t.startswith(TOOLSET_TAG_PREFIX)]
            assert len(toolset_tags) == 1, (
                f"Jira tool '{name}' has {len(toolset_tags)} toolset tags "
                f"(expected 1): {toolset_tags}"
            )

    def test_confluence_tools_have_toolset_tag(self, confluence_tools):
        """Every Confluence tool must have exactly one toolset:* tag."""
        for name, tool in confluence_tools.items():
            tags = tool.tags if hasattr(tool, "tags") else set()
            toolset_tags = [t for t in tags if t.startswith(TOOLSET_TAG_PREFIX)]
            assert len(toolset_tags) == 1, (
                f"Confluence tool '{name}' has {len(toolset_tags)} toolset "
                f"tags (expected 1): {toolset_tags}"
            )

    def test_jira_toolset_tags_are_valid(self, jira_tools):
        """Every Jira tool's toolset tag must reference a valid toolset."""
        for name, tool in jira_tools.items():
            tags = tool.tags if hasattr(tool, "tags") else set()
            toolset_name = get_toolset_tag(tags)
            if toolset_name is not None:
                assert toolset_name in ALL_TOOLSETS, (
                    f"Jira tool '{name}' has unknown toolset "
                    f"'{toolset_name}' (not in ALL_TOOLSETS)"
                )

    def test_confluence_toolset_tags_are_valid(self, confluence_tools):
        """Every Confluence tool's toolset tag must reference a valid toolset."""
        for name, tool in confluence_tools.items():
            tags = tool.tags if hasattr(tool, "tags") else set()
            toolset_name = get_toolset_tag(tags)
            if toolset_name is not None:
                assert toolset_name in ALL_TOOLSETS, (
                    f"Confluence tool '{name}' has unknown toolset "
                    f"'{toolset_name}' (not in ALL_TOOLSETS)"
                )

    def test_jira_tool_count(self, jira_tools):
        """Verify expected number of Jira tools.

        Baseline (pre-DC-parity): 53 tools.
        atlassian-dc-tool-parity (Req 15-27 + 33) adds 28 tools across
        9 new toolsets (filters, dashboards, notifications, lookups,
        permissions, users [myself + mentions], groups, project_roles,
        screens, archive) plus new issue-votes tools — totaling 81.
        """
        assert len(jira_tools) == 81, f"Expected 81 Jira tools, got {len(jira_tools)}"

    def test_confluence_tool_count(self, confluence_tools):
        """Verify expected number of Confluence tools.

        Baseline (pre-DC-parity): 28 tools.
        atlassian-dc-tool-parity (Req 28-40) adds 25 tools across 10
        new toolsets (restrictions, watchers, space_admin, templates,
        page_properties, archive, search, tasks, likes, groups) plus
        page_move/copy, long-task polling, and descendants under
        confluence_pages — totaling 53.
        """
        assert len(confluence_tools) == 53, (
            f"Expected 53 Confluence tools, got {len(confluence_tools)}"
        )


# ---------------------------------------------------------------------------
# Extended toolsets registry (atlassian-dc-tool-parity task 5.2)
# ---------------------------------------------------------------------------

# 25 toolsets added by the atlassian-dc-tool-parity feature:
# - 9 Jira, 10 Confluence, 6 Bitbucket
NEW_JIRA_TOOLSETS = frozenset(
    {
        "jira_filters",
        "jira_dashboards",
        "jira_notifications",
        "jira_lookups",
        "jira_permissions",
        "jira_groups",
        "jira_project_roles",
        "jira_screens",
        "jira_archive",
    }
)

NEW_CONFLUENCE_TOOLSETS = frozenset(
    {
        "confluence_restrictions",
        "confluence_watchers",
        "confluence_space_admin",
        "confluence_templates",
        "confluence_page_properties",
        "confluence_archive",
        "confluence_search",
        "confluence_tasks",
        "confluence_likes",
        "confluence_groups",
    }
)

NEW_BITBUCKET_TOOLSETS = frozenset(
    {
        "bitbucket_default_reviewers",
        "bitbucket_webhooks",
        "bitbucket_required_builds",
        "bitbucket_repository_admin",
        "bitbucket_project_admin",
        "bitbucket_deployments",
    }
)

NEW_TOOLSETS = NEW_JIRA_TOOLSETS | NEW_CONFLUENCE_TOOLSETS | NEW_BITBUCKET_TOOLSETS

BROADCAST_CAPABLE_TOOLSETS = frozenset({"bitbucket_webhooks", "jira_notifications"})


class TestExtendedToolsetsRegistry:
    """Tests for the 25 toolsets added by the atlassian-dc-tool-parity feature.

    Validates Requirements 42.1, 42.2, and 47.1:
    - Every new toolset name resolves from ALL_TOOLSETS (42.1, 42.2)
    - Broadcast-capable toolsets are never in DEFAULT_TOOLSETS (47.1)
    """

    def test_new_toolsets_count(self):
        """Sanity-check: feature adds exactly 25 toolsets."""
        assert len(NEW_TOOLSETS) == 25
        assert len(NEW_JIRA_TOOLSETS) == 9
        assert len(NEW_CONFLUENCE_TOOLSETS) == 10
        assert len(NEW_BITBUCKET_TOOLSETS) == 6

    @pytest.mark.parametrize("name", sorted(NEW_TOOLSETS))
    def test_new_toolset_is_registered(self, name):
        """Each new toolset name SHALL resolve from ALL_TOOLSETS.

        Validates: Requirements 42.1, 42.2
        """
        assert name in ALL_TOOLSETS, (
            f"Toolset '{name}' is missing from ALL_TOOLSETS; "
            f"Requirement 42.1 requires every new tool to belong to a "
            f"named toolset."
        )

    @pytest.mark.parametrize("name", sorted(NEW_TOOLSETS))
    def test_new_toolset_is_opt_in(self, name):
        """Each new toolset SHALL be default=False (operator opt-in).

        The feature adds 25 new toolsets, all opt-in; extending the default
        set would silently expand the default tool surface for operators.
        """
        defn = ALL_TOOLSETS[name]
        assert defn.default is False, (
            f"Toolset '{name}' is marked default=True but new toolsets "
            f"must be opt-in."
        )
        assert name not in DEFAULT_TOOLSETS

    @pytest.mark.parametrize("name", sorted(BROADCAST_CAPABLE_TOOLSETS))
    def test_broadcast_capable_toolset_not_in_defaults(self, name):
        """Broadcast-capable toolsets SHALL NOT be in DEFAULT_TOOLSETS.

        Validates: Requirement 47.1

        Tools that broadcast externally (webhook deliveries, email
        notifications) must be opted in explicitly via TOOLSETS=...
        """
        assert name in ALL_TOOLSETS, (
            f"Broadcast-capable toolset '{name}' is missing from ALL_TOOLSETS."
        )
        assert name not in DEFAULT_TOOLSETS, (
            f"Broadcast-capable toolset '{name}' must not be default-enabled "
            f"(Requirement 47.1)."
        )

    def test_default_selection_excludes_broadcast_capable(self, monkeypatch):
        """TOOLSETS=default SHALL NOT enable any broadcast-capable toolset.

        Validates: Requirement 47.1
        """
        monkeypatch.setenv("TOOLSETS", "default")
        enabled = get_enabled_toolsets()
        assert enabled.isdisjoint(BROADCAST_CAPABLE_TOOLSETS), (
            f"'default' selection unexpectedly enabled broadcast-capable "
            f"toolsets: {enabled & BROADCAST_CAPABLE_TOOLSETS}"
        )

    def test_default_selection_excludes_all_new_toolsets(self, monkeypatch):
        """TOOLSETS=default SHALL NOT enable any of the 25 new toolsets.

        The feature is purely additive and opt-in, so the default selection
        must remain unchanged.
        """
        monkeypatch.setenv("TOOLSETS", "default")
        enabled = get_enabled_toolsets()
        assert enabled.isdisjoint(NEW_TOOLSETS), (
            f"'default' selection unexpectedly enabled new opt-in toolsets: "
            f"{enabled & NEW_TOOLSETS}"
        )

    @pytest.mark.parametrize("name", sorted(BROADCAST_CAPABLE_TOOLSETS))
    def test_explicit_opt_in_enables_broadcast_capable(self, name, monkeypatch):
        """Explicit TOOLSETS=<broadcast_toolset> SHALL include it.

        Validates: Requirement 47.1 (opt-in path works)
        """
        monkeypatch.setenv("TOOLSETS", name)
        enabled = get_enabled_toolsets()
        assert name in enabled, (
            f"Explicit TOOLSETS={name} should enable the toolset but "
            f"got: {sorted(enabled)}"
        )
