"""Toolset definitions and filtering utilities for MCP Atlassian.

Groups tools into named toolsets controlled via the TOOLSETS env var.
Supports 'all', 'default', and comma-separated toolset names.
"""

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TOOLSET_TAG_PREFIX = "toolset:"


@dataclass(frozen=True)
class ToolsetDefinition:
    """Metadata for a named toolset group."""

    name: str
    description: str
    default: bool


# --- Jira toolsets (24) ---

JIRA_TOOLSETS: dict[str, ToolsetDefinition] = {
    "jira_issues": ToolsetDefinition(
        name="jira_issues",
        description="Core issue operations: CRUD, search, batch, changelogs",
        default=True,
    ),
    "jira_fields": ToolsetDefinition(
        name="jira_fields",
        description="Field search and option retrieval",
        default=True,
    ),
    "jira_comments": ToolsetDefinition(
        name="jira_comments",
        description="Issue comment operations",
        default=True,
    ),
    "jira_transitions": ToolsetDefinition(
        name="jira_transitions",
        description="Workflow transition operations",
        default=True,
    ),
    "jira_projects": ToolsetDefinition(
        name="jira_projects",
        description="Project, version, and component management",
        default=False,
    ),
    "jira_agile": ToolsetDefinition(
        name="jira_agile",
        description="Agile boards, sprints, and related operations",
        default=False,
    ),
    "jira_links": ToolsetDefinition(
        name="jira_links",
        description="Issue links, epic links, and remote links",
        default=False,
    ),
    "jira_worklog": ToolsetDefinition(
        name="jira_worklog",
        description="Time tracking and worklog operations",
        default=False,
    ),
    "jira_attachments": ToolsetDefinition(
        name="jira_attachments",
        description="Attachment download and image retrieval",
        default=False,
    ),
    "jira_users": ToolsetDefinition(
        name="jira_users",
        description="User profile operations",
        default=False,
    ),
    "jira_watchers": ToolsetDefinition(
        name="jira_watchers",
        description="Issue watcher operations",
        default=False,
    ),
    "jira_service_desk": ToolsetDefinition(
        name="jira_service_desk",
        description="Jira Service Management queues and service desks",
        default=False,
    ),
    "jira_forms": ToolsetDefinition(
        name="jira_forms",
        description="ProForma form operations",
        default=False,
    ),
    "jira_metrics": ToolsetDefinition(
        name="jira_metrics",
        description="Issue dates and SLA metrics",
        default=False,
    ),
    "jira_development": ToolsetDefinition(
        name="jira_development",
        description="Development info (branches, PRs, commits)",
        default=False,
    ),
    "jira_filters": ToolsetDefinition(
        name="jira_filters",
        description=(
            "Saved JQL filter management (list, get, search, create, update, "
            "owner-scoped delete)"
        ),
        default=False,
    ),
    "jira_dashboards": ToolsetDefinition(
        name="jira_dashboards",
        description="Dashboard discovery (list, get, search) — read-only",
        default=False,
    ),
    "jira_notifications": ToolsetDefinition(
        name="jira_notifications",
        description=(
            "Issue email notifications (notify). Broadcast-capable — emails are "
            "not retractable; opt in explicitly."
        ),
        default=False,
    ),
    "jira_lookups": ToolsetDefinition(
        name="jira_lookups",
        description=(
            "Instance-wide lookups (priorities, resolutions, statuses, issue types) "
            "— read-only, not project-scoped"
        ),
        default=False,
    ),
    "jira_permissions": ToolsetDefinition(
        name="jira_permissions",
        description="Per-issue my-permissions check — read-only",
        default=False,
    ),
    "jira_groups": ToolsetDefinition(
        name="jira_groups",
        description=(
            "Group discovery (list groups, list a user's groups) — read-only; "
            "no membership writes"
        ),
        default=False,
    ),
    "jira_project_roles": ToolsetDefinition(
        name="jira_project_roles",
        description="Project roles and actors — read-only",
        default=False,
    ),
    "jira_screens": ToolsetDefinition(
        name="jira_screens",
        description="Issue create/edit screen field metadata — read-only",
        default=False,
    ),
    "jira_archive": ToolsetDefinition(
        name="jira_archive",
        description=(
            "Issue archive and restore (DC 9.4+). Reversible via the paired "
            "restore tool."
        ),
        default=False,
    ),
}

# --- Confluence toolsets (17) ---

