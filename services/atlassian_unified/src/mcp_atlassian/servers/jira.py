"""Jira FastMCP server instance and tool definitions."""

import base64
import json
import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent
from pydantic import Field
from requests.exceptions import HTTPError

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.jira.constants import DEFAULT_READ_JIRA_FIELDS
from mcp_atlassian.jira.forms_common import convert_datetime_to_timestamp
from mcp_atlassian.models.jira import JiraAttachment
from mcp_atlassian.models.jira.common import JiraUser
from mcp_atlassian.servers.dependencies import get_jira_fetcher
from mcp_atlassian.utils import dc_guards
from mcp_atlassian.utils.decorators import check_write_access
from mcp_atlassian.utils.secret_redaction import redact_secrets
from mcp_atlassian.utils.media import (
    ATTACHMENT_MAX_BYTES,
    fetch_and_encode_attachment,
    is_image_attachment,
)

logger = logging.getLogger(__name__)


# Regex patterns for Jira key validation.
# Per Atlassian docs, Cloud project keys are 2-10 chars. Server/Data Center
# allows longer keys (configurable). We accept any length to support both.
# Underscores are also allowed to support non-standard project key formats
ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9_]+-\d+$"
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]+$"

jira_mcp = FastMCP(
    name="Jira MCP Service",
    instructions="Provides tools for interacting with Atlassian Jira.",
)


_VALID_VISIBILITY_TYPES: frozenset[str] = frozenset({"role", "group"})


def _parse_visibility(
    visibility: str | None,
    field_name: str = "visibility",
) -> dict[str, str] | dc_guards.StructuredError | None:
    """Parse a visibility JSON string into a dict and validate ``{type, value}``.

    The helper preserves backwards-compatible behaviour for the two trivial
    cases: ``None`` in → ``None`` out (public comment, Req 27.3), and
    malformed JSON still raises :class:`ValueError` (existing contract).

    When the parsed object is a dict, the helper additionally enforces the
    shape required by Req 27.1 / 27.2 / 27.4:

    - both ``type`` and ``value`` must be present and non-empty, or
    - both must be absent (any other dict keys are ignored by upstream), or
    - ``type`` must be one of ``{"role", "group"}``.

    When any of those conditions is violated, the helper returns a
    :class:`~mcp_atlassian.utils.dc_guards.StructuredError` with
    ``error_code="invalid_visibility"`` so the calling tool can return it
    directly and issue zero POST/PUT against the comment endpoint.

    Args:
        visibility: JSON string like ``'{"type":"group","value":"jira-users"}'``
            or ``None`` for a public comment.
        field_name: Parameter name used in error messages (``"visibility"`` for
            ``jira_add_comment`` / ``jira_update_comment``, or
            ``"comment_visibility"`` for ``jira_create_issue_link``).

    Returns:
        * ``None`` when ``visibility`` was ``None`` (public comment).
        * A ``dict`` when the input is a well-formed ``{type, value}`` pair or
          an empty dict (back-compat: caller may treat empty-dict as public).
        * A :class:`StructuredError` with ``error_code="invalid_visibility"``
          when the dict has ``type`` without ``value`` (or vice versa), or
          when ``type`` is not one of ``{"role", "group"}``.

    Raises:
        ValueError: If the input is not valid JSON or the decoded value is
            not a JSON object.
    """
    if visibility is None:
        return None
    try:
        parsed = json.loads(visibility)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{field_name} must be a valid JSON object, e.g. "
            f'{{"type":"group","value":"jira-users"}}; got error: {e}'
        ) from e
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{field_name} must be a valid JSON object, e.g. "
            '{"type":"group","value":"jira-users"}'
        )

    vis_type_raw = parsed.get("type")
    vis_value_raw = parsed.get("value")

    # Normalize: treat non-string / empty / whitespace-only as "missing".
    def _is_present(v: Any) -> bool:
        return isinstance(v, str) and v.strip() != ""

    has_type = _is_present(vis_type_raw)
    has_value = _is_present(vis_value_raw)

    # Empty dict or dict missing both fields: preserve legacy passthrough.
    # Callers treat an empty visibility dict as no restriction.
    if not has_type and not has_value:
        return parsed

    # Req 27.2 (and mirror): exactly one of type/value present is invalid.
    if has_type and not has_value:
        return dc_guards.StructuredError(
            error_code="invalid_visibility",
            message=(
                f"{field_name}.type was provided without {field_name}.value; "
                "both fields are required when restricting comment visibility."
            ),
            details={
                "field": field_name,
                "reason": "value_missing",
                "type": vis_type_raw,
            },
        )
    if has_value and not has_type:
        return dc_guards.StructuredError(
            error_code="invalid_visibility",
            message=(
                f"{field_name}.value was provided without {field_name}.type; "
                "both fields are required when restricting comment visibility."
            ),
            details={
                "field": field_name,
                "reason": "type_missing",
                "value": vis_value_raw,
            },
        )

    # Both present — validate the type vocabulary.
    assert isinstance(vis_type_raw, str)  # narrowed by _is_present
    if vis_type_raw not in _VALID_VISIBILITY_TYPES:
        return dc_guards.StructuredError(
            error_code="invalid_visibility",
            message=(
                f"{field_name}.type must be one of "
                f"{sorted(_VALID_VISIBILITY_TYPES)}; got {vis_type_raw!r}."
            ),
            details={
                "field": field_name,
                "reason": "invalid_type",
                "type": vis_type_raw,
                "allowed_types": sorted(_VALID_VISIBILITY_TYPES),
            },
        )

    return parsed


def _parse_additional_fields(
    additional_fields: dict[str, Any] | str | None,
) -> dict[str, Any]:
    """Parse additional_fields from dict or JSON string.

    Args:
        additional_fields: Dict, JSON string, or None.

    Returns:
        Parsed dict of additional fields.

    Raises:
        ValueError: If the input is not valid JSON or not a dict.
    """
    if additional_fields is None:
        return {}
    if isinstance(additional_fields, dict):
        return additional_fields
    if isinstance(additional_fields, str):
        try:
            parsed = json.loads(additional_fields)
            if not isinstance(parsed, dict):
                raise ValueError(
                    "Parsed additional_fields is not a JSON object (dict)."
                )
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(f"additional_fields is not valid JSON: {e}") from e
    raise ValueError("additional_fields must be a dictionary or JSON string.")


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_users"},
    annotations={"title": "Get User Profile", "readOnlyHint": True},
)
async def get_user_profile(
    ctx: Context,
    user_identifier: Annotated[
        str,
        Field(
            description="Identifier for the user (e.g., email address 'user@example.com', username 'johndoe', account ID 'accountid:...', or key for Server/DC)."
        ),
    ],
) -> str:
    """
    Retrieve profile information for a specific Jira user.

    Args:
        ctx: The FastMCP context.
        user_identifier: User identifier (email, username, key, or account ID).

    Returns:
        JSON string representing the Jira user profile object, or an error object if not found.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        user: JiraUser = jira.get_user_profile_by_identifier(user_identifier)
        result = user.to_simplified_dict()
        response_data = {"success": True, "user": result}
    except Exception as e:
        error_message = ""
        log_level = logging.ERROR
        if isinstance(e, ValueError) and "not found" in str(e).lower():
            log_level = logging.WARNING
            error_message = str(e)
        elif isinstance(e, MCPAtlassianAuthenticationError):
            error_message = f"Authentication/Permission Error: {str(e)}"
        elif isinstance(e, OSError | HTTPError):
            error_message = f"Network or API Error: {str(e)}"
        else:
            error_message = (
                "An unexpected error occurred while fetching the user profile."
            )
            logger.exception(
                f"Unexpected error in get_user_profile for '{user_identifier}':"
            )
        error_result = {
            "success": False,
            "error": str(e),
            "user_identifier": user_identifier,
        }
        logger.log(
            log_level,
            f"get_user_profile failed for '{user_identifier}': {error_message}",
        )
        response_data = error_result
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_users"},
    annotations={"title": "Get Current User Profile", "readOnlyHint": True},
)
async def get_current_user_profile(
    ctx: Context,
    include_full_profile: Annotated[
        bool,
        Field(
            description=(
                "When True, return the full unfiltered ``/myself`` payload. "
                "When False, return only the most useful fields (account ID, "
                "name, displayName, email). Default True."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """Return the authenticated user's profile (``/myself``).

    Useful as the entry point for "what should I review?" / "issues
    assigned to me" workflows — gives the LLM the user's account ID, key,
    name, and display name in one call.

    Returns:
        JSON string with the current user's profile.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        profile = jira.get_current_user_profile()
        if not include_full_profile and isinstance(profile, dict):
            profile = {
                k: profile.get(k)
                for k in ("accountId", "key", "name", "displayName", "emailAddress")
                if profile.get(k) is not None
            }
        return json.dumps({"success": True, "user": profile}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error fetching current user profile: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_users"},
    annotations={"title": "Search Jira Users", "readOnlyHint": True},
)
async def search_users(
    ctx: Context,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Substring matched against name/displayName/email. "
                "Cloud uses the 'query' parameter; Server/DC uses 'username'."
            ),
            default=None,
        ),
    ] = None,
    max_results: Annotated[
        int,
        Field(description="Maximum number of users to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """Search for Jira users.

    Returns:
        JSON string with simplified user records.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        users = jira.search_users(query=query, max_results=max_results)
        simplified = [u.to_simplified_dict() for u in users]
        return json.dumps(
            {"success": True, "count": len(simplified), "users": simplified},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching users: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_users"},
    annotations={"title": "Search Assignable Users", "readOnlyHint": True},
)
async def search_assignable_users(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(description="Project key (e.g., 'PROJ') the user must be assignable in"),
    ],
    query: Annotated[
        str | None,
        Field(description="Substring filter (Cloud) or username (Server/DC)", default=None),
    ] = None,
    max_results: Annotated[
        int,
        Field(description="Maximum number of users to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """Find users that can be assigned issues in the given project.

    Use this before ``update_issue`` (assignee field) to discover valid
    assignees that match a name fragment.

    Returns:
        JSON string with simplified user records.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        users = jira.search_assignable_users(
            project_key=project_key, query=query, max_results=max_results
        )
        simplified = [u.to_simplified_dict() for u in users]
        return json.dumps(
            {
                "success": True,
                "project_key": project_key,
                "count": len(simplified),
                "users": simplified,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching assignable users: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_links"},
    annotations={"title": "Get Remote Issue Links", "readOnlyHint": True},
)
async def get_remote_issue_links(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """List remote (web/external) links attached to an issue.

    Returns:
        JSON string with remote link objects.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        links = jira.get_remote_issue_links(issue_key)
        return json.dumps(
            {
                "success": True,
                "issue_key": issue_key,
                "count": len(links),
                "remote_links": links,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error getting remote links: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_watchers"},
    annotations={"title": "Get Issue Watchers", "readOnlyHint": True},
)
async def get_issue_watchers(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get the list of watchers for a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        JSON string with watcher count and list of watchers.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.get_issue_watchers(issue_key)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_watchers"},
    annotations={
        "title": "Add Issue Watcher",
        "readOnlyHint": False,
    },
)
@check_write_access
async def add_watcher(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    user_identifier: Annotated[
        str,
        Field(
            description=(
                "User to add as watcher. For Jira Cloud, use the"
                " account ID. For Jira Server/DC, use the username."
            ),
        ),
    ],
) -> str:
    """Add a user as a watcher to a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        user_identifier: Account ID (Cloud) or username (Server/DC).

    Returns:
        JSON string with success confirmation.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.add_watcher(issue_key, user_identifier)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_watchers"},
    annotations={
        "title": "Remove Issue Watcher",
        "readOnlyHint": False,
    },
)
@check_write_access
async def remove_watcher(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    username: Annotated[
        str | None,
        Field(
            description=("Username to remove (for Jira Server/DC)."),
            default=None,
        ),
    ] = None,
    account_id: Annotated[
        str | None,
        Field(
            description=("Account ID to remove (for Jira Cloud)."),
            default=None,
        ),
    ] = None,
) -> str:
    """Remove a user from watching a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        username: Username to remove (Server/DC).
        account_id: Account ID to remove (Cloud).

    Returns:
        JSON string with success confirmation.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.remove_watcher(issue_key, username=username, account_id=account_id)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_issues"},
    annotations={"title": "Get Issue", "readOnlyHint": True},
)
async def get_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "(Optional) Comma-separated list of fields to return (e.g., 'summary,status,customfield_10010'). "
                "You may also provide a single field as a string (e.g., 'duedate'). "
                "Use '*all' for all fields (including custom fields), or omit for essential fields only."
            ),
            default=",".join(DEFAULT_READ_JIRA_FIELDS),
        ),
    ] = ",".join(DEFAULT_READ_JIRA_FIELDS),
    expand: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Fields to expand. Examples: 'renderedFields' (for rendered content), "
                "'transitions' (for available status transitions), 'changelog' (for history)"
            ),
            default=None,
        ),
    ] = None,
    comment_limit: Annotated[
        int,
        Field(
            description="Maximum number of comments to include (0 or null for no comments)",
            default=10,
            ge=0,
            le=100,
        ),
    ] = 10,
    properties: Annotated[
        str | None,
        Field(
            description="(Optional) A comma-separated list of issue properties to return",
            default=None,
        ),
    ] = None,
    update_history: Annotated[
        bool,
        Field(
            description="Whether to update the issue view history for the requesting user",
            default=True,
        ),
    ] = True,
) -> str:
    """Get details of a specific Jira issue including its Epic links and relationship information.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        fields: Comma-separated list of fields to return (e.g., 'summary,status,customfield_10010'), a single field as a string (e.g., 'duedate'), '*all' for all fields, or omitted for essentials.
        expand: Optional fields to expand.
        comment_limit: Maximum number of comments.
        properties: Issue properties to return.
        update_history: Whether to update issue view history.

    Returns:
        JSON string representing the Jira issue object.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    jira = await get_jira_fetcher(ctx)
    fields_list: str | list[str] | None = fields
    if fields and fields != "*all":
        fields_list = [f.strip() for f in fields.split(",")]

    issue = jira.get_issue(
        issue_key=issue_key,
        fields=fields_list,
        expand=expand,
        comment_limit=comment_limit,
        properties=properties.split(",") if properties else None,
        update_history=update_history,
    )
    result = issue.to_simplified_dict()
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_issues"},
    annotations={"title": "Search Issues", "readOnlyHint": True},
)
async def search(
    ctx: Context,
    jql: Annotated[
        str,
        Field(
            description=(
                "JQL query string (Jira Query Language). Examples:\n"
                '- Find Epics: "issuetype = Epic AND project = PROJ"\n'
                '- Find issues in Epic: "parent = PROJ-123"\n'
                "- Find by status: \"status = 'In Progress' AND project = PROJ\"\n"
                '- Find by assignee: "assignee = currentUser()"\n'
                '- Find recently updated: "updated >= -7d AND project = PROJ"\n'
                '- Find by label: "labels = frontend AND project = PROJ"\n'
                '- Find by priority: "priority = High AND project = PROJ"'
            )
        ),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "(Optional) Comma-separated fields to return in the results. "
                "Use '*all' for all fields, or specify individual fields like 'summary,status,assignee,priority'"
            ),
            default=",".join(DEFAULT_READ_JIRA_FIELDS),
        ),
    ] = ",".join(DEFAULT_READ_JIRA_FIELDS),
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1),
    ] = 10,
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    projects_filter: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comma-separated list of project keys to filter results by. "
                "Overrides the environment variable JIRA_PROJECTS_FILTER if provided."
            ),
            default=None,
        ),
    ] = None,
    expand: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) fields to expand. Examples: 'renderedFields', 'transitions', 'changelog'"
            ),
            default=None,
        ),
    ] = None,
    page_token: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Pagination token from a previous search result. "
                "Cloud only — Server/DC uses start_at for pagination."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Search Jira issues using JQL (Jira Query Language).

    Args:
        ctx: The FastMCP context.
        jql: JQL query string.
        fields: Comma-separated fields to return.
        limit: Maximum number of results.
        start_at: Starting index for pagination.
        projects_filter: Comma-separated list of project keys to filter by.
        expand: Optional fields to expand.
        page_token: Pagination token from a previous search result (Cloud only).

    Returns:
        JSON string representing the search results including pagination info.
    """
    jira = await get_jira_fetcher(ctx)
    fields_list: str | list[str] | None = fields
    if fields and fields != "*all":
        fields_list = [f.strip() for f in fields.split(",")]

    search_result = jira.search_issues(
        jql=jql,
        fields=fields_list,
        limit=limit,
        start=start_at,
        expand=expand,
        projects_filter=projects_filter,
        page_token=page_token,
    )
    result = search_result.to_simplified_dict()
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_fields"},
    annotations={"title": "Search Fields", "readOnlyHint": True},
)
async def search_fields(
    ctx: Context,
    keyword: Annotated[
        str,
        Field(
            description="Keyword for fuzzy search. If left empty, lists the first 'limit' available fields in their default order.",
            default="",
        ),
    ] = "",
    limit: Annotated[
        int, Field(description="Maximum number of results", default=10, ge=1)
    ] = 10,
    refresh: Annotated[
        bool,
        Field(description="Whether to force refresh the field list", default=False),
    ] = False,
) -> str:
    """Search Jira fields by keyword with fuzzy match.

    Args:
        ctx: The FastMCP context.
        keyword: Keyword for fuzzy search.
        limit: Maximum number of results.
        refresh: Whether to force refresh the field list.

    Returns:
        JSON string representing a list of matching field definitions.
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.search_fields(keyword, limit=limit, refresh=refresh)
    return json.dumps(result, indent=2, ensure_ascii=False)