CONFLUENCE_TOOLSETS: dict[str, ToolsetDefinition] = {
    "confluence_pages": ToolsetDefinition(
        name="confluence_pages",
        description="Page CRUD, search, children, and history",
        default=True,
    ),
    "confluence_comments": ToolsetDefinition(
        name="confluence_comments",
        description="Page comment operations",
        default=True,
    ),
    "confluence_labels": ToolsetDefinition(
        name="confluence_labels",
        description="Page label operations",
        default=False,
    ),
    "confluence_users": ToolsetDefinition(
        name="confluence_users",
        description="User search operations",
        default=False,
    ),
    "confluence_analytics": ToolsetDefinition(
        name="confluence_analytics",
        description="Page view analytics",
        default=False,
    ),
    "confluence_attachments": ToolsetDefinition(
        name="confluence_attachments",
        description="Attachment upload, download, and management",
        default=False,
    ),
    "confluence_spaces": ToolsetDefinition(
        name="confluence_spaces",
        description="Space discovery (list spaces, contributed spaces)",
        default=False,
    ),
    "confluence_restrictions": ToolsetDefinition(
        name="confluence_restrictions",
        description=(
            "Page content restrictions — list, set (with prior-state receipt), clear"
        ),
        default=False,
    ),
    "confluence_watchers": ToolsetDefinition(
        name="confluence_watchers",
        description=(
            "Page watchers — list watchers, self-watch, self-unwatch "
            "(self-scoped only)"
        ),
        default=False,
    ),
    "confluence_space_admin": ToolsetDefinition(
        name="confluence_space_admin",
        description="Space permissions inspection — read-only",
        default=False,
    ),
    "confluence_templates": ToolsetDefinition(
        name="confluence_templates",
        description="Templates and blueprints — list and create pages from templates",
        default=False,
    ),
    "confluence_page_properties": ToolsetDefinition(
        name="confluence_page_properties",
        description=(
            "Structured page properties — list, get, set (idempotent), delete"
        ),
        default=False,
    ),
    "confluence_archive": ToolsetDefinition(
        name="confluence_archive",
        description=(
            "Archive pages and spaces, restore archived pages — no permanent "
            "delete primitives"
        ),
        default=False,
    ),
    "confluence_search": ToolsetDefinition(
        name="confluence_search",
        description=(
            "Advanced CQL search with sort, pagination, and space-filter awareness"
        ),
        default=False,
    ),
    "confluence_tasks": ToolsetDefinition(
        name="confluence_tasks",
        description="Inline tasks on pages — read-only",
        default=False,
    ),
    "confluence_likes": ToolsetDefinition(
        name="confluence_likes",
        description=(
            "Page likes (like / unlike). Plugin-gated; falls back to "
            "plugin_unavailable when the likes plugin is absent."
        ),
        default=False,
    ),
    "confluence_groups": ToolsetDefinition(
        name="confluence_groups",
        description=(
            "Group discovery (search groups, list a user's groups) — read-only; "
            "no membership writes"
        ),
        default=False,
    ),
}

# --- Bitbucket toolsets (14) ---

BITBUCKET_TOOLSETS: dict[str, ToolsetDefinition] = {
    "bitbucket_repositories": ToolsetDefinition(
        name="bitbucket_repositories",
        description=(
            "Repository and project operations (list, get, search, file content, "
            "browse, create/update/delete files via commits)"
        ),
        default=True,
    ),
    "bitbucket_pull_requests": ToolsetDefinition(
        name="bitbucket_pull_requests",
        description=(
            "Pull request operations (list, get, create, update, merge, approve, decline, "
            "reopen, reviewers, comments, inline comments, diff, changes, merge-status)"
        ),
        default=True,
    ),
    "bitbucket_pr_tasks": ToolsetDefinition(
        name="bitbucket_pr_tasks",
        description=(
            "Pull request tasks (blocker comments) — create, list, resolve, reopen"
        ),
        default=False,
    ),
    "bitbucket_branches": ToolsetDefinition(
        name="bitbucket_branches",
        description=(
            "Branch and tag operations (list, create, delete branches and tags; "
            "read-only branch restrictions)"
        ),
        default=False,
    ),
    "bitbucket_commits": ToolsetDefinition(
        name="bitbucket_commits",
        description=(
            "Commit operations (list, get, changes, diff, compare refs, code search)"
        ),
        default=False,
    ),
    "bitbucket_code_insights": ToolsetDefinition(
        name="bitbucket_code_insights",
        description=(
            "Code Insights reports and annotations (SonarQube / Snyk / Trivy style "
            "quality data surfaced inline on PRs)"
        ),
        default=False,
    ),
    "bitbucket_users": ToolsetDefinition(
        name="bitbucket_users",
        description="User lookup and search (used when adding reviewers, etc.)",
        default=False,
    ),
    "bitbucket_builds": ToolsetDefinition(
        name="bitbucket_builds",
        description="CI build status — read and publish per-commit build state",
        default=False,
    ),
    "bitbucket_default_reviewers": ToolsetDefinition(
        name="bitbucket_default_reviewers",
        description=(
            "Repository default reviewer rules — list, get, create, update, delete"
        ),
        default=False,
    ),
    "bitbucket_webhooks": ToolsetDefinition(
        name="bitbucket_webhooks",
        description=(
            "Repository webhooks CRUD (DC 5.4+). Broadcast-capable — webhook "
            "deliveries hit external URLs; opt in explicitly. Secrets are "
            "redacted from responses."
        ),
        default=False,
    ),
    "bitbucket_required_builds": ToolsetDefinition(
        name="bitbucket_required_builds",
        description=(
            "Required-builds merge checks (list, create, delete). Plugin-gated; "
            "falls back to plugin_unavailable when the required-builds plugin "
            "is absent."
        ),
        default=False,
    ),
    "bitbucket_repository_admin": ToolsetDefinition(
        name="bitbucket_repository_admin",
        description=(
            "Repository admin writes (create, update, fork) — no delete "
            "primitives"
        ),
        default=False,
    ),
    "bitbucket_project_admin": ToolsetDefinition(
        name="bitbucket_project_admin",
        description=(
            "Project admin writes (create, update) — no delete primitives"
        ),
        default=False,
    ),
    "bitbucket_deployments": ToolsetDefinition(
        name="bitbucket_deployments",
        description=(
            "Deployments (list, get) — read-only (DC 7.10+)"
        ),
        default=False,
    ),
}