def _matches_contains(option: dict[str, Any], needle: str) -> bool:
    """Check if option value contains needle (case-insensitive).

    Checks both the parent option value and any child option values
    (for cascading select fields).

    Args:
        option: Simplified option dict with 'value' and optional
            'child_options' keys.
        needle: Substring to search for (case-insensitive).

    Returns:
        True if the needle is found in the option or its children.
    """
    lower_needle = needle.lower()
    value = option.get("value", "")
    if isinstance(value, str) and lower_needle in value.lower():
        return True
    # Check children for cascading selects
    for child in option.get("child_options", []):
        child_value = child.get("value", "")
        if isinstance(child_value, str) and lower_needle in child_value.lower():
            return True
    return False


def _apply_option_filters(
    options: list[dict[str, Any]],
    contains: str | None,
    return_limit: int | None,
) -> list[dict[str, Any]]:
    """Apply contains filter and limit to option list.

    Args:
        options: List of simplified option dicts.
        contains: Case-insensitive substring filter (or None to skip).
        return_limit: Maximum number of results (or None for no limit).

    Returns:
        Filtered and/or limited list of option dicts.
    """
    result = options
    if contains:
        result = [opt for opt in result if _matches_contains(opt, contains)]
    if return_limit is not None:
        result = result[:return_limit]
    return result


def _to_values_only_payload(options: list[dict[str, Any]]) -> list[Any]:
    """Extract values only from options, preserving cascading structure.

    For simple options: returns ``["value1", "value2"]``
    For cascading: returns
    ``[{"value": "parent", "children": ["child1", "child2"]}]``

    Args:
        options: List of simplified option dicts.

    Returns:
        Compact list of values or value/children structures.
    """
    result: list[Any] = []
    for opt in options:
        value = opt.get("value", "")
        children = opt.get("child_options", [])
        if children:
            result.append(
                {
                    "value": value,
                    "children": [c.get("value", "") for c in children],
                }
            )
        else:
            result.append(value)
    return result


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_fields"},
    annotations={"title": "Get Field Options", "readOnlyHint": True},
)
async def get_field_options(
    ctx: Context,
    field_id: Annotated[
        str,
        Field(
            description="Custom field ID (e.g., 'customfield_10001'). "
            "Use jira_search_fields to find field IDs."
        ),
    ],
    context_id: Annotated[
        str | None,
        Field(
            description="Field context ID (Cloud only). "
            "If omitted, auto-resolves to the global context.",
            default=None,
        ),
    ] = None,
    project_key: Annotated[
        str | None,
        Field(
            description="Project key (required for Server/DC). Example: 'PROJ'",
            default=None,
        ),
    ] = None,
    issue_type: Annotated[
        str | None,
        Field(
            description="Issue type name (required for Server/DC). Example: 'Bug'",
            default=None,
        ),
    ] = None,
    contains: Annotated[
        str | None,
        Field(
            description="Case-insensitive substring filter on option "
            "values. Also matches child values in cascading selects.",
            default=None,
        ),
    ] = None,
    return_limit: Annotated[
        int | None,
        Field(
            description="Maximum number of results to return "
            "(applied after filtering).",
            default=None,
            ge=1,
        ),
    ] = None,
    values_only: Annotated[
        bool,
        Field(
            description="If true, return only value strings in a "
            "compact JSON format instead of full option objects.",
            default=False,
        ),
    ] = False,
) -> str:
    """Get allowed option values for a custom field.

    Returns the list of valid options for select, multi-select, radio,
    checkbox, and cascading select custom fields.

    Cloud: Uses the Field Context Option API. If context_id is not provided,
    automatically resolves to the global context.

    Server/DC: Uses createmeta to get allowedValues. Requires project_key
    and issue_type parameters.

    Args:
        ctx: The FastMCP context.
        field_id: The custom field ID.
        context_id: Field context ID (Cloud only, auto-resolved if omitted).
        project_key: Project key (required for Server/DC).
        issue_type: Issue type name (required for Server/DC).
        contains: Case-insensitive substring filter on option values.
        return_limit: Cap on number of results after filtering.
        values_only: Return compact format with only value strings.

    Returns:
        JSON string with the list of available options.
    """
    jira = await get_jira_fetcher(ctx)
    options = jira.get_field_options(
        field_id=field_id,
        context_id=context_id,
        project_key=project_key,
        issue_type=issue_type,
    )
    result = [opt.to_simplified_dict() for opt in options]
    result = _apply_option_filters(result, contains, return_limit)
    if values_only:
        return json.dumps(
            _to_values_only_payload(result),
            indent=2,
            ensure_ascii=False,
        )
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_issues"},
    annotations={"title": "Get Project Issues", "readOnlyHint": True},
)
async def get_project_issues(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1, le=50),
    ] = 10,
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
) -> str:
    """Get all issues for a specific Jira project.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        limit: Maximum number of results.
        start_at: Starting index for pagination.

    Returns:
        JSON string representing the search results including pagination info.
    """
    jira = await get_jira_fetcher(ctx)
    search_result = jira.get_project_issues(
        project_key=project_key, start=start_at, limit=limit
    )
    result = search_result.to_simplified_dict()
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_transitions"},
    annotations={"title": "Get Transitions", "readOnlyHint": True},
)
async def get_transitions(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get available status transitions for a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        JSON string representing a list of available transitions.
    """
    jira = await get_jira_fetcher(ctx)
    # Underlying method returns list[dict] in the desired format
    transitions = jira.get_available_transitions(issue_key)
    return json.dumps(transitions, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_worklog"},
    annotations={"title": "Get Worklog", "readOnlyHint": True},
)
async def get_worklog(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get worklog entries for a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        JSON string representing the worklog entries.
    """
    jira = await get_jira_fetcher(ctx)
    worklogs = jira.get_worklogs(issue_key)
    result = {"worklogs": worklogs}
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_attachments"},
    annotations={"title": "Download Attachments", "readOnlyHint": True},
)
async def download_attachments(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> list[TextContent | EmbeddedResource]:
    """Download attachments from a Jira issue.

    Returns attachment contents as base64-encoded embedded resources so that
    they are available over the MCP protocol without requiring filesystem
    access on the server.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        A list containing a text summary and one EmbeddedResource per
        successfully downloaded attachment.
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.get_issue_attachment_contents(issue_key=issue_key)

    contents: list[TextContent | EmbeddedResource] = []

    if not result.get("success"):
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(result, indent=2, ensure_ascii=False),
            )
        )
        return contents

    attachments = result.get("attachments", [])
    failed = result.get("failed", [])
    downloaded = 0

    for attachment in attachments:
        data_bytes: bytes = attachment["data"]
        filename = attachment["filename"]

        if len(data_bytes) > ATTACHMENT_MAX_BYTES:
            failed.append(
                {
                    "filename": filename,
                    "error": (
                        f"Attachment '{filename}' is {len(data_bytes)} bytes"
                        " which exceeds the 50 MB inline limit."
                        " Retrieve it directly from Jira."
                    ),
                }
            )
            continue

        encoded = base64.b64encode(data_bytes).decode("ascii")
        mime_type = attachment.get("content_type", "application/octet-stream")
        downloaded += 1

        contents.append(
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"attachment:///{issue_key}/{filename}",
                    mimeType=mime_type,
                    blob=encoded,
                ),
            )
        )

    summary: dict[str, Any] = {
        "success": True,
        "issue_key": result.get("issue_key", issue_key),
        "total": result.get("total", 0),
        "downloaded": downloaded,
        "failed": failed,
    }

    if not attachments and not failed:
        summary["message"] = result.get(
            "message", f"No attachments found for issue {issue_key}"
        )

    # Insert summary text at the beginning
    contents.insert(
        0,
        TextContent(
            type="text",
            text=json.dumps(summary, indent=2, ensure_ascii=False),
        ),
    )

    return contents


@jira_mcp.tool(
    tags={"jira", "read", "attachments", "toolset:jira_attachments"},
    annotations={"title": "Get Issue Images", "readOnlyHint": True},
)
async def get_issue_images(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description=(
                "Jira issue key (e.g., 'PROJ-123'). Returns image "
                "attachments as inline ImageContent for LLM vision."
            ),
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> list[TextContent | ImageContent]:
    """Get all images attached to a Jira issue as inline image content.

    Filters attachments to images only (PNG, JPEG, GIF, WebP, SVG, BMP)
    and returns them as base64-encoded ImageContent that clients can
    render directly. Non-image attachments are excluded.

    Files with ambiguous MIME types (application/octet-stream) are
    detected by filename extension as a fallback. Images larger than
    50 MB are skipped with an error entry in the summary.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        A list with a text summary followed by one ImageContent per
        successfully downloaded image.
    """
    jira = await get_jira_fetcher(ctx)
    contents: list[TextContent | ImageContent] = []

    attachments = jira.get_issue_attachments(issue_key)

    # Filter to image attachments
    image_attachments: list[tuple[JiraAttachment, str]] = []
    for att in attachments:
        is_img, resolved_mime = is_image_attachment(att.content_type, att.filename)
        if is_img:
            image_attachments.append((att, resolved_mime))

    if not image_attachments:
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "issue_key": issue_key,
                        "total_images": 0,
                        "downloaded": 0,
                        "failed": [],
                        "message": "No image attachments found",
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        )
        return contents

    fetched: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    for att, resolved_mime in image_attachments:
        filename = att.filename or "unknown"

        if att.size > ATTACHMENT_MAX_BYTES:
            failed.append(
                {
                    "filename": filename,
                    "error": (
                        f"Image is {att.size} bytes "
                        "which exceeds the 50 MB inline limit."
                    ),
                }
            )
            continue

        if not att.url:
            failed.append({"filename": filename, "error": "No download URL"})
            continue

        encoded, _, fetched_bytes = fetch_and_encode_attachment(
            fetch_fn=jira.fetch_attachment_content,
            url=att.url,
            filename=filename,
            mime_type=resolved_mime,
        )
        if encoded is None:
            if fetched_bytes > 0:
                error_msg = (
                    f"Downloaded size {fetched_bytes} bytes "
                    "exceeds the 50 MB inline limit."
                )
            else:
                error_msg = "Fetch failed"
            failed.append({"filename": filename, "error": error_msg})
            continue

        fetched.append({"filename": filename, "size": fetched_bytes})
        contents.append(
            ImageContent(
                type="image",
                data=encoded,
                mimeType=resolved_mime,
            )
        )

    summary: dict[str, object] = {
        "success": True,
        "issue_key": issue_key,
        "total_images": len(image_attachments),
        "downloaded": len(fetched),
        "failed": failed,
    }
    contents.insert(
        0,
        TextContent(
            type="text",
            text=json.dumps(summary, indent=2, ensure_ascii=False),
        ),
    )
    return contents


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_agile"},
    annotations={"title": "Get Agile Boards", "readOnlyHint": True},
)
async def get_agile_boards(
    ctx: Context,
    board_name: Annotated[
        str | None,
        Field(description="(Optional) The name of board, support fuzzy search"),
    ] = None,
    project_key: Annotated[
        str | None,
        Field(
            description="(Optional) Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ] = None,
    board_type: Annotated[
        str | None,
        Field(
            description="(Optional) The type of jira board (e.g., 'scrum', 'kanban')"
        ),
    ] = None,
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1, le=50),
    ] = 10,
) -> str:
    """Get jira agile boards by name, project key, or type.

    Args:
        ctx: The FastMCP context.
        board_name: Name of the board (fuzzy search).
        project_key: Project key.
        board_type: Board type ('scrum' or 'kanban').
        start_at: Starting index.
        limit: Maximum results.

    Returns:
        JSON string representing a list of board objects.
    """
    jira = await get_jira_fetcher(ctx)
    boards = jira.get_all_agile_boards_model(
        board_name=board_name,
        project_key=project_key,
        board_type=board_type,
        start=start_at,
        limit=limit,
    )
    result = [board.to_simplified_dict() for board in boards]
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_agile"},
    annotations={"title": "Get Board Issues", "readOnlyHint": True},
)
async def get_board_issues(
    ctx: Context,
    board_id: Annotated[str, Field(description="The id of the board (e.g., '1001')")],
    jql: Annotated[
        str,
        Field(
            description=(
                "JQL query string (Jira Query Language). Examples:\n"
                '- Find Epics: "issuetype = Epic AND project = PROJ"\n'
                '- Find issues in Epic: "parent = PROJ-123"\n'
                "- Find by status: \"status = 'In Progress' AND project = PROJ\"\n"
                '- Find by assignee: "assignee = currentUser()"\n'
                '- Find recently updated: "updated >= -7d AND project = PROJ"\n'
                '- Find by label: "labels = frontend AND project = PROJ"\n'
                '- Find by priority: "priority = High AND project = PROJ"'
            )
        ),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "Comma-separated fields to return in the results. "
                "Use '*all' for all fields, or specify individual "
                "fields like 'summary,status,assignee,priority'"
            ),
            default=",".join(DEFAULT_READ_JIRA_FIELDS),
        ),
    ] = ",".join(DEFAULT_READ_JIRA_FIELDS),
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1, le=50),
    ] = 10,
    expand: Annotated[
        str,
        Field(
            description="Optional fields to expand in the response (e.g., 'changelog').",
            default="version",
        ),
    ] = "version",
) -> str:
    """Get all issues linked to a specific board filtered by JQL.

    Args:
        ctx: The FastMCP context.
        board_id: The ID of the board.
        jql: JQL query string to filter issues.
        fields: Comma-separated fields to return.
        start_at: Starting index for pagination.
        limit: Maximum number of results.
        expand: Optional fields to expand.

    Returns:
        JSON string representing the search results including pagination info.
    """
    jira = await get_jira_fetcher(ctx)
    fields_list: str | list[str] | None = fields
    if fields and fields != "*all":
        fields_list = [f.strip() for f in fields.split(",")]

    search_result = jira.get_board_issues(
        board_id=board_id,
        jql=jql,
        fields=fields_list,
        start=start_at,
        limit=limit,
        expand=expand,
    )
    result = search_result.to_simplified_dict()
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_agile"},
    annotations={"title": "Get Sprints from Board", "readOnlyHint": True},
)
async def get_sprints_from_board(
    ctx: Context,
    board_id: Annotated[str, Field(description="The id of board (e.g., '1000')")],
    state: Annotated[
        str | None,
        Field(description="Sprint state (e.g., 'active', 'future', 'closed')"),
    ] = None,
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1, le=50),
    ] = 10,
) -> str:
    """Get jira sprints from board by state.

    Args:
        ctx: The FastMCP context.
        board_id: The ID of the board.
        state: Sprint state ('active', 'future', 'closed'). If None, returns all sprints.
        start_at: Starting index.
        limit: Maximum results.

    Returns:
        JSON string representing a list of sprint objects.
    """
    jira = await get_jira_fetcher(ctx)
    sprints = jira.get_all_sprints_from_board_model(
        board_id=board_id, state=state, start=start_at, limit=limit
    )
    result = [sprint.to_simplified_dict() for sprint in sprints]
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_agile"},
    annotations={"title": "Get Sprint Issues", "readOnlyHint": True},
)
async def get_sprint_issues(
    ctx: Context,
    sprint_id: Annotated[str, Field(description="The id of sprint (e.g., '10001')")],
    fields: Annotated[
        str,
        Field(
            description=(
                "Comma-separated fields to return in the results. "
                "Use '*all' for all fields, or specify individual "
                "fields like 'summary,status,assignee,priority'"
            ),
            default=",".join(DEFAULT_READ_JIRA_FIELDS),
        ),
    ] = ",".join(DEFAULT_READ_JIRA_FIELDS),
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=10, ge=1, le=50),
    ] = 10,
) -> str:
    """Get jira issues from sprint.

    Args:
        ctx: The FastMCP context.
        sprint_id: The ID of the sprint.
        fields: Comma-separated fields to return.
        start_at: Starting index.
        limit: Maximum results.

    Returns:
        JSON string representing the search results including pagination info.
    """
    jira = await get_jira_fetcher(ctx)
    fields_list: str | list[str] | None = fields
    if fields and fields != "*all":
        fields_list = [f.strip() for f in fields.split(",")]

    search_result = jira.get_sprint_issues(
        sprint_id=sprint_id, fields=fields_list, start=start_at, limit=limit
    )
    result = search_result.to_simplified_dict()
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_links"},
    annotations={"title": "Get Link Types", "readOnlyHint": True},
)
async def get_link_types(
    ctx: Context,
    name_filter: Annotated[
        str | None,
        Field(
            description="(Optional) Filter link types by name substring (case-insensitive)",
        ),
    ] = None,
) -> str:
    """Get all available issue link types.

    Args:
        ctx: The FastMCP context.
        name_filter: Optional substring to filter link types by name.

    Returns:
        JSON string representing a list of issue link type objects.
    """
    jira = await get_jira_fetcher(ctx)
    link_types = jira.get_issue_link_types()
    formatted_link_types = [link_type.to_simplified_dict() for link_type in link_types]
    if name_filter:
        name_lower = name_filter.lower()
        formatted_link_types = [
            lt
            for lt in formatted_link_types
            if name_lower in lt.get("name", "").lower()
        ]
    return json.dumps(formatted_link_types, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_issues"},
    annotations={"title": "Create Issue", "destructiveHint": True},
)
@check_write_access
async def create_issue(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description=(
                "The JIRA project key (e.g. 'PROJ', 'DEV', 'ACV2'). "
                "This is the prefix of issue keys in your project. "
                "Never assume what it might be, always ask the user."
            ),
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    summary: Annotated[str, Field(description="Summary/title of the issue")],
    issue_type: Annotated[
        str,
        Field(
            description=(
                "Issue type (e.g. 'Task', 'Bug', 'Story', 'Epic', 'Subtask'). "
                "The available types depend on your project configuration. "
                "For subtasks, use 'Subtask' (not 'Sub-task') and include parent in additional_fields."
            ),
        ),
    ],
    assignee: Annotated[
        str | None,
        Field(
            description="(Optional) Assignee's user identifier (string): Email, display name, or account ID (e.g., 'user@example.com', 'John Doe', 'accountid:...')",
            default=None,
        ),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Issue description in Markdown format", default=None),
    ] = None,
    components: Annotated[
        str | None,
        Field(
            description="(Optional) Comma-separated list of component names to assign (e.g., 'Frontend,API')",
            default=None,
        ),
    ] = None,
    additional_fields: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) JSON string of additional fields to set. Examples:\n"
                '- Set priority: {"priority": {"name": "High"}}\n'
                '- Add labels: {"labels": ["frontend", "urgent"]}\n'
                '- Link to parent (for any issue type): {"parent": "PROJ-123"}\n'
                '- Link to epic: {"epicKey": "EPIC-123"} or {"epic_link": "EPIC-123"}\n'
                '- Set Fix Version/s: {"fixVersions": [{"id": "10020"}]}\n'
                '- Custom fields: {"customfield_10010": "value"}'
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create a new Jira issue with optional Epic link or parent for subtasks.

    Args:
        ctx: The FastMCP context.
        project_key: The JIRA project key.
        summary: Summary/title of the issue.
        issue_type: Issue type (e.g., 'Task', 'Bug', 'Story', 'Epic', 'Subtask').
        assignee: Assignee's user identifier (string): Email, display name, or account ID (e.g., 'user@example.com', 'John Doe', 'accountid:...').
        description: Issue description in Markdown format.
        components: Comma-separated list of component names.
        additional_fields: JSON string of additional fields.

    Returns:
        JSON string representing the created issue object.

    Raises:
        ValueError: If in read-only mode or Jira client is unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    # Parse components from comma-separated string to list
    components_list = None
    if components and isinstance(components, str):
        components_list = [
            comp.strip() for comp in components.split(",") if comp.strip()
        ]

    extra_fields = _parse_additional_fields(additional_fields)

    issue = jira.create_issue(
        project_key=project_key,
        summary=summary,
        issue_type=issue_type,
        description=description,
        assignee=assignee,
        components=components_list,
        **extra_fields,
    )
    result = issue.to_simplified_dict()
    return json.dumps(
        {"message": "Issue created successfully", "issue": result},
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_issues"},
    annotations={"title": "Batch Create Issues", "destructiveHint": True},
)
@check_write_access
async def batch_create_issues(
    ctx: Context,
    issues: Annotated[
        str,
        Field(
            description=(
                "JSON array of issue objects. Each object should contain:\n"
                "- project_key (required): The project key (e.g., 'PROJ')\n"
                "- summary (required): Issue summary/title\n"
                "- issue_type (required): Type of issue (e.g., 'Task', 'Bug')\n"
                "- description (optional): Issue description in Markdown format\n"
                "- assignee (optional): Assignee username or email\n"
                "- components (optional): Array of component names\n"
                "Example: [\n"
                '  {"project_key": "PROJ", "summary": "Issue 1", "issue_type": "Task"},\n'
                '  {"project_key": "PROJ", "summary": "Issue 2", "issue_type": "Bug", "components": ["Frontend"]}\n'
                "]"
            )
        ),
    ],
    validate_only: Annotated[
        bool,
        Field(
            description="If true, only validates the issues without creating them",
            default=False,
        ),
    ] = False,
) -> str:
    """Create multiple Jira issues in a batch.

    Args:
        ctx: The FastMCP context.
        issues: JSON array string of issue objects.
        validate_only: If true, only validates without creating.

    Returns:
        JSON string indicating success and listing created issues (or validation result).

    Raises:
        ValueError: If in read-only mode, Jira client unavailable, or invalid JSON.
    """
    jira = await get_jira_fetcher(ctx)
    # Parse issues from JSON string
    try:
        issues_list = json.loads(issues)
        if not isinstance(issues_list, list):
            raise ValueError("Input 'issues' must be a JSON array string.")
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON in issues")
    except Exception as e:
        raise ValueError(f"Invalid input for issues: {e}") from e

    # Create issues in batch
    created_issues = jira.batch_create_issues(issues_list, validate_only=validate_only)

    message = (
        "Issues validated successfully"
        if validate_only
        else "Issues created successfully"
    )
    result = {
        "message": message,
        "issues": [issue.to_simplified_dict() for issue in created_issues],
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_issues"},
    annotations={"title": "Batch Get Changelogs", "readOnlyHint": True},
)
async def batch_get_changelogs(
    ctx: Context,
    issue_ids_or_keys: Annotated[
        str,
        Field(
            description="Comma-separated list of Jira issue IDs or keys (e.g. 'PROJ-123,PROJ-124')"
        ),
    ],
    fields: Annotated[
        str | None,
        Field(
            description="(Optional) Comma-separated list of fields to filter changelogs by (e.g. 'status,assignee'). Default to None for all fields.",
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of changelogs to return in result for each issue. "
                "Default to -1 for all changelogs. "
                "Notice that it only limits the results in the response, "
                "the function will still fetch all the data."
            ),
            default=-1,
        ),
    ] = -1,
) -> str:
    """Get changelogs for multiple Jira issues (Cloud only).

    Args:
        ctx: The FastMCP context.
        issue_ids_or_keys: List of issue IDs or keys.
        fields: List of fields to filter changelogs by. None for all fields.
        limit: Maximum changelogs per issue (-1 for all).

    Returns:
        JSON string representing a list of issues with their changelogs.

    Raises:
        NotImplementedError: If run on Jira Server/Data Center.
        ValueError: If Jira client is unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    # Ensure this runs only on Cloud, as per original function docstring
    if not jira.config.is_cloud:
        raise NotImplementedError(
            "Batch get issue changelogs is only available on Jira Cloud."
        )

    # Parse CSV strings into lists
    keys_list = [k.strip() for k in issue_ids_or_keys.split(",") if k.strip()]
    fields_list: list[str] | None = None
    if fields is not None:
        fields_list = [f.strip() for f in fields.split(",") if f.strip()]

    # Call the underlying method
    issues_with_changelogs = jira.batch_get_changelogs(
        issue_ids_or_keys=keys_list, fields=fields_list
    )

    # Format the response
    results = []
    limit_val = None if limit == -1 else limit
    for issue in issues_with_changelogs:
        results.append(
            {
                "issue_id": issue.id,
                "changelogs": [
                    changelog.to_simplified_dict()
                    for changelog in issue.changelogs[:limit_val]
                ],
            }
        )
    return json.dumps(results, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_issues"},
    annotations={"title": "Update Issue", "destructiveHint": True},
)
@check_write_access
async def update_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    fields: Annotated[
        str,
        Field(
            description=(
                "JSON string of fields to update. For 'assignee', provide a string identifier (email, name, or accountId). "
                "For 'description', provide text in Markdown format. "
                'Example: \'{"assignee": "user@example.com", "summary": "New Summary", "description": "## Updated\\nMarkdown text"}\''
            )
        ),
    ],
    additional_fields: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) JSON string of additional fields to update. "
                "Use this for custom fields or more complex updates. "
                'Link to epic: {"epicKey": "EPIC-123"} or {"epic_link": "EPIC-123"}.'
            ),
            default=None,
        ),
    ] = None,
    components: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comma-separated list of component names "
                "(e.g., 'Frontend,API')"
            ),
            default=None,
        ),
    ] = None,
    attachments: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) JSON string array or comma-separated list of file paths to attach to the issue. "
                "Example: '/path/to/file1.txt,/path/to/file2.txt' or ['/path/to/file1.txt','/path/to/file2.txt']"
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Update an existing Jira issue including changing status, adding Epic links, updating fields, etc.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        fields: JSON string of fields to update. Text fields like 'description' should use Markdown format.
        additional_fields: Optional JSON string of additional fields.
        components: Comma-separated list of component names.
        attachments: Optional JSON array string or comma-separated list of file paths.

    Returns:
        JSON string representing the updated issue object and attachment results.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable, or invalid input.
    """
    jira = await get_jira_fetcher(ctx)
    update_fields = _parse_additional_fields(fields)

    # Parse components from comma-separated string to list
    components_list = None
    if components and isinstance(components, str):
        components_list = [
            comp.strip() for comp in components.split(",") if comp.strip()
        ]

    extra_fields = _parse_additional_fields(additional_fields)

    # Parse attachments
    attachment_paths = []
    if attachments:
        if isinstance(attachments, str):
            try:
                parsed = json.loads(attachments)
                if isinstance(parsed, list):
                    attachment_paths = [str(p) for p in parsed]
                else:
                    raise ValueError("attachments JSON string must be an array.")
            except json.JSONDecodeError:
                # Assume comma-separated if not valid JSON array
                attachment_paths = [
                    p.strip() for p in attachments.split(",") if p.strip()
                ]
        else:
            raise ValueError(
                "attachments must be a JSON array string or comma-separated string."
            )

    # Combine fields and additional_fields
    all_updates = {**update_fields, **extra_fields}
    if components_list:
        all_updates["components"] = components_list
    if attachment_paths:
        all_updates["attachments"] = attachment_paths

    try:
        issue = jira.update_issue(issue_key=issue_key, **all_updates)
        result = issue.to_simplified_dict()
        if (
            hasattr(issue, "custom_fields")
            and "attachment_results" in issue.custom_fields
        ):
            result["attachment_results"] = issue.custom_fields["attachment_results"]
        return json.dumps(
            {"message": "Issue updated successfully", "issue": result},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error updating issue {issue_key}: {str(e)}", exc_info=True)
        raise ValueError(f"Failed to update issue {issue_key}: {str(e)}")


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_issues"},
    annotations={"title": "Delete Issue", "destructiveHint": True},
)
@check_write_access
async def delete_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Delete an existing Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.

    Returns:
        JSON string indicating success.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    deleted = jira.delete_issue(issue_key)
    result = {"message": f"Issue {issue_key} has been deleted successfully."}
    # The underlying method raises on failure, so if we reach here, it's success.
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_comments"},
    annotations={"title": "Add Comment", "destructiveHint": True},
)
@check_write_access
async def add_comment(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    body: Annotated[str, Field(description="Comment text in Markdown format")],
    visibility: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comment visibility as JSON string "
                '(e.g. \'{"type":"group",'
                '"value":"jira-users"}\')'
            )
        ),
    ] = None,
    public: Annotated[
        bool | None,
        Field(
            description=(
                "(Optional) For JSM/Service Desk issues only. "
                "Set to true for customer-visible comment, "
                "false for internal agent-only comment. "
                "Uses the ServiceDesk API (plain text, not "
                "Markdown). Cannot be combined with visibility."
            )
        ),
    ] = None,
) -> str:
    """Add a comment to a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        body: Comment text in Markdown.
        visibility: (Optional) Comment visibility as JSON string.
        public: (Optional) For JSM issues. True = customer-visible,
            False = internal/agent-only. Uses ServiceDesk API.

    Returns:
        JSON string representing the added comment object.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    # Req 27.1/27.2/27.4: validate visibility BEFORE acquiring the fetcher
    # so malformed input issues zero POST against /rest/api/2/issue/.../comment.
    visibility_dict = _parse_visibility(visibility)
    if isinstance(visibility_dict, dc_guards.StructuredError):
        return json.dumps({"success": False, **visibility_dict.to_dict()})

    jira = await get_jira_fetcher(ctx)
    result = jira.add_comment(issue_key, body, visibility_dict, public=public)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_comments"},
    annotations={"title": "Edit Comment", "destructiveHint": True},
)
@check_write_access
async def edit_comment(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    comment_id: Annotated[str, Field(description="The ID of the comment to edit")],
    body: Annotated[str, Field(description="Updated comment text in Markdown format")],
    visibility: Annotated[
        str | None,
        Field(
            description='(Optional) Comment visibility as JSON string (e.g. \'{"type":"group","value":"jira-users"}\')'
        ),
    ] = None,
) -> str:
    """Edit an existing comment on a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        comment_id: The ID of the comment to edit.
        body: Updated comment text in Markdown.
        visibility: (Optional) Comment visibility as JSON string.

    Returns:
        JSON string representing the updated comment object.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    # Req 27.1/27.2/27.4: validate visibility BEFORE acquiring the fetcher
    # so malformed input issues zero PUT against /rest/api/2/issue/.../comment/{id}.
    visibility_dict = _parse_visibility(visibility)
    if isinstance(visibility_dict, dc_guards.StructuredError):
        return json.dumps({"success": False, **visibility_dict.to_dict()})

    jira = await get_jira_fetcher(ctx)
    result = jira.edit_comment(issue_key, comment_id, body, visibility_dict)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_worklog"},
    annotations={"title": "Add Worklog", "destructiveHint": True},
)
@check_write_access
async def add_worklog(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    time_spent: Annotated[
        str,
        Field(
            description=(
                "Time spent in Jira format. Examples: "
                "'1h 30m' (1 hour and 30 minutes), '1d' (1 day), '30m' (30 minutes), '4h' (4 hours)"
            )
        ),
    ],
    comment: Annotated[
        str | None,
        Field(description="(Optional) Comment for the worklog in Markdown format"),
    ] = None,
    started: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Start time in ISO format. If not provided, the current time will be used. "
                "Example: '2023-08-01T12:00:00.000+0000'"
            )
        ),
    ] = None,
    # Add original_estimate and remaining_estimate as per original tool
    original_estimate: Annotated[
        str | None, Field(description="(Optional) New value for the original estimate")
    ] = None,
    remaining_estimate: Annotated[
        str | None, Field(description="(Optional) New value for the remaining estimate")
    ] = None,
) -> str:
    """Add a worklog entry to a Jira issue.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        time_spent: Time spent in Jira format.
        comment: Optional comment in Markdown.
        started: Optional start time in ISO format.
        original_estimate: Optional new original estimate.
        remaining_estimate: Optional new remaining estimate.


    Returns:
        JSON string representing the added worklog object.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    # add_worklog returns dict
    worklog_result = jira.add_worklog(
        issue_key=issue_key,
        time_spent=time_spent,
        comment=comment,
        started=started,
        original_estimate=original_estimate,
        remaining_estimate=remaining_estimate,
    )
    result = {"message": "Worklog added successfully", "worklog": worklog_result}
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_links"},
    annotations={"title": "Link to Epic", "destructiveHint": True},
)
@check_write_access
async def link_to_epic(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="The key of the issue to link (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    epic_key: Annotated[
        str,
        Field(
            description="The key of the epic to link to (e.g., 'PROJ-456')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Link an existing issue to an epic.

    Args:
        ctx: The FastMCP context.
        issue_key: The key of the issue to link.
        epic_key: The key of the epic to link to.

    Returns:
        JSON string representing the updated issue object.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    issue = jira.link_issue_to_epic(issue_key, epic_key)
    result = {
        "message": f"Issue {issue_key} has been linked to epic {epic_key}.",
        "issue": issue.to_simplified_dict(),
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_links"},
    annotations={"title": "Create Issue Link", "destructiveHint": True},
)
@check_write_access
async def create_issue_link(
    ctx: Context,
    link_type: Annotated[
        str,
        Field(
            description="The type of link to create (e.g., 'Duplicate', 'Blocks', 'Relates to')"
        ),
    ],
    inward_issue_key: Annotated[
        str,
        Field(
            description="The key of the inward issue (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    outward_issue_key: Annotated[
        str,
        Field(
            description="The key of the outward issue (e.g., 'PROJ-456')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    comment: Annotated[
        str | None, Field(description="(Optional) Comment to add to the link")
    ] = None,
    comment_visibility: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Visibility settings for the comment as JSON string "
                '(e.g. \'{"type":"group","value":"jira-users"}\')'
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create a link between two Jira issues.

    Args:
        ctx: The FastMCP context.
        link_type: The type of link (e.g., 'Blocks').
        inward_issue_key: The key of the source issue.
        outward_issue_key: The key of the target issue.
        comment: Optional comment text.
        comment_visibility: Optional JSON string for comment visibility.

    Returns:
        JSON string indicating success or failure.

    Raises:
        ValueError: If required fields are missing, invalid input, in read-only mode, or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    if not all([link_type, inward_issue_key, outward_issue_key]):
        raise ValueError(
            "link_type, inward_issue_key, and outward_issue_key are required."
        )

    # Req 27.1/27.2/27.4: validate visibility BEFORE issuing the link POST
    # so malformed input issues zero POST against /rest/api/2/issueLink.
    visibility_dict = _parse_visibility(comment_visibility, "comment_visibility")
    if isinstance(visibility_dict, dc_guards.StructuredError):
        return json.dumps({"success": False, **visibility_dict.to_dict()})

    link_data = {
        "type": {"name": link_type},
        "inwardIssue": {"key": inward_issue_key},
        "outwardIssue": {"key": outward_issue_key},
    }

    if comment:
        comment_obj: dict[str, Any] = {"body": comment}
        if visibility_dict:
            if "type" in visibility_dict and "value" in visibility_dict:
                comment_obj["visibility"] = visibility_dict
            else:
                logger.warning("Invalid comment_visibility dictionary structure.")
        link_data["comment"] = comment_obj

    result = jira.create_issue_link(link_data)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_links"},
    annotations={"title": "Create Remote Issue Link", "destructiveHint": True},
)
@check_write_access
async def create_remote_issue_link(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="The key of the issue to add the link to (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    url: Annotated[
        str,
        Field(
            description="The URL to link to (e.g., 'https://example.com/page' or Confluence page URL)"
        ),
    ],
    title: Annotated[
        str,
        Field(
            description="The title/name of the link (e.g., 'Documentation Page', 'Confluence Page')"
        ),
    ],
    summary: Annotated[
        str | None, Field(description="(Optional) Description of the link")
    ] = None,
    relationship: Annotated[
        str | None,
        Field(
            description="(Optional) Relationship description (e.g., 'causes', 'relates to', 'documentation')"
        ),
    ] = None,
    icon_url: Annotated[
        str | None, Field(description="(Optional) URL to a 16x16 icon for the link")
    ] = None,
) -> str:
    """Create a remote issue link (web link or Confluence link) for a Jira issue.

    This tool allows you to add web links and Confluence links to Jira issues.
    The links will appear in the issue's "Links" section and can be clicked to navigate to external resources.

    Args:
        ctx: The FastMCP context.
        issue_key: The key of the issue to add the link to.
        url: The URL to link to (can be any web page or Confluence page).
        title: The title/name that will be displayed for the link.
        summary: Optional description of what the link is for.
        relationship: Optional relationship description.
        icon_url: Optional URL to a 16x16 icon for the link.

    Returns:
        JSON string indicating success or failure.

    Raises:
        ValueError: If required fields are missing, invalid input, in read-only mode, or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    if not issue_key:
        raise ValueError("issue_key is required.")
    if not url:
        raise ValueError("url is required.")
    if not title:
        raise ValueError("title is required.")

    # Build the remote link data structure
    link_object = {
        "url": url,
        "title": title,
    }

    if summary:
        link_object["summary"] = summary

    if icon_url:
        link_object["icon"] = {"url16x16": icon_url, "title": title}

    link_data = {"object": link_object}

    if relationship:
        link_data["relationship"] = relationship

    result = jira.create_remote_issue_link(issue_key, link_data)
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_links"},
    annotations={"title": "Remove Issue Link", "destructiveHint": True},
)
@check_write_access
async def remove_issue_link(
    ctx: Context,
    link_id: Annotated[str, Field(description="The ID of the link to remove")],
) -> str:
    """Remove a link between two Jira issues.

    Args:
        ctx: The FastMCP context.
        link_id: The ID of the link to remove.

    Returns:
        JSON string indicating success.

    Raises:
        ValueError: If link_id is missing, in read-only mode, or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    if not link_id:
        raise ValueError("link_id is required")

    result = jira.remove_issue_link(link_id)  # Returns dict on success
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_transitions"},
    annotations={"title": "Transition Issue", "destructiveHint": True},
)
@check_write_access
async def transition_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    transition_id: Annotated[
        str,
        Field(
            description=(
                "ID of the transition to perform. Use the jira_get_transitions tool first "
                "to get the available transition IDs for the issue. Example values: '11', '21', '31'"
            )
        ),
    ],
    fields: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) JSON string of fields to update during the transition. "
                "Some transitions require specific fields to be set (e.g., resolution). "
                'Example: \'{"resolution": {"name": "Fixed"}}\''
            ),
            default=None,
        ),
    ] = None,
    comment: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comment to add during the transition in Markdown format. "
                "This will be visible in the issue history."
            ),
        ),
    ] = None,
) -> str:
    """Transition a Jira issue to a new status.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key.
        transition_id: ID of the transition.
        fields: Optional JSON string of fields to update during transition.
        comment: Optional comment for the transition in Markdown format.

    Returns:
        JSON string representing the updated issue object.

    Raises:
        ValueError: If required fields missing, invalid input, in read-only mode, or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    if not issue_key or not transition_id:
        raise ValueError("issue_key and transition_id are required.")

    # Parse fields from JSON string
    update_fields = _parse_additional_fields(fields)

    issue = jira.transition_issue(
        issue_key=issue_key,
        transition_id=transition_id,
        fields=update_fields,
        comment=comment,
    )

    result = {
        "message": f"Issue {issue_key} transitioned successfully",
        "issue": issue.to_simplified_dict() if issue else None,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_agile"},
    annotations={"title": "Create Sprint", "destructiveHint": True},
)
@check_write_access
async def create_sprint(
    ctx: Context,
    board_id: Annotated[str, Field(description="The id of board (e.g., '1000')")],
    name: Annotated[str, Field(description="Name of the sprint (e.g., 'Sprint 1')")],
    start_date: Annotated[
        str, Field(description="Start time for sprint (ISO 8601 format)")
    ],
    end_date: Annotated[
        str, Field(description="End time for sprint (ISO 8601 format)")
    ],
    goal: Annotated[
        str | None, Field(description="(Optional) Goal of the sprint")
    ] = None,
) -> str:
    """Create Jira sprint for a board.

    Args:
        ctx: The FastMCP context.
        board_id: Board ID.
        name: Sprint name.
        start_date: Start date (ISO format).
        end_date: End date (ISO format).
        goal: Optional sprint goal.

    Returns:
        JSON string representing the created sprint object.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    sprint = jira.create_sprint(
        board_id=board_id,
        sprint_name=name,
        start_date=start_date,
        end_date=end_date,
        goal=goal,
    )
    return json.dumps(sprint.to_simplified_dict(), indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_agile"},
    annotations={"title": "Update Sprint", "destructiveHint": True},
)
@check_write_access
async def update_sprint(
    ctx: Context,
    sprint_id: Annotated[str, Field(description="The id of sprint (e.g., '10001')")],
    name: Annotated[
        str | None, Field(description="(Optional) New name for the sprint")
    ] = None,
    state: Annotated[
        str | None,
        Field(description="(Optional) New state for the sprint (future|active|closed)"),
    ] = None,
    start_date: Annotated[
        str | None, Field(description="(Optional) New start date for the sprint")
    ] = None,
    end_date: Annotated[
        str | None, Field(description="(Optional) New end date for the sprint")
    ] = None,
    goal: Annotated[
        str | None, Field(description="(Optional) New goal for the sprint")
    ] = None,
) -> str:
    """Update jira sprint.

    Args:
        ctx: The FastMCP context.
        sprint_id: The ID of the sprint.
        name: Optional new name.
        state: Optional new state (future|active|closed).
        start_date: Optional new start date.
        end_date: Optional new end date.
        goal: Optional new goal.

    Returns:
        JSON string representing the updated sprint object or an error message.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    sprint = jira.update_sprint(
        sprint_id=sprint_id,
        sprint_name=name,
        state=state,
        start_date=start_date,
        end_date=end_date,
        goal=goal,
    )

    if sprint is None:
        error_payload = {
            "error": f"Failed to update sprint {sprint_id}. Check logs for details."
        }
        return json.dumps(error_payload, indent=2, ensure_ascii=False)
    else:
        return json.dumps(sprint.to_simplified_dict(), indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_agile"},
    annotations={"title": "Add Issues to Sprint", "readOnlyHint": False},
)
@check_write_access
async def add_issues_to_sprint(
    ctx: Context,
    sprint_id: Annotated[str, Field(description="Sprint ID to add issues to")],
    issue_keys: Annotated[
        str,
        Field(description="Comma-separated issue keys (e.g., 'PROJ-1,PROJ-2')"),
    ],
) -> str:
    """Add issues to a Jira sprint.

    Args:
        ctx: The FastMCP context.
        sprint_id: The ID of the sprint.
        issue_keys: Comma-separated issue keys.

    Returns:
        JSON string with success message.

    Raises:
        ValueError: If in read-only mode or Jira client unavailable.
    """
    jira = await get_jira_fetcher(ctx)
    keys_list = [k.strip() for k in issue_keys.split(",") if k.strip()]
    jira.add_issues_to_sprint(sprint_id, keys_list)
    result = {
        "message": f"Successfully added {len(keys_list)} issue(s) to sprint",
        "sprint_id": sprint_id,
        "issue_keys": keys_list,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_projects"},
    annotations={"title": "Get Project Versions", "readOnlyHint": True},
)
async def get_project_versions(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get all fix versions for a specific Jira project."""
    jira = await get_jira_fetcher(ctx)
    versions = jira.get_project_versions(project_key)
    return json.dumps(versions, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_projects"},
    annotations={"title": "Get Project Components", "readOnlyHint": True},
)
async def get_project_components(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get all components for a specific Jira project."""
    jira = await get_jira_fetcher(ctx)
    components = jira.get_project_components(project_key)
    return json.dumps(components, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_projects"},
    annotations={"title": "Get All Projects", "readOnlyHint": True},
)
async def get_all_projects(
    ctx: Context,
    include_archived: Annotated[
        bool,
        Field(
            description="Whether to include archived projects in the results",
            default=False,
        ),
    ] = False,
) -> str:
    """Get all Jira projects accessible to the current user.

    Args:
        ctx: The FastMCP context.
        include_archived: Whether to include archived projects.

    Returns:
        JSON string representing a list of project objects accessible to the user.
        Project keys are always returned in uppercase.
        If JIRA_PROJECTS_FILTER is configured, only returns projects matching those keys.

    Raises:
        ValueError: If the Jira client is not configured or available.
    """
    try:
        jira = await get_jira_fetcher(ctx)
        projects = jira.get_all_projects(include_archived=include_archived)
    except (MCPAtlassianAuthenticationError, HTTPError, OSError, ValueError) as e:
        error_message = ""
        log_level = logging.ERROR
        if isinstance(e, MCPAtlassianAuthenticationError):
            error_message = f"Authentication/Permission Error: {str(e)}"
        elif isinstance(e, OSError | HTTPError):
            error_message = f"Network or API Error: {str(e)}"
        elif isinstance(e, ValueError):
            error_message = f"Configuration Error: {str(e)}"

        error_result = {
            "success": False,
            "error": error_message,
        }
        logger.log(log_level, f"get_all_projects failed: {error_message}")
        return json.dumps(error_result, indent=2, ensure_ascii=False)

    # Ensure all project keys are uppercase
    for project in projects:
        if "key" in project:
            project["key"] = project["key"].upper()

    # Apply project filter if configured
    if jira.config.projects_filter:
        # Split projects filter by commas and handle possible whitespace
        allowed_project_keys = {
            p.strip().upper() for p in jira.config.projects_filter.split(",")
        }
        projects = [
            project
            for project in projects
            if project.get("key") in allowed_project_keys
        ]

    return json.dumps(projects, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_service_desk"},
    annotations={
        "title": "Get Service Desk For Project",
        "readOnlyHint": True,
    },
)
async def get_service_desk_for_project(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'SUP')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
) -> str:
    """
    Get the Jira Service Desk associated with a project key.

    Server/Data Center only. Not available on Jira Cloud.

    Args:
        ctx: The FastMCP context.
        project_key: Jira project key.

    Returns:
        JSON string with project key and service desk data (or null if not found).

    Raises:
        NotImplementedError: If connected to Jira Cloud (Server/DC only).
    """
    jira = await get_jira_fetcher(ctx)
    service_desk = jira.get_service_desk_for_project(project_key=project_key)
    result = {
        "project_key": project_key.upper(),
        "service_desk": service_desk.to_simplified_dict() if service_desk else None,
    }
    return json.dumps(result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_service_desk"},
    annotations={"title": "Get Service Desk Queues", "readOnlyHint": True},
)
async def get_service_desk_queues(
    ctx: Context,
    service_desk_id: Annotated[
        str,
        Field(description="Service desk ID (e.g., '4')"),
    ],
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=50, ge=1, le=50),
    ] = 50,
) -> str:
    """
    Get queues for a Jira Service Desk.

    Server/Data Center only. Not available on Jira Cloud.

    Args:
        ctx: The FastMCP context.
        service_desk_id: Service desk ID.
        start_at: Starting index for pagination.
        limit: Maximum number of queues to return.

    Returns:
        JSON string with queue list and pagination metadata.

    Raises:
        NotImplementedError: If connected to Jira Cloud (Server/DC only).
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.get_service_desk_queues(
        service_desk_id=service_desk_id,
        start_at=start_at,
        limit=limit,
        include_count=True,
    )
    return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_service_desk"},
    annotations={"title": "Get Queue Issues", "readOnlyHint": True},
)
async def get_queue_issues(
    ctx: Context,
    service_desk_id: Annotated[
        str,
        Field(description="Service desk ID (e.g., '4')"),
    ],
    queue_id: Annotated[
        str,
        Field(description="Queue ID (e.g., '47')"),
    ],
    start_at: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of results (1-50)", default=50, ge=1),
    ] = 50,
) -> str:
    """
    Get issues from a Jira Service Desk queue.

    Server/Data Center only. Not available on Jira Cloud.

    Args:
        ctx: The FastMCP context.
        service_desk_id: Service desk ID.
        queue_id: Queue ID.
        start_at: Starting index for pagination.
        limit: Maximum number of issues to return.

    Returns:
        JSON string with queue metadata, issues, and pagination metadata.

    Raises:
        NotImplementedError: If connected to Jira Cloud (Server/DC only).
    """
    jira = await get_jira_fetcher(ctx)
    result = jira.get_queue_issues(
        service_desk_id=service_desk_id,
        queue_id=queue_id,
        start_at=start_at,
        limit=limit,
    )
    return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_projects"},
    annotations={"title": "Create Version", "destructiveHint": True},
)
@check_write_access
async def create_version(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    name: Annotated[str, Field(description="Name of the version")],
    start_date: Annotated[
        str | None, Field(description="Start date (YYYY-MM-DD)", default=None)
    ] = None,
    release_date: Annotated[
        str | None, Field(description="Release date (YYYY-MM-DD)", default=None)
    ] = None,
    description: Annotated[
        str | None, Field(description="Description of the version", default=None)
    ] = None,
) -> str:
    """Create a new fix version in a Jira project.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        name: Name of the version.
        start_date: Start date (optional).
        release_date: Release date (optional).
        description: Description (optional).

    Returns:
        JSON string of the created version object.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        version = jira.create_project_version(
            project_key=project_key,
            name=name,
            start_date=start_date,
            release_date=release_date,
            description=description,
        )
        return json.dumps(version, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(
            f"Error creating version in project {project_key}: {str(e)}", exc_info=True
        )
        return json.dumps(
            {"success": False, "error": str(e)}, indent=2, ensure_ascii=False
        )


@jira_mcp.tool(
    name="batch_create_versions",
    tags={"jira", "write", "toolset:jira_projects"},
    annotations={"title": "Batch Create Versions", "destructiveHint": True},
)
@check_write_access
async def batch_create_versions(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    versions: Annotated[
        str,
        Field(
            description=(
                "JSON array of version objects. Each object should contain:\n"
                "- name (required): Name of the version\n"
                "- startDate (optional): Start date (YYYY-MM-DD)\n"
                "- releaseDate (optional): Release date (YYYY-MM-DD)\n"
                "- description (optional): Description of the version\n"
                "Example: [\n"
                '  {"name": "v1.0", "startDate": "2025-01-01", "releaseDate": "2025-02-01", "description": "First release"},\n'
                '  {"name": "v2.0"}\n'
                "]"
            )
        ),
    ],
) -> str:
    """Batch create multiple versions in a Jira project.

    Args:
        ctx: The FastMCP context.
        project_key: The project key.
        versions: JSON array string of version objects.

    Returns:
        JSON array of results, each with success flag, version or error.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        version_list = json.loads(versions)
        if not isinstance(version_list, list):
            raise ValueError("Input 'versions' must be a JSON array string.")
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON in versions")
    except Exception as e:
        raise ValueError(f"Invalid input for versions: {e}") from e

    results = []
    if not version_list:
        return json.dumps(results, indent=2, ensure_ascii=False)

    for idx, v in enumerate(version_list):
        # Defensive: ensure v is a dict and has a name
        if not isinstance(v, dict) or not v.get("name"):
            results.append(
                {
                    "success": False,
                    "error": f"Item {idx}: Each version must be an object with at least a 'name' field.",
                }
            )
            continue
        try:
            version = jira.create_project_version(
                project_key=project_key,
                name=v["name"],
                start_date=v.get("startDate"),
                release_date=v.get("releaseDate"),
                description=v.get("description"),
            )
            results.append({"success": True, "version": version})
        except Exception as e:
            logger.error(
                f"Error creating version in batch for project {project_key}: {str(e)}",
                exc_info=True,
            )
            results.append({"success": False, "error": str(e), "input": v})
    return json.dumps(results, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_forms"},
    annotations={"title": "Get Issue Forms", "readOnlyHint": True},
)
async def get_issue_proforma_forms(
    ctx: Context,
    issue_key: Annotated[str, Field(description="Jira issue key (e.g., 'PROJ-123')")],
) -> str:
    """
    Get all ProForma forms associated with a Jira issue.

    Uses the new Jira Forms REST API. Form IDs are returned as UUIDs.

    Args:
        ctx: The FastMCP context.
        issue_key: The issue key to get forms for.

    Returns:
        JSON string representing the list of ProForma forms, or an error object if failed.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        forms = jira.get_issue_forms(issue_key)
        forms_data = [form.to_simplified_dict() for form in forms]
        response_data = {"success": True, "forms": forms_data, "count": len(forms)}
    except Exception as e:
        error_message = ""
        log_level = logging.ERROR
        if isinstance(e, ValueError) and "not found" in str(e).lower():
            log_level = logging.WARNING
            error_message = str(e)
        elif isinstance(e, MCPAtlassianAuthenticationError):
            error_message = f"Authentication/Permission Error: {str(e)}"
        elif isinstance(e, OSError | HTTPError):
            error_message = f"Network or API Error: {str(e)}"
        else:
            error_message = (
                "An unexpected error occurred while fetching ProForma forms."
            )
            logger.exception(
                f"Unexpected error in get_issue_proforma_forms for '{issue_key}':"
            )
        error_result = {
            "success": False,
            "error": str(e),
            "issue_key": issue_key,
        }
        logger.log(
            log_level,
            f"get_issue_proforma_forms failed for '{issue_key}': {error_message}",
        )
        response_data = error_result
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "toolset:jira_forms"},
    annotations={"title": "Get Form Details", "readOnlyHint": True},
)
async def get_proforma_form_details(
    ctx: Context,
    issue_key: Annotated[str, Field(description="Jira issue key (e.g., 'PROJ-123')")],
    form_id: Annotated[
        str,
        Field(
            description="ProForma form UUID (e.g., '1946b8b7-8f03-4dc0-ac2d-5fac0d960c6a')"
        ),
    ],
) -> str:
    """
    Get detailed information about a specific ProForma form.

    Uses the new Jira Forms REST API. Returns form details including ADF design structure.

    Args:
        ctx: The FastMCP context.
        issue_key: The issue key containing the form.
        form_id: The form UUID identifier.

    Returns:
        JSON string representing the ProForma form details, or an error object if failed.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        form = jira.get_form_details(issue_key, form_id)
        if form is None:
            response_data = {
                "success": False,
                "error": f"Form {form_id} not found for issue {issue_key}",
                "issue_key": issue_key,
                "form_id": form_id,
            }
        else:
            response_data = {"success": True, "form": form.to_simplified_dict()}
    except Exception as e:
        error_message = ""
        log_level = logging.ERROR
        if isinstance(e, ValueError) and "not found" in str(e).lower():
            log_level = logging.WARNING
            error_message = str(e)
        elif isinstance(e, MCPAtlassianAuthenticationError):
            error_message = f"Authentication/Permission Error: {str(e)}"
        elif isinstance(e, OSError | HTTPError):
            error_message = f"Network or API Error: {str(e)}"
        else:
            error_message = (
                "An unexpected error occurred while fetching ProForma form details."
            )
            logger.exception(
                f"Unexpected error in get_proforma_form_details for '{issue_key}/{form_id}':"
            )
        error_result = {
            "success": False,
            "error": str(e),
            "issue_key": issue_key,
            "form_id": form_id,
        }
        logger.log(
            log_level,
            f"get_proforma_form_details failed for '{issue_key}/{form_id}': {error_message}",
        )
        response_data = error_result
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "write", "toolset:jira_forms"},
    annotations={"title": "Update Form Answers", "destructiveHint": True},
)
@check_write_access
async def update_proforma_form_answers(
    ctx: Context,
    issue_key: Annotated[str, Field(description="Jira issue key (e.g., 'PROJ-123')")],
    form_id: Annotated[
        str,
        Field(
            description="ProForma form UUID (e.g., '1946b8b7-8f03-4dc0-ac2d-5fac0d960c6a')"
        ),
    ],
    answers: Annotated[
        list[dict],
        Field(
            description="List of answer objects. Each answer must have: questionId (string), type (TEXT/NUMBER/SELECT/etc), value (any)"
        ),
    ],
) -> str:
    """
    Update form field answers using the Jira Forms REST API.

    This is the primary method for updating form data. Each answer object
    must specify the question ID, answer type, and value.

    **⚠️ KNOWN LIMITATION - DATETIME fields:**
    The Jira Forms API does NOT properly preserve time components in DATETIME fields.
    Only the date portion is stored; times are reset to midnight (00:00:00).

    **Workaround for DATETIME fields:**
    Use jira_update_issue to directly update the underlying custom fields instead:
    1. Get the custom field ID from the form details (question's "jiraField" property)
    2. Use jira_update_issue with fields like: {"customfield_XXXXX": "2026-01-09T11:50:00-08:00"}

    Example:
    ```python
    # Instead of updating via form (loses time):
    # jira_update_proforma_form_answers(issue_key, form_id, [{"questionId": "91", "type": "DATETIME", "value": "..."}])

    # Use direct field update (preserves time):
    jira_update_issue(issue_key, {"customfield_10542": "2026-01-09T11:50:00-08:00"})
    ```

    **Automatic DateTime Conversion:**
    For DATE and DATETIME fields, you can provide values as:
    - ISO 8601 strings (e.g., "2024-12-17T19:00:00Z", "2024-12-17")
    - Unix timestamps in milliseconds (e.g., 1734465600000)

    The tool automatically converts ISO 8601 strings to Unix timestamps.

    Example answers:
    [
        {"questionId": "q1", "type": "TEXT", "value": "Updated description"},
        {"questionId": "q2", "type": "SELECT", "value": "Product A"},
        {"questionId": "q3", "type": "NUMBER", "value": 42},
        {"questionId": "q4", "type": "DATE", "value": "2024-12-17"}
    ]

    Common answer types:
    - TEXT: String values
    - NUMBER: Numeric values
    - DATE: Date values (ISO 8601 string or Unix timestamp in ms)
    - DATETIME: DateTime values - ⚠️ USE WORKAROUND ABOVE
    - SELECT: Single selection from options
    - MULTI_SELECT: Multiple selections (value as list)
    - CHECKBOX: Boolean values

    Args:
        ctx: The FastMCP context.
        issue_key: The issue key containing the form.
        form_id: The form UUID (get from get_issue_proforma_forms).
        answers: List of answer objects with questionId, type, and value.

    Returns:
        JSON string with operation result.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        # Convert datetime strings to Unix timestamps for DATE/DATETIME fields
        processed_answers = []
        for answer in answers:
            processed_answer = answer.copy()
            if "type" in answer and "value" in answer:
                processed_answer["value"] = convert_datetime_to_timestamp(
                    answer["value"], answer["type"]
                )
            processed_answers.append(processed_answer)

        result = jira.update_form_answers(issue_key, form_id, processed_answers)
        response_data = {
            "success": True,
            "message": f"Successfully updated form {form_id} for issue {issue_key}",
            "issue_key": issue_key,
            "form_id": form_id,
            "updated_fields": len(answers),
            "result": result,
        }
    except Exception as e:
        error_message = ""
        log_level = logging.ERROR
        if isinstance(e, ValueError) and "not found" in str(e).lower():
            log_level = logging.WARNING
            error_message = str(e)
        elif isinstance(e, MCPAtlassianAuthenticationError):
            error_message = f"Authentication/Permission Error: {str(e)}"
        elif isinstance(e, OSError | HTTPError):
            error_message = f"Network or API Error: {str(e)}"
        else:
            error_message = (
                "An unexpected error occurred while updating ProForma form answers."
            )
            logger.exception(
                f"Unexpected error in update_proforma_form_answers for '{issue_key}/{form_id}':"
            )
        error_result = {
            "success": False,
            "error": str(e),
            "issue_key": issue_key,
            "form_id": form_id,
        }
        logger.log(
            log_level,
            f"update_proforma_form_answers failed for '{issue_key}/{form_id}': {error_message}",
        )
        response_data = error_result
    return json.dumps(response_data, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "metrics", "toolset:jira_metrics"},
    annotations={"title": "Get Issue Dates", "readOnlyHint": True},
)
async def get_issue_dates(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    include_status_changes: Annotated[
        bool,
        Field(
            description="Include status change history with timestamps and durations"
        ),
    ] = True,
    include_status_summary: Annotated[
        bool,
        Field(description="Include aggregated time spent in each status"),
    ] = True,
) -> str:
    """
    Get date information and status transition history for a Jira issue.

    Returns dates (created, updated, due date, resolution date) and optionally
    status change history with time tracking for workflow analysis.

    Args:
        ctx: The FastMCP context.
        issue_key: The Jira issue key.
        include_status_changes: Whether to include status change history.
        include_status_summary: Whether to include aggregated time per status.

    Returns:
        JSON string with issue dates and optional status tracking data.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        result = jira.get_issue_dates(
            issue_key=issue_key,
            include_created=True,
            include_updated=True,
            include_due_date=True,
            include_resolution_date=True,
            include_status_changes=include_status_changes,
            include_status_summary=include_status_summary,
        )
        return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error getting issue dates for {issue_key}: {str(e)}")
        error_result = {"success": False, "error": str(e), "issue_key": issue_key}
        return json.dumps(error_result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "metrics", "sla", "toolset:jira_metrics"},
    annotations={"title": "Get Issue SLA", "readOnlyHint": True},
)
async def get_issue_sla(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    metrics: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated list of SLA metrics to calculate. "
                "Available: cycle_time, lead_time, time_in_status, due_date_compliance, "
                "resolution_time, first_response_time. "
                "Defaults to configured metrics or 'cycle_time,time_in_status'."
            )
        ),
    ] = None,
    working_hours_only: Annotated[
        bool | None,
        Field(
            description=(
                "Calculate using working hours only (excludes weekends/non-business hours). "
                "Defaults to value from JIRA_SLA_WORKING_HOURS_ONLY environment variable."
            )
        ),
    ] = None,
    include_raw_dates: Annotated[
        bool,
        Field(description="Include raw date values in the response"),
    ] = False,
) -> str:
    """
    Calculate SLA metrics for a Jira issue.

    Computes various time-based metrics including cycle time, lead time,
    time spent in each status, due date compliance, and more.

    Working hours can be configured via environment variables:
    - JIRA_SLA_WORKING_HOURS_ONLY: Enable working hours filtering (true/false)
    - JIRA_SLA_WORKING_HOURS_START: Start time (e.g., "09:00")
    - JIRA_SLA_WORKING_HOURS_END: End time (e.g., "17:00")
    - JIRA_SLA_WORKING_DAYS: Working days (e.g., "1,2,3,4,5" for Mon-Fri)
    - JIRA_SLA_TIMEZONE: Timezone for calculations (e.g., "America/New_York")

    Args:
        ctx: The FastMCP context.
        issue_key: The Jira issue key.
        metrics: Comma-separated list of metrics to calculate.
        working_hours_only: Use working hours only for calculations.
        include_raw_dates: Include raw date values in response.

    Returns:
        JSON string with calculated SLA metrics.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        # Parse metrics from comma-separated string
        metrics_list = None
        if metrics:
            metrics_list = [m.strip() for m in metrics.split(",") if m.strip()]

        result = jira.get_issue_sla(
            issue_key=issue_key,
            metrics=metrics_list,
            working_hours_only=working_hours_only,
            include_raw_dates=include_raw_dates,
        )
        return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error calculating SLA for {issue_key}: {str(e)}")
        error_result = {"success": False, "error": str(e), "issue_key": issue_key}
        return json.dumps(error_result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "development", "toolset:jira_development"},
    annotations={"title": "Get Issue Development Info", "readOnlyHint": True},
)
async def get_issue_development_info(
    ctx: Context,
    issue_key: Annotated[str, Field(description="Jira issue key (e.g., 'PROJ-123')")],
    application_type: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter by application type. "
                "Examples: 'stash' (Bitbucket Server), 'bitbucket', 'github', 'gitlab'"
            )
        ),
    ] = None,
    data_type: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter by data type. "
                "Examples: 'pullrequest', 'branch', 'repository'"
            )
        ),
    ] = None,
) -> str:
    """
    Get development information (PRs, commits, branches) linked to a Jira issue.

    This retrieves the development panel information that shows linked
    pull requests, branches, and commits from connected source control systems
    like Bitbucket, GitHub, or GitLab.

    Args:
        ctx: The FastMCP context.
        issue_key: The Jira issue key.
        application_type: Optional filter by source control type.
        data_type: Optional filter by data type (pullrequest, branch, etc.).

    Returns:
        JSON string with development information including:
        - pullRequests: List of linked pull requests with status, author, reviewers
        - branches: List of linked branches
        - commits: List of linked commits
        - repositories: List of repositories involved
    """
    jira = await get_jira_fetcher(ctx)
    try:
        result = jira.get_issue_development_info(
            issue_key=issue_key,
            application_type=application_type,
            data_type=data_type,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error getting development info for {issue_key}: {str(e)}")
        error_result = {"success": False, "error": str(e), "issue_key": issue_key}
        return json.dumps(error_result, indent=2, ensure_ascii=False)


@jira_mcp.tool(
    tags={"jira", "read", "development", "toolset:jira_development"},
    annotations={"title": "Get Issues Development Info", "readOnlyHint": True},
)
async def get_issues_development_info(
    ctx: Context,
    issue_keys: Annotated[
        str,
        Field(
            description="Comma-separated list of Jira issue keys (e.g., 'PROJ-123,PROJ-456')"
        ),
    ],
    application_type: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter by application type. "
                "Examples: 'stash' (Bitbucket Server), 'bitbucket', 'github', 'gitlab'"
            )
        ),
    ] = None,
    data_type: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter by data type. "
                "Examples: 'pullrequest', 'branch', 'repository'"
            )
        ),
    ] = None,
) -> str:
    """
    Get development information for multiple Jira issues.

    Batch retrieves development panel information (PRs, commits, branches)
    for multiple issues at once.

    Args:
        ctx: The FastMCP context.
        issue_keys: List of Jira issue keys.
        application_type: Optional filter by source control type.
        data_type: Optional filter by data type.

    Returns:
        JSON string with list of development information for each issue.
    """
    jira = await get_jira_fetcher(ctx)
    # Parse CSV string into list
    keys_list = [k.strip() for k in issue_keys.split(",") if k.strip()]
    try:
        results = jira.get_issues_development_info(
            issue_keys=keys_list,
            application_type=application_type,
            data_type=data_type,
        )
        return json.dumps(results, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error getting development info for issues: {str(e)}")
        error_result = {"success": False, "error": str(e)}
        return json.dumps(error_result, indent=2, ensure_ascii=False)

# =============================================================================
# Jira Filters (toolset:jira_filters) — atlassian-dc-tool-parity Requirement 15
# =============================================================================
#
# Filters are not project-scoped so these tools skip ``check_project_filter``.
# Read tools skip ``check_read_only``; write tools run it before any HTTP.
# ``jira_delete_own_filter`` additionally resolves the filter owner and calls
# ``require_owner`` before issuing the DELETE, emitting ``not_filter_owner``
# when the authenticated user does not own the target filter (Req 15.3, 15.4,
# 46.1, 46.2). Read-only mode blocks the DELETE before any owner resolution.


_FILTERS_READ_TAGS = {"jira", "read", "toolset:jira_filters"}
_FILTERS_WRITE_TAGS = {"jira", "write", "toolset:jira_filters"}


@jira_mcp.tool(
    name="list_my_filters",
    tags=_FILTERS_READ_TAGS,
    annotations={"title": "List My Filters", "readOnlyHint": True},
)
async def jira_list_my_filters(
    ctx: Context,
    include_favourites: Annotated[
        bool,
        Field(
            description=(
                "Whether to include the authenticated user's favourite "
                "filters in the response (forwarded to Jira as the "
                "``includeFavourites`` query flag). Default True."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """List filters owned by the authenticated user (DC).

    Calls ``GET /rest/api/2/filter/my``.

    Returns:
        JSON string with the list of filter objects.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        filters = jira.list_my_filters(include_favourites=include_favourites)
        return json.dumps(
            {"success": True, "count": len(filters), "filters": filters},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing my filters: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="get_filter",
    tags=_FILTERS_READ_TAGS,
    annotations={"title": "Get Filter", "readOnlyHint": True},
)
async def jira_get_filter(
    ctx: Context,
    filter_id: Annotated[
        str,
        Field(description="Jira filter identifier (e.g., '10001')"),
    ],
) -> str:
    """Fetch a single filter by id (DC).

    Calls ``GET /rest/api/2/filter/{filter_id}``.

    Returns:
        JSON string with the filter object.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        filter_obj = jira.get_filter(filter_id)
        return json.dumps(
            {"success": True, "filter": filter_obj},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error getting filter {filter_id}: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="search_filters",
    tags=_FILTERS_READ_TAGS,
    annotations={"title": "Search Filters", "readOnlyHint": True},
)
async def jira_search_filters(
    ctx: Context,
    filter_name: Annotated[
        str | None,
        Field(
            description="Substring to match against the filter name.",
            default=None,
        ),
    ] = None,
    account_id: Annotated[
        str | None,
        Field(
            description=(
                "Cloud account id of the filter owner (accepted by DC for "
                "forward-compat; generally unused on Server/DC)."
            ),
            default=None,
        ),
    ] = None,
    owner: Annotated[
        str | None,
        Field(
            description="Server/DC username of the filter owner.",
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum number of filters to return. Default 25.",
            default=25,
            ge=1,
        ),
    ] = 25,
) -> str:
    """Search filters by name and/or owner (DC).

    Calls ``GET /rest/api/2/filter/search``.

    Returns:
        JSON string with the matched filters.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        filters = jira.search_filters(
            filter_name=filter_name,
            account_id=account_id,
            owner=owner,
            limit=limit,
        )
        return json.dumps(
            {"success": True, "count": len(filters), "filters": filters},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching filters: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="create_filter",
    tags=_FILTERS_WRITE_TAGS,
    annotations={"title": "Create Filter", "readOnlyHint": False},
)
async def jira_create_filter(
    ctx: Context,
    name: Annotated[str, Field(description="Filter display name")],
    jql: Annotated[str, Field(description="JQL query backing the filter")],
    description: Annotated[
        str | None,
        Field(description="Optional filter description.", default=None),
    ] = None,
    favourite: Annotated[
        bool,
        Field(
            description=(
                "Whether to mark the new filter as a favourite of the "
                "authenticated user. Default False."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """Create a new filter owned by the authenticated user (DC).

    Calls ``POST /rest/api/2/filter``. Returns a structured
    ``read_only_mode`` error without issuing any HTTP request when the
    server is running with ``READ_ONLY_MODE=true``.

    Returns:
        JSON string with the created filter object.
    """
    # 1. Read-only precheck (belt-and-suspenders; zero HTTP on reject).
    if err := dc_guards.check_read_only(_FILTERS_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)
    try:
        created = jira.create_filter(
            name=name,
            jql=jql,
            description=description,
            favourite=favourite,
        )
        return json.dumps(
            {"success": True, "filter": created},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error creating filter {name!r}: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="update_filter",
    tags=_FILTERS_WRITE_TAGS,
    annotations={"title": "Update Filter", "readOnlyHint": False},
)
async def jira_update_filter(
    ctx: Context,
    filter_id: Annotated[
        str,
        Field(description="Jira filter identifier (e.g., '10001')"),
    ],
    name: Annotated[
        str | None,
        Field(description="New filter display name.", default=None),
    ] = None,
    jql: Annotated[
        str | None,
        Field(description="New JQL query backing the filter.", default=None),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="New filter description.", default=None),
    ] = None,
    favourite: Annotated[
        bool | None,
        Field(
            description=(
                "Whether to mark the filter as a favourite of the "
                "authenticated user. Omit to leave unchanged."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Update an existing filter (DC).

    Calls ``PUT /rest/api/2/filter/{filter_id}`` with the supplied fields.
    Omitted parameters are not forwarded.

    Returns:
        JSON string with the updated filter object.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_FILTERS_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name
    if jql is not None:
        updates["jql"] = jql
    if description is not None:
        updates["description"] = description
    if favourite is not None:
        updates["favourite"] = favourite

    jira = await get_jira_fetcher(ctx)
    try:
        updated = jira.update_filter(filter_id, **updates)
        return json.dumps(
            {"success": True, "filter": updated},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error updating filter {filter_id}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="delete_own_filter",
    tags=_FILTERS_WRITE_TAGS,
    annotations={"title": "Delete Own Filter", "readOnlyHint": False},
)
async def jira_delete_own_filter(
    ctx: Context,
    filter_id: Annotated[
        str,
        Field(description="Jira filter identifier (e.g., '10001')"),
    ],
) -> str:
    """Delete a filter owned by the authenticated user (DC).

    Owner-scoped destructive operation. The tool resolves the target
    filter's owner via ``GET /rest/api/2/filter/{filter_id}`` first, then
    compares it to the authenticated user through
    :func:`mcp_atlassian.utils.dc_guards.require_owner`. When the names
    do not match, the tool returns a structured ``not_filter_owner``
    error and performs zero write HTTP calls against
    ``/rest/api/2/filter/{filter_id}`` (Requirements 15.3, 15.4, 46.1,
    46.2). Read-only mode short-circuits even before owner resolution.

    Returns:
        JSON string confirming deletion on success, or a structured
        error payload (``read_only_mode`` / ``not_filter_owner``) with
        ``success=False``.
    """
    # 1. Read-only precheck — blocks the whole flow, including the owner
    # read, so no outbound HTTP happens while the server is locked.
    if err := dc_guards.check_read_only(_FILTERS_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Resolve filter owner (read-only HTTP). Surface any failure
    # without attempting the DELETE.
    try:
        owner_name = jira.get_filter_owner_name(filter_id)
    except Exception as e:
        logger.error(
            f"Error resolving owner of filter {filter_id}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    # 3. Owner gate. ``require_owner`` returns ``not_owner``; map it to
    # the filter-specific ``not_filter_owner`` code so the agent can
    # distinguish filter ownership failures from other owner-scoped
    # tools. The mapping preserves the ``details`` payload so the
    # authenticated user and owner name stay observable.
    owner_err = dc_guards.require_owner(jira, owner_name)
    if owner_err is not None:
        mapped = dc_guards.StructuredError(
            error_code="not_filter_owner",
            message=(
                f"Authenticated user is not the owner of filter "
                f"{filter_id!r}; DELETE blocked."
            ),
            details={
                **owner_err.details,
                "filter_id": filter_id,
            },
        )
        return json.dumps(
            {"success": False, **mapped.to_dict()},
            indent=2,
            ensure_ascii=False,
        )

    # 4. Issue the DELETE only after the owner gate passes.
    try:
        jira.delete_filter(filter_id)
    except Exception as e:
        logger.error(
            f"Error deleting filter {filter_id}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "message": f"Filter {filter_id} has been deleted successfully.",
            "filter_id": filter_id,
        },
        indent=2,
        ensure_ascii=False,
    )

# =============================================================================
# Jira Dashboards (toolset:jira_dashboards) — atlassian-dc-tool-parity Req 16
# =============================================================================
#
# Dashboards are read-only and are not project-scoped, so these tools skip
# both ``check_read_only`` (by codebase convention for read tools) and
# ``check_project_filter``. No write tools are registered in this toolset
# (Requirement 16.2).


_DASHBOARDS_READ_TAGS = {"jira", "read", "toolset:jira_dashboards"}


@jira_mcp.tool(
    name="list_dashboards",
    tags=_DASHBOARDS_READ_TAGS,
    annotations={"title": "List Dashboards", "readOnlyHint": True},
)
async def jira_list_dashboards(
    ctx: Context,
    limit: Annotated[
        int,
        Field(
            description="Maximum number of dashboards to return. Default 25.",
            default=25,
            ge=1,
        ),
    ] = 25,
) -> str:
    """List dashboards visible to the authenticated user (DC).

    Calls ``GET /rest/api/2/dashboard``.

    Returns:
        JSON string with the list of dashboard objects.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        dashboards = jira.list_dashboards(limit=limit)
        return json.dumps(
            {
                "success": True,
                "count": len(dashboards),
                "dashboards": dashboards,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing dashboards: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="get_dashboard",
    tags=_DASHBOARDS_READ_TAGS,
    annotations={"title": "Get Dashboard", "readOnlyHint": True},
)
async def jira_get_dashboard(
    ctx: Context,
    dashboard_id: Annotated[
        str,
        Field(description="Jira dashboard identifier (e.g., '10000')"),
    ],
) -> str:
    """Fetch a single dashboard by id (DC).

    Calls ``GET /rest/api/2/dashboard/{dashboard_id}``.

    Returns:
        JSON string with the dashboard object.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        dashboard = jira.get_dashboard(dashboard_id)
        return json.dumps(
            {"success": True, "dashboard": dashboard},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error getting dashboard {dashboard_id}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="search_dashboards",
    tags=_DASHBOARDS_READ_TAGS,
    annotations={"title": "Search Dashboards", "readOnlyHint": True},
)
async def jira_search_dashboards(
    ctx: Context,
    dashboard_name: Annotated[
        str | None,
        Field(
            description="Substring to match against the dashboard name.",
            default=None,
        ),
    ] = None,
    account_id: Annotated[
        str | None,
        Field(
            description=(
                "Cloud account id of the dashboard owner (accepted by DC "
                "for forward-compat; generally unused on Server/DC)."
            ),
            default=None,
        ),
    ] = None,
    owner: Annotated[
        str | None,
        Field(
            description="Server/DC username of the dashboard owner.",
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum number of dashboards to return. Default 25.",
            default=25,
            ge=1,
        ),
    ] = 25,
) -> str:
    """Search dashboards by name and/or owner (DC).

    Calls ``GET /rest/api/2/dashboard/search``.

    Returns:
        JSON string with the matched dashboards.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        dashboards = jira.search_dashboards(
            dashboard_name=dashboard_name,
            account_id=account_id,
            owner=owner,
            limit=limit,
        )
        return json.dumps(
            {
                "success": True,
                "count": len(dashboards),
                "dashboards": dashboards,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching dashboards: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Jira Notifications (toolset:jira_notifications) — atlassian-dc-tool-parity Req 17
# =============================================================================
#
# ``jira_notify_issue`` is a broadcast-capable Write_Tool: it triggers Jira DC
# to send email to watchers / voters / reporter / assignee / explicit user and
# group recipients. Emails are not retractable, so the toolset is intentionally
# NOT part of ``DEFAULT_TOOLSETS`` (operators must opt in via
# ``TOOLSETS=...,jira_notifications,...``). See ``utils/toolsets.py`` where the
# toolset is defined with ``default=False`` (Req 17.2, 47.1).
#
# Prelude (zero HTTP on reject):
#   1. ``check_read_only`` — blocks every write tool when the server is
#      running with ``READ_ONLY_MODE=true``.
#   2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set, the
#      issue-key prefix is extracted and validated against the allow-list
#      before any outbound HTTP. Issue keys follow ``[A-Z][A-Z0-9_]+-\\d+``
#      (see :data:`ISSUE_KEY_PATTERN`), so the prefix is the project key.
#
# Success response embeds a reversible-receipt shape per Req 17.3 / 47.2:
# ``inverse_tool=None`` and ``inverse_args=None`` because emails cannot be
# un-sent; ``note="Email sends are not retractable"`` documents that
# non-retractable nature; ``recipient_scope`` summarizes the request (total
# ``recipient_count`` plus the input descriptor fields so the agent can audit
# who was targeted).


_NOTIFICATIONS_WRITE_TAGS = {"jira", "write", "toolset:jira_notifications"}


@jira_mcp.tool(
    name="notify_issue",
    tags=_NOTIFICATIONS_WRITE_TAGS,
    annotations={"title": "Notify Issue", "readOnlyHint": False},
)
async def jira_notify_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123') to notify about.",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    subject: Annotated[
        str,
        Field(description="Email subject line."),
    ],
    text_body: Annotated[
        str,
        Field(description="Plain-text body of the email."),
    ],
    html_body: Annotated[
        str | None,
        Field(
            description="Optional HTML body. Omit to send plain-text only.",
            default=None,
        ),
    ] = None,
    to_watchers: Annotated[
        bool,
        Field(description="Send to issue watchers. Default False.", default=False),
    ] = False,
    to_voters: Annotated[
        bool,
        Field(description="Send to issue voters. Default False.", default=False),
    ] = False,
    to_reporter: Annotated[
        bool,
        Field(description="Send to the issue reporter. Default False.", default=False),
    ] = False,
    to_assignee: Annotated[
        bool,
        Field(description="Send to the issue assignee. Default False.", default=False),
    ] = False,
    to_users: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of Server/DC usernames to notify directly."
            ),
            default=None,
        ),
    ] = None,
    to_groups: Annotated[
        list[str] | None,
        Field(
            description="Optional list of group names to notify.",
            default=None,
        ),
    ] = None,
    restrict_groups: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of group names; Jira filters out recipients "
                "outside these groups before sending."
            ),
            default=None,
        ),
    ] = None,
    restrict_permissions: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of permission keys (e.g., 'BROWSE'); Jira "
                "filters out recipients lacking these permissions before "
                "sending."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Send an email notification about a Jira issue (DC).

    Write_Tool for Requirement 17.1. Wraps
    ``POST /rest/api/2/issue/{issue_key}/notify`` via
    :meth:`NotificationsMixin.notify_issue`. This tool is broadcast-capable
    (Req 17.2, 47.1): emails are dispatched by Jira's mail subsystem and
    cannot be recalled, so the enclosing toolset
    ``toolset:jira_notifications`` is explicitly opt-in and NOT part of
    ``DEFAULT_TOOLSETS``.

    Prelude (zero HTTP on reject):

    1. ``check_read_only`` — blocks the send when the server is running
       with ``READ_ONLY_MODE=true``.
    2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.

    On success the response includes a reversible-receipt shape
    (Req 17.3, 47.2) with ``inverse_tool=None`` and ``inverse_args=None``
    (emails are not retractable), a human-readable
    ``note="Email sends are not retractable"``, and a
    ``recipient_scope`` summarizing the total ``recipient_count`` plus
    the input descriptor fields (``to_watchers``, ``to_voters``,
    ``to_reporter``, ``to_assignee``, ``to_users``, ``to_groups``,
    ``restrict_groups``, ``restrict_permissions``) so the agent can
    audit who was targeted.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key the notification is about.
        subject: Email subject line.
        text_body: Plain-text email body.
        html_body: Optional HTML email body.
        to_watchers: Include issue watchers.
        to_voters: Include issue voters.
        to_reporter: Include the issue reporter.
        to_assignee: Include the issue assignee.
        to_users: Usernames to notify directly.
        to_groups: Group names to notify.
        restrict_groups: Group names recipients must belong to.
        restrict_permissions: Permission keys recipients must have.

    Returns:
        JSON string with ``success=True``, ``issue_key``,
        ``recipient_count``, and ``receipt``; or a structured error
        payload (``read_only_mode`` / ``filtered_out``) with
        ``success=False`` on precheck rejection.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_NOTIFICATIONS_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Project filter precheck — extract the project key from the
    # validated issue-key prefix. ``ISSUE_KEY_PATTERN`` guarantees the
    # first segment (before the first ``-``) is the project key.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    # 3. Dispatch the notification. The mixin returns
    # ``{"recipient_count": int}`` summing the four booleans and the
    # lengths of the ``to_users`` / ``to_groups`` lists (Req 17.3).
    try:
        result = jira.notify_issue(
            issue_key,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            to_watchers=to_watchers,
            to_voters=to_voters,
            to_reporter=to_reporter,
            to_assignee=to_assignee,
            to_users=to_users,
            to_groups=to_groups,
            restrict_groups=restrict_groups,
            restrict_permissions=restrict_permissions,
        )
    except Exception as e:
        logger.error(
            f"Error notifying issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    recipient_count = int(result.get("recipient_count", 0))

    # 4. Build non-retractable reversible receipt (Req 17.3, 47.2).
    # Emails cannot be un-sent, so ``inverse_tool`` / ``inverse_args`` are
    # both ``None`` and ``note`` explains the non-retractable nature.
    # ``recipient_scope`` carries the total count plus the input
    # descriptor fields so the caller has a full audit trail of who was
    # targeted (empty lists are normalized to ``[]`` rather than ``None``
    # so the shape is uniform across invocations).
    recipient_scope: dict[str, Any] = {
        "recipient_count": recipient_count,
        "to_watchers": bool(to_watchers),
        "to_voters": bool(to_voters),
        "to_reporter": bool(to_reporter),
        "to_assignee": bool(to_assignee),
        "to_users": list(to_users) if to_users else [],
        "to_groups": list(to_groups) if to_groups else [],
        "restrict_groups": list(restrict_groups) if restrict_groups else [],
        "restrict_permissions": (
            list(restrict_permissions) if restrict_permissions else []
        ),
    }
    receipt = dc_guards.build_receipt(
        object_id=issue_key,
        inverse_tool=None,
        inverse_args=None,
        note="Email sends are not retractable",
        recipient_scope=recipient_scope,
    )

    return json.dumps(
        {
            "success": True,
            "issue_key": issue_key,
            "recipient_count": recipient_count,
            "receipt": receipt,
        },
        indent=2,
        ensure_ascii=False,
    )

# =============================================================================
# Jira Issue Votes (toolset:jira_issues) — atlassian-dc-tool-parity Req 18
# =============================================================================
#
# Three tools wrap :class:`VotesMixin` against
# ``/rest/api/2/issue/{issueIdOrKey}/votes`` so agents can read, add, and
# remove the authenticated user's vote on an issue. They live under the
# existing ``toolset:jira_issues`` group (already enabled by default) so
# operators do not need to opt in separately (Requirements 18.1, 18.2).
#
# Prelude (zero HTTP on reject), matching the ``jira_notify_issue``
# pattern from task 24.2:
#
#   1. ``check_read_only`` — write tools only; skipped for the read tool
#      since reads are always permitted.
#   2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set,
#      the issue-key prefix (project key) is validated against the
#      allow-list before any outbound HTTP. ``ISSUE_KEY_PATTERN``
#      guarantees the segment before the first ``-`` is the project key.
#
# Idempotency flags (Req 18.3, 18.4):
#
#   * ``jira_add_issue_vote`` returns ``already_voted=True`` when the
#     authenticated user had already voted before the POST. The mixin
#     captures the pre-state via GET so the flag is deterministic even
#     though Jira DC's POST response is empty.
#   * ``jira_remove_issue_vote`` returns ``not_voted=True`` when the
#     authenticated user had no active vote before the DELETE. The
#     mixin likewise captures the pre-state via GET and tolerates the
#     404 some DC versions return for a redundant DELETE.


_VOTES_READ_TAGS = {"jira", "read", "toolset:jira_issues"}
_VOTES_WRITE_TAGS = {"jira", "write", "toolset:jira_issues"}


@jira_mcp.tool(
    name="get_issue_votes",
    tags=_VOTES_READ_TAGS,
    annotations={"title": "Get Issue Votes", "readOnlyHint": True},
)
async def jira_get_issue_votes(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Get vote metadata for a Jira issue (DC).

    Read_Tool for Requirement 18.1. Wraps
    ``GET /rest/api/2/issue/{issue_key}/votes`` via
    :meth:`VotesMixin.get_issue_votes`. Returns the vote count, whether
    the authenticated user has voted, and the voter list (when the
    upstream response includes voter details).

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to read votes from.

    Returns:
        JSON string with ``success=True``, ``issue_key``, ``votes``
        (int), ``has_voted`` (bool), and ``voters`` (list); or a
        structured ``filtered_out`` error payload with ``success=False``
        on precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 1. Project filter precheck — extract the project key from the
    # validated issue-key prefix. ``ISSUE_KEY_PATTERN`` guarantees the
    # first segment (before the first ``-``) is the project key.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        result = jira.get_issue_votes(issue_key)
    except Exception as e:
        logger.error(
            f"Error getting votes for issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {"success": True, **result},
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="add_issue_vote",
    tags=_VOTES_WRITE_TAGS,
    annotations={"title": "Add Issue Vote", "readOnlyHint": False},
)
async def jira_add_issue_vote(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Cast the authenticated user's vote on a Jira issue (DC, idempotent).

    Write_Tool for Requirement 18.2. Wraps
    ``POST /rest/api/2/issue/{issue_key}/votes`` via
    :meth:`VotesMixin.add_issue_vote`. Idempotent from the caller's
    perspective: when the user has already voted, the tool still
    returns ``success=True`` and surfaces ``already_voted=True`` so the
    agent can treat the repeated call as a no-op (Requirement 18.3).

    Prelude (zero HTTP on reject):

    1. ``check_read_only`` — blocks the vote when the server is running
       with ``READ_ONLY_MODE=true``.
    2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to vote on.

    Returns:
        JSON string with ``success=True``, ``issue_key``,
        ``already_voted`` (bool), and ``votes`` (post-operation count);
        or a structured error payload (``read_only_mode`` /
        ``filtered_out``) with ``success=False`` on precheck rejection.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_VOTES_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Project filter precheck — extract the project key from the
    # validated issue-key prefix.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        result = jira.add_issue_vote(issue_key)
    except Exception as e:
        logger.error(
            f"Error adding vote for issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {"success": True, **result},
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="remove_issue_vote",
    tags=_VOTES_WRITE_TAGS,
    annotations={"title": "Remove Issue Vote", "readOnlyHint": False},
)
async def jira_remove_issue_vote(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Retract the authenticated user's vote on a Jira issue (DC, idempotent).

    Write_Tool for Requirement 18.2. Wraps
    ``DELETE /rest/api/2/issue/{issue_key}/votes`` via
    :meth:`VotesMixin.remove_issue_vote`. Idempotent from the caller's
    perspective: when the user had not voted, the tool still returns
    ``success=True`` and surfaces ``not_voted=True`` so the agent can
    treat the repeated call as a no-op (Requirement 18.4).

    Prelude (zero HTTP on reject):

    1. ``check_read_only`` — blocks the retraction when the server is
       running with ``READ_ONLY_MODE=true``.
    2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to retract the vote from.

    Returns:
        JSON string with ``success=True``, ``issue_key``, ``not_voted``
        (bool), and ``votes`` (post-operation count); or a structured
        error payload (``read_only_mode`` / ``filtered_out``) with
        ``success=False`` on precheck rejection.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_VOTES_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Project filter precheck — extract the project key from the
    # validated issue-key prefix.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        result = jira.remove_issue_vote(issue_key)
    except Exception as e:
        logger.error(
            f"Error removing vote for issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {"success": True, **result},
        indent=2,
        ensure_ascii=False,
    )

# =============================================================================
# Jira Lookups (toolset:jira_lookups) — atlassian-dc-tool-parity Req 19
# =============================================================================
#
# Four read-only tools wrap :class:`LookupsMixin` against the instance-wide
# lookup endpoints under ``/rest/api/2/`` (priority, resolution, status,
# issuetype). These endpoints are NOT project-scoped — they return reference
# data that applies to the whole Jira instance — so the server-layer
# ``check_project_filter`` precheck is intentionally skipped for this
# toolset (Req 19.1). By codebase convention read tools also skip
# ``check_read_only``, and no write tools are registered in
# ``toolset:jira_lookups`` (Req 19.2).
#
# The toolset is defined with ``default=False`` in ``utils/toolsets.py`` so
# operators opt in via ``TOOLSETS=...,jira_lookups,...``.


_LOOKUPS_READ_TAGS = {"jira", "read", "toolset:jira_lookups"}


@jira_mcp.tool(
    name="list_priorities",
    tags=_LOOKUPS_READ_TAGS,
    annotations={"title": "List Priorities", "readOnlyHint": True},
)
async def jira_list_priorities(ctx: Context) -> str:
    """List all available Jira issue priorities (DC).

    Read_Tool for Requirement 19.1. Wraps
    ``GET /rest/api/2/priority`` via
    :meth:`LookupsMixin.list_priorities`. The endpoint is instance-wide
    (not project-scoped), so no project-filter precheck runs.

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``priorities`` (list of priority objects as returned by Jira).
    """
    jira = await get_jira_fetcher(ctx)
    try:
        priorities = jira.list_priorities()
        return json.dumps(
            {
                "success": True,
                "count": len(priorities),
                "priorities": priorities,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing priorities: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="list_resolutions",
    tags=_LOOKUPS_READ_TAGS,
    annotations={"title": "List Resolutions", "readOnlyHint": True},
)
async def jira_list_resolutions(ctx: Context) -> str:
    """List all available Jira issue resolutions (DC).

    Read_Tool for Requirement 19.1. Wraps
    ``GET /rest/api/2/resolution`` via
    :meth:`LookupsMixin.list_resolutions`. The endpoint is instance-wide
    (not project-scoped), so no project-filter precheck runs.

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``resolutions`` (list of resolution objects as returned by Jira).
    """
    jira = await get_jira_fetcher(ctx)
    try:
        resolutions = jira.list_resolutions()
        return json.dumps(
            {
                "success": True,
                "count": len(resolutions),
                "resolutions": resolutions,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing resolutions: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="list_statuses",
    tags=_LOOKUPS_READ_TAGS,
    annotations={"title": "List Statuses", "readOnlyHint": True},
)
async def jira_list_statuses(ctx: Context) -> str:
    """List all available Jira issue statuses (DC).

    Read_Tool for Requirement 19.1. Wraps
    ``GET /rest/api/2/status`` via
    :meth:`LookupsMixin.list_statuses`. The endpoint is instance-wide
    (not project-scoped), so no project-filter precheck runs.

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``statuses`` (list of status objects as returned by Jira).
    """
    jira = await get_jira_fetcher(ctx)
    try:
        statuses = jira.list_statuses()
        return json.dumps(
            {
                "success": True,
                "count": len(statuses),
                "statuses": statuses,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing statuses: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@jira_mcp.tool(
    name="list_issue_types",
    tags=_LOOKUPS_READ_TAGS,
    annotations={"title": "List Issue Types", "readOnlyHint": True},
)
async def jira_list_issue_types(ctx: Context) -> str:
    """List all available Jira issue types (DC).

    Read_Tool for Requirement 19.1. Wraps
    ``GET /rest/api/2/issuetype`` via
    :meth:`LookupsMixin.list_issue_types`. The endpoint is instance-wide
    (not project-scoped), so no project-filter precheck runs.

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``issue_types`` (list of issue type objects as returned by Jira).
    """
    jira = await get_jira_fetcher(ctx)
    try:
        issue_types = jira.list_issue_types()
        return json.dumps(
            {
                "success": True,
                "count": len(issue_types),
                "issue_types": issue_types,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing issue types: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Jira My-Permissions (toolset:jira_permissions) — atlassian-dc-tool-parity Req 20
# =============================================================================
#
# One read-only tool wraps :class:`PermissionsMixin` against the per-issue
# my-permissions endpoint ``GET /rest/api/2/mypermissions``. The response
# is condensed to a ``{permission_key: bool}`` map so the agent can skip
# calls that would otherwise 403 (Req 20.1).
#
# Prelude (zero HTTP on reject):
#
#   1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set,
#      the issue-key prefix (project key) is validated against the
#      allow-list before any outbound HTTP. ``ISSUE_KEY_PATTERN``
#      guarantees the segment before the first ``-`` is the project key.
#
# By codebase convention read tools also skip ``check_read_only``, and no
# write tools are registered in ``toolset:jira_permissions`` (Req 20.2).
# The toolset is defined with ``default=False`` in ``utils/toolsets.py`` so
# operators opt in via ``TOOLSETS=...,jira_permissions,...``.


_PERMISSIONS_READ_TAGS = {"jira", "read", "toolset:jira_permissions"}


@jira_mcp.tool(
    name="get_my_issue_permissions",
    tags=_PERMISSIONS_READ_TAGS,
    annotations={"title": "Get My Issue Permissions", "readOnlyHint": True},
)
async def jira_get_my_issue_permissions(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
    permission_keys: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of permission keys to restrict the query to "
                "(e.g., ['BROWSE_PROJECTS', 'CREATE_ISSUES']). When omitted, "
                "Jira returns the full permission set visible to the user."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Get the authenticated user's permissions for a specific Jira issue (DC).

    Read_Tool for Requirement 20.1. Wraps
    ``GET /rest/api/2/mypermissions?issueKey={issue_key}`` via
    :meth:`PermissionsMixin.get_my_issue_permissions`. The raw Jira
    payload is condensed into a flat ``{permission_key: bool}`` mapping
    so an agent can quickly skip tools that would fail with 403 for the
    current user on this issue.

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to read permissions against.
        permission_keys: Optional list of permission keys to narrow the
            server-side response. When empty or ``None``, the full set
            is returned.

    Returns:
        JSON string with ``success=True``, ``issue_key``, ``count``
        (int, number of entries in the map), and ``permissions``
        (``{permission_key: bool}``); or a structured ``filtered_out``
        error payload with ``success=False`` on precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 1. Project filter precheck — extract the project key from the
    # validated issue-key prefix. ``ISSUE_KEY_PATTERN`` guarantees the
    # first segment (before the first ``-``) is the project key.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        permissions = jira.get_my_issue_permissions(
            issue_key, permission_keys=permission_keys
        )
    except Exception as e:
        logger.error(
            f"Error getting my-permissions for issue {issue_key}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "issue_key": issue_key,
            "count": len(permissions),
            "permissions": permissions,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Jira Myself (toolset:jira_users) — atlassian-dc-tool-parity Req 21
# =============================================================================
#
# One read-only tool wraps :class:`MyselfMixin` against
# ``GET /rest/api/2/myself`` to return the authenticated user's profile
# (key, displayName, emailAddress, timeZone, etc.). The mixin already
# strips top-level ``password`` / ``token`` / ``sessionCookie`` keys
# defensively; at the server layer we additionally run the response
# through ``redact_secrets`` as belt-and-suspenders so any nested
# credential-like field is replaced with the placeholder before the
# payload leaves the process (Req 21.2).
#
# Prelude (zero HTTP on reject):
#
# - The endpoint is not project-scoped, so ``check_project_filter`` does
#   not apply. Per codebase convention read tools skip
#   ``check_read_only`` as well.
#
# The ``toolset:jira_users`` toolset is defined with ``default=False`` in
# ``utils/toolsets.py`` so operators opt in explicitly via
# ``TOOLSETS=...,jira_users,...``.


_MYSELF_READ_TAGS = {"jira", "read", "toolset:jira_users"}


@jira_mcp.tool(
    name="get_myself",
    tags=_MYSELF_READ_TAGS,
    annotations={"title": "Get Myself", "readOnlyHint": True},
)
async def jira_get_myself(ctx: Context) -> str:
    """Return the authenticated user's profile from ``/rest/api/2/myself`` (DC).

    Read_Tool for Requirement 21.1. Wraps
    ``GET /rest/api/2/myself`` via :meth:`MyselfMixin.get_myself`. The
    response typically includes ``name``, ``key``, ``displayName``,
    ``emailAddress``, ``timeZone``, ``locale``, ``avatarUrls``, and
    group membership — useful as the entry point for "what's assigned
    to me?" workflows.

    Per Requirement 21.2, the response must not include ``password``,
    API token, PAT, or session cookie fields. Two layers of redaction
    are applied:

    1. :meth:`MyselfMixin.get_myself` strips top-level ``password`` /
       ``token`` / ``sessionCookie`` keys before returning.
    2. The server layer walks the full payload with
       :func:`redact_secrets`, replacing any nested secret-like key
       (``password``, ``token``, ``secret``, ``apiKey``, ``apitoken``,
       ``sessionCookie``, etc.) with the redacted placeholder.

    Args:
        ctx: The FastMCP context.

    Returns:
        JSON string with ``success=True`` and ``user`` (the redacted
        profile payload); or ``success=False`` with ``error`` on
        upstream failure.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        profile = jira.get_myself()
    except Exception as e:
        logger.error(f"Error fetching /myself: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    redacted = redact_secrets(profile)
    return json.dumps(
        {"success": True, "user": redacted},
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Jira Groups (toolset:jira_groups) — atlassian-dc-tool-parity Req 22
# =============================================================================
#
# Two read-only tools wrap :class:`GroupsMixin` against the instance-wide
# group endpoints ``GET /rest/api/2/user/groups`` and
# ``GET /rest/api/2/groups/picker``. These endpoints are NOT project-scoped
# — they return group metadata that applies to the whole Jira instance —
# so the server-layer ``check_project_filter`` precheck is intentionally
# skipped for this toolset.
#
# Per Requirement 22.2 / 22.3 this toolset registers ONLY read tools; no
# ``add_user_to_group``, ``remove_user_from_group`` or permission-grant
# Write_Tool is exposed. By codebase convention read tools also skip
# ``check_read_only``.
#
# The toolset is defined with ``default=False`` in ``utils/toolsets.py`` so
# operators opt in via ``TOOLSETS=...,jira_groups,...``.


_GROUPS_READ_TAGS = {"jira", "read", "toolset:jira_groups"}


@jira_mcp.tool(
    name="get_user_groups",
    tags=_GROUPS_READ_TAGS,
    annotations={"title": "Get User Groups", "readOnlyHint": True},
)
async def jira_get_user_groups(
    ctx: Context,
    username: Annotated[
        str | None,
        Field(
            description=(
                "DC username of the user (Server/DC lookup key). "
                "Exactly one of `username` or `account_id` must be provided."
            ),
            default=None,
        ),
    ] = None,
    account_id: Annotated[
        str | None,
        Field(
            description=(
                "Cloud accountId of the user (Cloud lookup key). "
                "Exactly one of `username` or `account_id` must be provided."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Return the groups a Jira user belongs to (DC).

    Read_Tool for Requirement 22.1. Wraps
    ``GET /rest/api/2/user/groups`` via
    :meth:`GroupsMixin.get_user_groups`. On Jira Data Center the lookup
    key is ``username``; on Jira Cloud the same endpoint accepts
    ``accountId``. Callers should supply whichever identifier is
    available for their deployment.

    The endpoint is instance-wide (not project-scoped), so no
    project-filter precheck runs. Per Req 22.2/22.3 this tool is
    strictly read-only — there is no companion add/remove/grant tool.

    Args:
        ctx: The FastMCP context.
        username: DC username of the user (Server/DC lookup key).
        account_id: Cloud accountId of the user (Cloud lookup key).

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``groups`` (list of group objects as returned by Jira); or
        ``success=False`` with ``error`` on invalid input.
    """
    if username is None and account_id is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Exactly one of `username` or `account_id` must be "
                    "provided."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )

    jira = await get_jira_fetcher(ctx)
    try:
        groups = jira.get_user_groups(username=username, account_id=account_id)
    except Exception as e:
        logger.error(f"Error getting user groups: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "count": len(groups),
            "groups": groups,
        },
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="list_groups",
    tags=_GROUPS_READ_TAGS,
    annotations={"title": "List Groups", "readOnlyHint": True},
)
async def jira_list_groups(
    ctx: Context,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Optional substring to match on the group name. "
                "When omitted, the server returns the default picker "
                "listing."
            ),
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of groups to return (forwarded as "
                "`maxResults`)."
            ),
            default=50,
            ge=1,
            le=1000,
        ),
    ] = 50,
) -> str:
    """Search groups on the Jira instance (DC).

    Read_Tool for Requirement 22.1. Wraps
    ``GET /rest/api/2/groups/picker`` via
    :meth:`GroupsMixin.list_groups`. The endpoint accepts a substring
    ``query`` on the group name and a ``maxResults`` page size.

    The endpoint is instance-wide (not project-scoped), so no
    project-filter precheck runs. Per Req 22.2/22.3 this tool is
    strictly read-only — no membership or permission-grant companion
    tools are registered.

    Args:
        ctx: The FastMCP context.
        query: Optional substring to match on the group name.
        limit: Maximum number of groups to return (default 50).

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``groups`` (list of group objects as returned by Jira); or
        ``success=False`` with ``error`` on upstream failure.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        groups = jira.list_groups(query=query, limit=limit)
    except Exception as e:
        logger.error(f"Error listing groups: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "count": len(groups),
            "groups": groups,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Jira @Mention Suggestions (toolset:jira_users) — atlassian-dc-tool-parity Req 23
# =============================================================================
#
# One read-only tool wraps :class:`MentionsMixin` against
# ``GET /rest/api/2/user/picker`` to return user-suggestion candidates
# suitable for an ``@mention`` in a comment. The picker accepts an
# optional ``issueKey`` that Jira DC uses to bias the suggestion order
# toward users who have interacted with the referenced issue.
#
# Prelude (zero HTTP on reject):
#
# - Per Requirement 23.2, an empty or whitespace-only ``query`` MUST
#   short-circuit to an empty list BEFORE any outbound HTTP call. This
#   contract is enforced inside :meth:`MentionsMixin.get_mention_suggestions`
#   so the server layer does not need to duplicate the guard.
# - The endpoint is not project-scoped, so ``check_project_filter`` does
#   not apply. Per codebase convention read tools skip
#   ``check_read_only`` as well.
#
# Registered under the existing ``toolset:jira_users`` so operators who
# opt in via ``TOOLSETS=...,jira_users,...`` pick it up alongside the
# ``jira_get_myself`` tool from Requirement 21.


_MENTIONS_READ_TAGS = {"jira", "read", "toolset:jira_users"}


@jira_mcp.tool(
    name="get_mention_suggestions",
    tags=_MENTIONS_READ_TAGS,
    annotations={"title": "Get Mention Suggestions", "readOnlyHint": True},
)
async def jira_get_mention_suggestions(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Free-text fragment to match against user name, "
                "display name, or email. An empty or whitespace-only "
                "value returns an empty list without contacting Jira."
            ),
        ),
    ],
    issue_key: Annotated[
        str | None,
        Field(
            description=(
                "Optional Jira issue key (e.g. `PROJ-123`) used to bias "
                "suggestion ordering toward users who have interacted "
                "with the referenced issue."
            ),
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of suggestions to return (forwarded as "
                "`maxResults`)."
            ),
            default=10,
            ge=1,
            le=1000,
        ),
    ] = 10,
) -> str:
    """Return @mention user suggestions matching ``query`` (DC).

    Read_Tool for Requirement 23.1. Wraps
    ``GET /rest/api/2/user/picker`` via
    :meth:`MentionsMixin.get_mention_suggestions`. Per Requirement 23.2
    an empty or whitespace-only ``query`` returns an empty list without
    issuing any HTTP request — this short-circuit is enforced inside
    the mixin.

    The endpoint is instance-wide (not project-scoped), so no
    project-filter precheck runs. Passing ``issue_key`` lets Jira DC
    rank users familiar with that issue higher in the returned list.

    Args:
        ctx: The FastMCP context.
        query: Free-text fragment to match against user identity. Empty
            or whitespace-only values short-circuit to an empty list.
        issue_key: Optional issue key used by Jira DC to bias ordering.
        limit: Maximum number of suggestions to return (default 10).

    Returns:
        JSON string with ``success=True``, ``count`` (int), and
        ``suggestions`` (list of user-suggestion objects as returned by
        the picker); or ``success=False`` with ``error`` on upstream
        failure.
    """
    jira = await get_jira_fetcher(ctx)
    try:
        suggestions = jira.get_mention_suggestions(
            query, issue_key=issue_key, limit=limit
        )
    except Exception as e:
        logger.error(f"Error fetching mention suggestions: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "count": len(suggestions),
            "suggestions": suggestions,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Jira Project Roles (toolset:jira_project_roles) — atlassian-dc-tool-parity Req 24
# =============================================================================
#
# Two read-only tools wrap :class:`ProjectRolesMixin` against the
# project-scoped role endpoints
# ``GET /rest/api/2/project/{projectIdOrKey}/role`` and
# ``GET /rest/api/2/project/{projectIdOrKey}/role/{roleId}``. The first
# enumerates the roles defined on a project as a ``{name: self_url}``
# map; the second resolves a specific role to its full payload including
# the ``actors`` array (users and groups assigned to the role).
#
# Prelude (zero HTTP on reject):
#
#   1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set,
#      the ``project_key`` argument is validated against the allow-list
#      before any outbound HTTP. Rejection is structured ``filtered_out``.
#
# By codebase convention read tools also skip ``check_read_only``, and
# per Requirement 24.2 no write tools are registered in
# ``toolset:jira_project_roles`` — role creation, deletion, and
# actor-assignment are intentionally out of scope for this toolset.
#
# The toolset is defined with ``default=False`` in ``utils/toolsets.py``
# so operators opt in via ``TOOLSETS=...,jira_project_roles,...``.


_PROJECT_ROLES_READ_TAGS = {"jira", "read", "toolset:jira_project_roles"}


@jira_mcp.tool(
    name="list_project_roles",
    tags=_PROJECT_ROLES_READ_TAGS,
    annotations={"title": "List Project Roles", "readOnlyHint": True},
)
async def jira_list_project_roles(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
) -> str:
    """List the roles defined for a Jira project (DC).

    Read_Tool for Requirement 24.1. Wraps
    ``GET /rest/api/2/project/{projectIdOrKey}/role`` via
    :meth:`ProjectRolesMixin.list_project_roles`. Jira returns a JSON
    object mapping each role name to the role's self URL, shaped like
    ``{"Administrators": "https://.../role/10002", ...}``. The role id
    embedded in the self URL is what :func:`jira_get_project_role_actors`
    expects as its ``role_id`` argument.

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, ``project_key`` must be in the allow-list. Rejection
       is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        project_key: Jira project key whose roles should be listed.

    Returns:
        JSON string with ``success=True``, ``project_key``, ``count``
        (int, number of roles), and ``roles`` (``{name: self_url}``);
        or a structured ``filtered_out`` error payload with
        ``success=False`` on precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 1. Project filter precheck.
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        roles = jira.list_project_roles(project_key)
    except Exception as e:
        logger.error(
            f"Error listing project roles for {project_key}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "project_key": project_key,
            "count": len(roles),
            "roles": roles,
        },
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="get_project_role_actors",
    tags=_PROJECT_ROLES_READ_TAGS,
    annotations={"title": "Get Project Role Actors", "readOnlyHint": True},
)
async def jira_get_project_role_actors(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    role_id: Annotated[
        str,
        Field(
            description=(
                "Role identifier as returned by `jira_list_project_roles` "
                "(parsed from the self URL, e.g., '10002')."
            ),
        ),
    ],
) -> str:
    """Get a Jira project role with its assigned actors (DC).

    Read_Tool for Requirement 24.1. Wraps
    ``GET /rest/api/2/project/{projectIdOrKey}/role/{roleId}`` via
    :meth:`ProjectRolesMixin.get_project_role_actors`. The response
    includes the role ``id``, ``name``, ``description``, ``self`` URL,
    and an ``actors`` array of user and group assignments.

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, ``project_key`` must be in the allow-list. Rejection
       is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        project_key: Jira project key whose role is being inspected.
        role_id: Role identifier (as returned by ``list_project_roles``).

    Returns:
        JSON string with ``success=True``, ``project_key``, ``role_id``,
        and ``role`` (the role payload including ``actors``); or a
        structured ``filtered_out`` error payload with ``success=False``
        on precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 1. Project filter precheck.
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        role = jira.get_project_role_actors(project_key, role_id)
    except Exception as e:
        logger.error(
            f"Error getting project role actors for "
            f"{project_key}/{role_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "project_key": project_key,
            "role_id": role_id,
            "role": role,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Jira Screens metadata (toolset:jira_screens) — atlassian-dc-tool-parity Req 25
# =============================================================================
#
# Two read-only tools wrap :class:`ScreensMixin` against
# ``GET /rest/api/2/issue/createmeta`` and
# ``GET /rest/api/2/issue/{key}/editmeta`` to return the fields visible
# on the create and edit screens, respectively. Both responses are
# flattened into a ``list[dict]`` so agents can iterate field metadata
# uniformly.
#
# Prelude (zero HTTP on reject):
#
#   1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set,
#      the ``project_key`` argument (create screen) or the issue-key
#      prefix (edit screen) is validated against the allow-list before
#      any outbound HTTP. Rejection is structured ``filtered_out``.
#
# By codebase convention read tools also skip ``check_read_only``, and
# per Requirement 25.3 no write tools are registered in
# ``toolset:jira_screens`` — screen and field-configuration mutations
# are intentionally out of scope for this toolset.
#
# The toolset is defined with ``default=False`` in ``utils/toolsets.py``
# so operators opt in via ``TOOLSETS=...,jira_screens,...``.


_SCREENS_READ_TAGS = {"jira", "read", "toolset:jira_screens"}


@jira_mcp.tool(
    name="get_issue_create_screen",
    tags=_SCREENS_READ_TAGS,
    annotations={"title": "Get Issue Create Screen", "readOnlyHint": True},
)
async def jira_get_issue_create_screen(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(
            description="Jira project key (e.g., 'PROJ', 'ACV2')",
            pattern=PROJECT_KEY_PATTERN,
        ),
    ],
    issue_type_id: Annotated[
        str | None,
        Field(
            description=(
                "Issue type identifier (numeric string as returned by "
                "`jira_list_issue_types`, e.g., '10001'). Either "
                "`issue_type_id` OR `issue_type_name` must be provided."
            ),
            default=None,
        ),
    ] = None,
    issue_type_name: Annotated[
        str | None,
        Field(
            description=(
                "Issue type display name (e.g., 'Task', 'Bug', 'Story'). "
                "Used only when `issue_type_id` is not provided. Either "
                "`issue_type_id` OR `issue_type_name` must be provided."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """List fields visible on the create screen for a project/issue type (DC).

    Read_Tool for Requirement 25.1. Wraps
    ``GET /rest/api/2/issue/createmeta`` with
    ``projectKeys={project_key}`` plus one of
    ``issuetypeIds={issue_type_id}`` or
    ``issuetypeNames={issue_type_name}``, and
    ``expand=projects.issuetypes.fields`` via
    :meth:`ScreensMixin.get_issue_create_screen`. The nested
    ``{"projects":[{"issuetypes":[{"fields":{...}}]}]}`` envelope is
    flattened into a ``list[dict]`` where each entry carries its
    ``fieldId`` plus the upstream metadata (``name``, ``required``,
    ``schema``, ``allowedValues``, ...).

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, ``project_key`` must be in the allow-list. Rejection
       is structured ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        project_key: Jira project key to inspect.
        issue_type_id: Optional issue type identifier; takes precedence
            when both id and name are supplied.
        issue_type_name: Optional issue type display name; used when
            ``issue_type_id`` is not provided.

    Returns:
        JSON string with ``success=True``, ``project_key``,
        ``issue_type_id``, ``issue_type_name``, ``count`` (int, number
        of fields), and ``fields`` (flattened list of field
        descriptors); or a structured ``filtered_out`` /
        ``invalid_argument`` error payload with ``success=False`` on
        precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 0. Argument validation — at least one identifier is required.
    if not issue_type_id and not issue_type_name:
        return json.dumps(
            {
                "success": False,
                "error_code": "invalid_argument",
                "message": (
                    "Either 'issue_type_id' or 'issue_type_name' must be "
                    "provided."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )

    # 1. Project filter precheck.
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        fields = jira.get_issue_create_screen(
            project_key,
            issue_type_id=issue_type_id,
            issue_type_name=issue_type_name,
        )
    except Exception as e:
        logger.error(
            f"Error getting create screen for project {project_key} "
            f"issue type id={issue_type_id!r} name={issue_type_name!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "project_key": project_key,
            "issue_type_id": issue_type_id,
            "issue_type_name": issue_type_name,
            "count": len(fields),
            "fields": fields,
        },
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="get_issue_edit_screen",
    tags=_SCREENS_READ_TAGS,
    annotations={"title": "Get Issue Edit Screen", "readOnlyHint": True},
)
async def jira_get_issue_edit_screen(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key (e.g., 'PROJ-123', 'ACV2-642')",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """List fields visible on the edit screen for a specific issue (DC).

    Read_Tool for Requirement 25.2. Wraps
    ``GET /rest/api/2/issue/{issueIdOrKey}/editmeta`` via
    :meth:`ScreensMixin.get_issue_edit_screen`. The upstream
    ``{"fields":{...}}`` envelope is flattened into a ``list[dict]``
    using the same shape as :func:`jira_get_issue_create_screen`, so
    both tools return a consistent payload.

    Prelude (zero HTTP on reject):

    1. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. ``ISSUE_KEY_PATTERN`` guarantees the segment before
       the first ``-`` is the project key. Rejection is structured
       ``filtered_out``.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key whose edit-screen fields are read.

    Returns:
        JSON string with ``success=True``, ``issue_key``, ``count``
        (int, number of fields), and ``fields`` (flattened list of
        field descriptors); or a structured ``filtered_out`` error
        payload with ``success=False`` on precheck rejection.
    """
    jira = await get_jira_fetcher(ctx)

    # 1. Project filter precheck — extract the project key from the
    # validated issue-key prefix. ``ISSUE_KEY_PATTERN`` guarantees the
    # first segment (before the first ``-``) is the project key.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    try:
        fields = jira.get_issue_edit_screen(issue_key)
    except Exception as e:
        logger.error(
            f"Error getting edit screen for issue {issue_key}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "issue_key": issue_key,
            "count": len(fields),
            "fields": fields,
        },
        indent=2,
        ensure_ascii=False,
    )

# =============================================================================
# Jira Archive / Restore (toolset:jira_archive) — atlassian-dc-tool-parity Req 26
# =============================================================================
#
# Two Write_Tools wrap :class:`ArchiveMixin` against
# ``PUT /rest/api/2/issue/{issue_key}/archive`` and
# ``PUT /rest/api/2/issue/{issue_key}/restore`` so agents can retire and
# bring back individual issues without resorting to permanent deletion
# (Requirements 26.1, 26.2).
#
# Both endpoints were introduced in Jira Data Center 9.4. The toolset is
# registered with ``default=False`` in :data:`JIRA_TOOLSETS`, so operators
# must opt in via ``TOOLSETS=...,jira_archive,...`` before the tools
# appear in the registered tool list.
#
# Prelude (zero HTTP on reject), matching the ``jira_notify_issue``
# pattern from task 24.2 and the ``jira_delete_own_filter`` pattern from
# task 22.3:
#
#   1. ``check_read_only`` — blocks every write tool when the server is
#      running with ``READ_ONLY_MODE=true``.
#   2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is set,
#      the issue-key prefix (project key) is validated against the
#      allow-list before any outbound HTTP. ``ISSUE_KEY_PATTERN``
#      guarantees the segment before the first ``-`` is the project key.
#   3. ``check_dc_version(required="9.4")`` — emits a structured
#      ``dc_version_too_old`` error identifying 9.4 as the minimum
#      version so callers on older instances get a deterministic
#      response without touching the archive endpoints (Req 26.3).
#
# ``jira_archive_issue`` returns a reversible receipt (Req 26.4, 47.2)
# referencing ``jira_restore_issue`` with the archived issue key so the
# agent can roll back the archive with a single tool call.
# ``jira_restore_issue`` itself is already the inverse of archive, so
# it does not build a receipt of its own — the success payload simply
# confirms the operation and echoes the issue key.


_ARCHIVE_WRITE_TAGS = {"jira", "write", "toolset:jira_archive"}


@jira_mcp.tool(
    name="archive_issue",
    tags=_ARCHIVE_WRITE_TAGS,
    annotations={"title": "Archive Issue", "readOnlyHint": False},
)
async def jira_archive_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key to archive (e.g., 'PROJ-123').",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Archive a single Jira issue (DC 9.4+).

    Write_Tool for Requirement 26.1. Wraps
    ``PUT /rest/api/2/issue/{issue_key}/archive`` via
    :meth:`ArchiveMixin.archive_issue`. The operation is reversible via
    the paired :func:`jira_restore_issue` tool.

    Prelude (zero HTTP on reject):

    1. ``check_read_only`` — blocks the archive when the server is
       running with ``READ_ONLY_MODE=true``.
    2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.
    3. ``check_dc_version(required="9.4")`` — archive is only
       available on Jira DC 9.4 or newer. Earlier versions return a
       structured ``dc_version_too_old`` error naming 9.4 as the
       minimum version (Req 26.3).

    On success the response includes a reversible-receipt shape
    (Req 26.4, 47.2) whose ``inverse_tool`` is ``"jira_restore_issue"``
    and ``inverse_args`` is ``{"issue_key": issue_key}``, so the agent
    can roll back the archive in a single call.

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to archive.

    Returns:
        JSON string with ``success=True``, ``issue_key``, and
        ``receipt``; or a structured error payload
        (``read_only_mode`` / ``filtered_out`` / ``dc_version_too_old``)
        with ``success=False`` on precheck rejection.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_ARCHIVE_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Project filter precheck — extract the project key from the
    # validated issue-key prefix. ``ISSUE_KEY_PATTERN`` guarantees the
    # first segment (before the first ``-``) is the project key.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    # 3. DC version gate — issue archive is DC 9.4+.
    if err := dc_guards.check_dc_version(jira, required="9.4"):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    # 4. Dispatch the archive. The mixin returns
    # ``{"archived": True, "issue_key": issue_key}`` which we embed into
    # the receipt shape below.
    try:
        jira.archive_issue(issue_key)
    except Exception as e:
        logger.error(
            f"Error archiving issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    # 5. Build reversible receipt (Req 26.4, 47.2). The inverse is the
    # paired ``jira_restore_issue`` tool with the same issue key.
    receipt = dc_guards.build_receipt(
        object_id=issue_key,
        inverse_tool="jira_restore_issue",
        inverse_args={"issue_key": issue_key},
        note=None,
    )

    return json.dumps(
        {
            "success": True,
            "issue_key": issue_key,
            "archived": True,
            "receipt": receipt,
        },
        indent=2,
        ensure_ascii=False,
    )


@jira_mcp.tool(
    name="restore_issue",
    tags=_ARCHIVE_WRITE_TAGS,
    annotations={"title": "Restore Issue", "readOnlyHint": False},
)
async def jira_restore_issue(
    ctx: Context,
    issue_key: Annotated[
        str,
        Field(
            description="Jira issue key to restore (e.g., 'PROJ-123').",
            pattern=ISSUE_KEY_PATTERN,
        ),
    ],
) -> str:
    """Restore a previously archived Jira issue (DC 9.4+).

    Write_Tool for Requirement 26.2. Wraps
    ``PUT /rest/api/2/issue/{issue_key}/restore`` via
    :meth:`ArchiveMixin.restore_issue`. This tool is itself the inverse
    of :func:`jira_archive_issue`, so the success payload simply
    confirms the operation and echoes the issue key rather than
    building a receipt of its own.

    Prelude (zero HTTP on reject):

    1. ``check_read_only`` — blocks the restore when the server is
       running with ``READ_ONLY_MODE=true``.
    2. ``check_project_filter`` — when ``JIRA_PROJECTS_FILTER`` is
       configured, the issue-key prefix (project key) must be in the
       allow-list. Rejection is structured ``filtered_out``.
    3. ``check_dc_version(required="9.4")`` — restore is only
       available on Jira DC 9.4 or newer. Earlier versions return a
       structured ``dc_version_too_old`` error naming 9.4 as the
       minimum version (Req 26.3).

    Args:
        ctx: The FastMCP context.
        issue_key: Jira issue key to restore.

    Returns:
        JSON string with ``success=True`` and ``issue_key``; or a
        structured error payload (``read_only_mode`` / ``filtered_out``
        / ``dc_version_too_old``) with ``success=False`` on precheck
        rejection.
    """
    # 1. Read-only precheck (zero HTTP on reject).
    if err := dc_guards.check_read_only(_ARCHIVE_WRITE_TAGS):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    jira = await get_jira_fetcher(ctx)

    # 2. Project filter precheck — extract the project key from the
    # validated issue-key prefix.
    project_key = issue_key.split("-", 1)[0]
    if err := dc_guards.check_project_filter(
        "jira", project_key, jira.config.projects_filter
    ):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    # 3. DC version gate — issue restore is DC 9.4+.
    if err := dc_guards.check_dc_version(jira, required="9.4"):
        return json.dumps(
            {"success": False, **err.to_dict()}, indent=2, ensure_ascii=False
        )

    # 4. Dispatch the restore.
    try:
        jira.restore_issue(issue_key)
    except Exception as e:
        logger.error(
            f"Error restoring issue {issue_key}: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "issue_key": issue_key,
            "restored": True,
        },
        indent=2,
        ensure_ascii=False,
    )