# --- Combined registry ---

ALL_TOOLSETS: dict[str, ToolsetDefinition] = {
    **JIRA_TOOLSETS,
    **CONFLUENCE_TOOLSETS,
    **BITBUCKET_TOOLSETS,
}

DEFAULT_TOOLSETS: set[str] = {
    name for name, defn in ALL_TOOLSETS.items() if defn.default
}


def get_enabled_toolsets() -> set[str]:
    """Parse the TOOLSETS env var into a set of enabled toolset names.

    Supports keywords 'all' (all registered toolsets) and 'default'
    (default-on toolsets), plus comma-separated specific toolset names.
    Case-insensitive for keywords.

    When TOOLSETS is unset or empty, returns all toolsets with a deprecation
    warning. In v0.22.0 the default will change to DEFAULT_TOOLSETS.
    Set ``TOOLSETS=all`` explicitly to preserve current behavior.

    Returns:
        A set of valid toolset names. Defaults to all toolsets when unset.
        Unknown names are silently dropped with a warning. If only unknown
        names are given, returns an empty set (fail-closed).

    Examples:
        TOOLSETS unset -> all toolsets (with deprecation warning)
        TOOLSETS="" -> all toolsets (with deprecation warning)
        TOOLSETS="all" -> all toolset names
        TOOLSETS="default" -> default toolset names
        TOOLSETS="default,jira_agile" -> defaults + jira_agile
        TOOLSETS="typo_name" -> set() (fail-closed)
    """
    toolsets_str = os.getenv("TOOLSETS")
    if not toolsets_str:
        logger.info("TOOLSETS not set — all toolsets enabled.")
        logger.warning(
            "TOOLSETS is not set — currently defaults to all toolsets. "
            "In v0.22.0, the default will change to DEFAULT_TOOLSETS. "
            "Set TOOLSETS=all explicitly to preserve current behavior."
        )
        return set(ALL_TOOLSETS.keys())

    # Split by comma and strip whitespace, filter empty tokens
    tokens = [t.strip() for t in toolsets_str.split(",")]
    tokens = [t for t in tokens if t]

    if not tokens:
        logger.info("TOOLSETS empty — all toolsets enabled.")
        logger.warning(
            "TOOLSETS is not set — currently defaults to all toolsets. "
            "In v0.22.0, the default will change to DEFAULT_TOOLSETS. "
            "Set TOOLSETS=all explicitly to preserve current behavior."
        )
        return set(ALL_TOOLSETS.keys())

    result: set[str] = set()

    for token in tokens:
        normalized = token.lower()
        if normalized == "all":
            logger.info("TOOLSETS: 'all' keyword — enabling all toolsets.")
            return set(ALL_TOOLSETS.keys())
        elif normalized == "default":
            logger.info("TOOLSETS: 'default' keyword — adding default toolsets.")
            result |= DEFAULT_TOOLSETS
        elif token in ALL_TOOLSETS:
            result.add(token)
        else:
            logger.warning(f"TOOLSETS: unknown toolset name '{token}' — ignoring.")

    if result:
        logger.info(f"TOOLSETS: enabled toolsets: {sorted(result)}")
    else:
        logger.warning(
            "TOOLSETS: no valid toolset names found — all tools will be blocked (fail-closed)."
        )

    return result


def should_include_tool_by_toolset(
    tool_tags: set[str], enabled_toolsets: set[str] | None
) -> bool:
    """Check if a tool should be included based on toolset filtering.

    Args:
        tool_tags: The tool's tag set (e.g. {"jira", "read", "toolset:jira_issues"}).
        enabled_toolsets: Set of enabled toolset names, or None to include all tools.

    Returns:
        True if the tool should be included, False otherwise.
        Tools without a toolset tag are always included (graceful fallback).
    """
    if enabled_toolsets is None:
        return True

    toolset_name = get_toolset_tag(tool_tags)
    if toolset_name is None:
        logger.warning(
            f"Tool has no toolset tag in {tool_tags} — including by default."
        )
        return True

    return toolset_name in enabled_toolsets


def get_toolset_tag(tags: set[str]) -> str | None:
    """Extract the toolset name from a tool's tag set.

    Args:
        tags: The tool's tag set.

    Returns:
        The toolset name (without prefix) if found, None otherwise.
    """
    for tag in tags:
        if tag.startswith(TOOLSET_TAG_PREFIX):
            return tag[len(TOOLSET_TAG_PREFIX) :]
    return None
