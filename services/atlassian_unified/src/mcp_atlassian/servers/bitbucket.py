"""Bitbucket Data Center FastMCP server instance and tool definitions."""

import json
import logging
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from mcp_atlassian.servers.dependencies import get_bitbucket_fetcher
from mcp_atlassian.utils import dc_guards
from mcp_atlassian.utils.secret_redaction import redact_secrets

logger = logging.getLogger(__name__)

bitbucket_mcp = FastMCP(
    name="Bitbucket MCP Service",
    instructions="Provides tools for interacting with Atlassian Bitbucket Server/Data Center.",
)


# =============================================================================
# Repository & Project Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Projects", "readOnlyHint": True},
)
async def list_projects(
    ctx: Context,
    limit: Annotated[
        int,
        Field(description="Maximum number of projects to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List all Bitbucket projects accessible to the authenticated user.

    Returns:
        JSON string with list of projects including key, name, and description.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # Mode guard — Cloud removed the global workspaces-listing endpoint in
    # CHANGE-2770 (September 2025). On Cloud, `/2.0/workspaces` now returns
    # HTTP 410 Gone. Short-circuit with a structured error before hitting
    # the wire so Cloud users see an actionable message instead of a 410.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_projects"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        projects = bb.get_projects(limit=limit)
        simplified = [
            {
                "key": p.get("key"),
                "name": p.get("name"),
                "description": p.get("description", ""),
                "public": p.get("public", False),
            }
            for p in projects
        ]
        return json.dumps({"success": True, "count": len(simplified), "projects": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error listing projects: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Repositories", "readOnlyHint": True},
)
async def list_repos(
    ctx: Context,
    project_key: Annotated[
        str,
        Field(description="The project key (e.g., 'PROJ')"),
    ],
    limit: Annotated[
        int,
        Field(description="Maximum number of repositories to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List repositories in a Bitbucket project.

    Returns:
        JSON string with list of repositories including slug, name, and clone URLs.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        repos = bb.get_repositories(project_key, limit=limit)
        simplified = [
            {
                "slug": r.get("slug"),
                "name": r.get("name"),
                "state": r.get("state"),
                "forkable": r.get("forkable"),
                "project_key": r.get("project", {}).get("key"),
            }
            for r in repos
        ]
        return json.dumps({"success": True, "count": len(simplified), "repositories": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error listing repos: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Repository", "readOnlyHint": True},
)
async def get_repo(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
) -> str:
    """Get details of a specific Bitbucket repository.

    Returns:
        JSON string with repository details including clone URLs, default branch, etc.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        repo = bb.get_repository(project_key, repo_slug)
        return json.dumps({"success": True, "repository": repo}, indent=2)
    except Exception as e:
        logger.error(f"Error getting repo: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Search Repositories", "readOnlyHint": True},
)
async def search_repos(
    ctx: Context,
    query: Annotated[str, Field(description="Search query to find repositories by name")],
    limit: Annotated[
        int,
        Field(description="Maximum number of results. Default 25.", default=25),
    ] = 25,
) -> str:
    """Search for Bitbucket repositories by name.

    Returns:
        JSON string with matching repositories.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        repos = bb.search_repositories(query, limit=limit)
        simplified = [
            {
                "slug": r.get("slug"),
                "name": r.get("name"),
                "project_key": r.get("project", {}).get("key"),
            }
            for r in repos
        ]
        return json.dumps({"success": True, "count": len(simplified), "repositories": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error searching repos: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get File Content", "readOnlyHint": True},
)
async def get_file_content(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    file_path: Annotated[str, Field(description="Path to the file within the repository")],
    at: Annotated[
        str | None,
        Field(description="Optional branch name or commit hash. Defaults to default branch.", default=None),
    ] = None,
) -> str:
    """Get the content of a file from a Bitbucket repository.

    Returns:
        JSON string with file content.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        content = bb.get_file_content(project_key, repo_slug, file_path, at=at)
        return json.dumps({"success": True, "path": file_path, "content": content}, indent=2)
    except Exception as e:
        logger.error(f"Error getting file content: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Get Raw File Content", "readOnlyHint": True},
)
async def get_raw_file_content(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    file_path: Annotated[
        str,
        Field(description="Path to the file within the repository"),
    ],
    at: Annotated[
        str | None,
        Field(
            description="Optional branch name or commit hash. Defaults to the default branch.",
            default=None,
        ),
    ] = None,
) -> str:
    """Fetch raw file content via the /raw/ endpoint.

    Use this for large or non-text files where ``get_file_content`` (which
    paginates the browse endpoint) is impractical.

    Returns:
        JSON string with the decoded file content.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        content = bb.get_raw_file_content(project_key, repo_slug, file_path, at=at)
        return json.dumps(
            {"success": True, "path": file_path, "content": content}, indent=2
        )
    except Exception as e:
        logger.error(f"Error getting raw file content: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Browse Directory", "readOnlyHint": True},
)
async def browse_directory(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    path: Annotated[
        str,
        Field(
            description="Repository-relative directory path. Empty string lists the root.",
            default="",
        ),
    ] = "",
    at: Annotated[
        str | None,
        Field(
            description="Optional branch name or commit hash. Defaults to the default branch.",
            default=None,
        ),
    ] = None,
) -> str:
    """List the children of a directory in a repository.

    Returns:
        JSON string with directory entries.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        entries = bb.browse_directory(project_key, repo_slug, path=path, at=at)
        return json.dumps(
            {"success": True, "path": path, "count": len(entries), "entries": entries},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error browsing directory: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Pull Request Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "List Pull Requests", "readOnlyHint": True},
)
async def list_pull_requests(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    state: Annotated[
        str,
        Field(description="PR state filter: OPEN, MERGED, DECLINED, or ALL. Default OPEN.", default="OPEN"),
    ] = "OPEN",
    limit: Annotated[
        int,
        Field(description="Maximum number of PRs to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List pull requests for a Bitbucket repository.

    Returns:
        JSON string with list of pull requests.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        prs = bb.get_pull_requests(project_key, repo_slug, state=state, limit=limit)
        simplified = [
            {
                "id": pr.get("id"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "author": pr.get("author", {}).get("user", {}).get("displayName"),
                "from_branch": pr.get("fromRef", {}).get("displayId"),
                "to_branch": pr.get("toRef", {}).get("displayId"),
                "created_date": pr.get("createdDate"),
                "updated_date": pr.get("updatedDate"),
                "reviewers": [
                    {
                        "user": rev.get("user", {}).get("displayName"),
                        "status": rev.get("status"),
                    }
                    for rev in pr.get("reviewers", [])
                ],
            }
            for pr in prs
        ]
        return json.dumps({"success": True, "count": len(simplified), "pull_requests": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error listing PRs: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Pull Request", "readOnlyHint": True},
)
async def get_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Get details of a specific pull request.

    Returns:
        JSON string with full pull request details.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        pr = bb.get_pull_request(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, "pull_request": pr}, indent=2)
    except Exception as e:
        logger.error(f"Error getting PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Create Pull Request", "readOnlyHint": False},
)
async def create_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    title: Annotated[str, Field(description="Pull request title")],
    from_branch: Annotated[str, Field(description="Source branch name (e.g., 'feature/my-feature')")],
    to_branch: Annotated[str, Field(description="Target branch name (e.g., 'main' or 'develop')")],
    description: Annotated[
        str | None,
        Field(description="Optional pull request description", default=None),
    ] = None,
    reviewers: Annotated[
        str | None,
        Field(description="Optional comma-separated list of reviewer usernames", default=None),
    ] = None,
) -> str:
    """Create a new pull request in a Bitbucket repository.

    Returns:
        JSON string with the created pull request details.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        reviewer_list = (
            [r.strip() for r in reviewers.split(",") if r.strip()]
            if reviewers
            else None
        )
        pr = bb.create_pull_request(
            project_key,
            repo_slug,
            title=title,
            from_branch=from_branch,
            to_branch=to_branch,
            description=description,
            reviewers=reviewer_list,
        )
        return json.dumps({"success": True, "pull_request": pr}, indent=2)
    except Exception as e:
        logger.error(f"Error creating PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Merge Pull Request", "readOnlyHint": False},
)
async def merge_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    message: Annotated[
        str | None,
        Field(description="Optional merge commit message", default=None),
    ] = None,
    delete_source_branch: Annotated[
        bool,
        Field(description="Whether to delete the source branch after merge. Default false.", default=False),
    ] = False,
) -> str:
    """Merge a pull request.

    Returns:
        JSON string with the merged pull request details.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        current_pr = bb.get_pull_request(project_key, repo_slug, pr_id)
        version = current_pr.get("version")

        result = bb.merge_pull_request(
            project_key,
            repo_slug,
            pr_id,
            version=version,
            message=message,
            delete_source_branch=delete_source_branch,
        )
        return json.dumps({"success": True, "pull_request": result}, indent=2)
    except Exception as e:
        logger.error(f"Error merging PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Approve Pull Request", "readOnlyHint": False},
)
async def approve_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Approve a pull request.

    Returns:
        JSON string confirming approval.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.approve_pull_request(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, "result": result}, indent=2)
    except Exception as e:
        logger.error(f"Error approving PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Decline Pull Request", "readOnlyHint": False},
)
async def decline_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Decline a pull request.

    Returns:
        JSON string with the declined pull request details.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        current_pr = bb.get_pull_request(project_key, repo_slug, pr_id)
        version = current_pr.get("version")

        result = bb.decline_pull_request(project_key, repo_slug, pr_id, version=version)
        return json.dumps({"success": True, "pull_request": result}, indent=2)
    except Exception as e:
        logger.error(f"Error declining PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Comment on Pull Request", "readOnlyHint": False},
)
async def comment_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    text: Annotated[str, Field(description="Comment text")],
    parent_id: Annotated[
        int | None,
        Field(description="Optional parent comment ID for replies", default=None),
    ] = None,
) -> str:
    """Add a comment to a pull request.

    Returns:
        JSON string with the created comment.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.add_pull_request_comment(
            project_key, repo_slug, pr_id, text=text, parent_id=parent_id
        )
        return json.dumps({"success": True, "comment": result}, indent=2)
    except Exception as e:
        logger.error(f"Error commenting on PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Pull Request Diff", "readOnlyHint": True},
)
async def get_pull_request_diff(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    context_lines: Annotated[
        int,
        Field(description="Number of context lines around changes. Default 3.", default=3),
    ] = 3,
) -> str:
    """Get the diff content of a pull request.

    Returns:
        JSON string with the diff content.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        diff = bb.get_pull_request_diff(project_key, repo_slug, pr_id, context_lines=context_lines)
        return json.dumps({"success": True, "diff": diff}, indent=2)
    except Exception as e:
        logger.error(f"Error getting PR diff: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Pull Request Activities", "readOnlyHint": True},
)
async def get_pull_request_activities(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    limit: Annotated[
        int,
        Field(description="Maximum number of activities to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """Get activities (comments, approvals, merges) for a pull request.

    Returns:
        JSON string with list of activities.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        activities = bb.get_pull_request_activities(project_key, repo_slug, pr_id, limit=limit)
        return json.dumps({"success": True, "count": len(activities), "activities": activities}, indent=2)
    except Exception as e:
        logger.error(f"Error getting PR activities: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "List Pull Request Changes", "readOnlyHint": True},
)
async def list_pull_request_changes(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    limit: Annotated[
        int,
        Field(description="Maximum number of entries to return. Default 100.", default=100),
    ] = 100,
) -> str:
    """List the files changed in a pull request with their change type.

    Returns:
        JSON string with the changed file paths (ADD/MODIFY/DELETE/COPY/MOVE).
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        changes = bb.get_pull_request_changes(project_key, repo_slug, pr_id, limit=limit)
        simplified = [
            {
                "path": c.get("path", {}).get("toString")
                or c.get("path", {}).get("name"),
                "type": c.get("type"),
                "src_path": c.get("srcPath", {}).get("toString")
                if c.get("srcPath")
                else None,
            }
            for c in changes
        ]
        return json.dumps(
            {"success": True, "count": len(simplified), "changes": simplified},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing PR changes: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Pull Request File Diff", "readOnlyHint": True},
)
async def get_pull_request_file_diff(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    path: Annotated[
        str,
        Field(description="Repository-relative file path within the PR diff"),
    ],
    context_lines: Annotated[
        int,
        Field(description="Number of context lines around changes. Default 3.", default=3),
    ] = 3,
) -> str:
    """Return the diff for one specific file in a PR.

    Useful for surgically reviewing one file in a large PR.

    Returns:
        JSON string with the per-file unified diff.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        diff = bb.get_pr_file_diff(
            project_key, repo_slug, pr_id, path, context_lines=context_lines
        )
        return json.dumps(
            {"success": True, "path": path, "diff": diff}, indent=2
        )
    except Exception as e:
        logger.error(f"Error getting PR file diff: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Get Pull Request Merge Status", "readOnlyHint": True},
)
async def get_pull_request_merge_status(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Check whether a PR is mergeable; surfaces conflicts and vetoes.

    Returns:
        JSON string with ``canMerge``, ``conflicted``, and ``vetoes``.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        status = bb.get_pr_merge_status(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, "merge_status": status}, indent=2)
    except Exception as e:
        logger.error(f"Error getting PR merge status: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Update Pull Request", "readOnlyHint": False},
)
async def update_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    title: Annotated[
        str | None,
        Field(description="New PR title (omit to keep)", default=None),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="New PR description (omit to keep)", default=None),
    ] = None,
    reviewers: Annotated[
        str | None,
        Field(
            description=(
                "Comma-separated list of reviewer usernames. Replaces the "
                "current reviewer set when provided. Omit to keep."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Update a pull request's title, description, or reviewer list.

    Fetches the current version internally to satisfy optimistic locking.

    Returns:
        JSON string with the updated pull request.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        current = bb.get_pull_request(project_key, repo_slug, pr_id)
        version = current.get("version")
        reviewer_list: list[str] | None = None
        if reviewers is not None:
            reviewer_list = [r.strip() for r in reviewers.split(",") if r.strip()]

        result = bb.update_pull_request(
            project_key,
            repo_slug,
            pr_id,
            version=version,
            title=title,
            description=description,
            reviewers=reviewer_list,
        )
        return json.dumps({"success": True, "pull_request": result}, indent=2)
    except Exception as e:
        logger.error(f"Error updating PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Add Pull Request Reviewer", "readOnlyHint": False},
)
async def add_pull_request_reviewer(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    username: Annotated[str, Field(description="Reviewer's username (slug)")],
) -> str:
    """Add a reviewer to a pull request.

    Returns:
        JSON string with the created participant.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.add_pr_reviewer(project_key, repo_slug, pr_id, username)
        return json.dumps({"success": True, "participant": result}, indent=2)
    except Exception as e:
        logger.error(f"Error adding PR reviewer: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Remove Pull Request Reviewer", "readOnlyHint": False},
)
async def remove_pull_request_reviewer(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    username: Annotated[str, Field(description="Reviewer's username (slug) to remove")],
) -> str:
    """Remove a reviewer/participant from a pull request.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.remove_pr_reviewer(project_key, repo_slug, pr_id, username)
        return json.dumps({"success": ok}, indent=2)
    except Exception as e:
        logger.error(f"Error removing PR reviewer: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Unapprove Pull Request", "readOnlyHint": False},
)
async def unapprove_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Withdraw the current user's approval from a pull request.

    Returns:
        JSON string confirming the withdrawal.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.unapprove_pull_request(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, "result": result}, indent=2)
    except Exception as e:
        logger.error(f"Error unapproving PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Request Changes on Pull Request", "readOnlyHint": False},
)
async def request_changes_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    username: Annotated[
        str,
        Field(description="Reviewer username (slug) marking the PR as needing changes"),
    ],
) -> str:
    """Mark a PR as ``NEEDS_WORK`` for the given reviewer (DC-only concept).

    This is Bitbucket DC's analog of GitHub's "Request changes" — the PR
    is blocked from merging until the status is cleared.

    Returns:
        JSON string with the updated participant.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.request_changes_pull_request(
            project_key, repo_slug, pr_id, username
        )
        return json.dumps({"success": True, "participant": result}, indent=2)
    except Exception as e:
        logger.error(f"Error requesting changes on PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Clear Request-Changes on Pull Request", "readOnlyHint": False},
)
async def unrequest_changes_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    username: Annotated[
        str,
        Field(description="Reviewer username (slug) whose NEEDS_WORK status should be cleared"),
    ],
) -> str:
    """Clear a NEEDS_WORK status (returns the reviewer to UNAPPROVED).

    Returns:
        JSON string with the updated participant.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.unrequest_changes_pull_request(
            project_key, repo_slug, pr_id, username
        )
        return json.dumps({"success": True, "participant": result}, indent=2)
    except Exception as e:
        logger.error(f"Error clearing request-changes on PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Reopen Pull Request", "readOnlyHint": False},
)
async def reopen_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
) -> str:
    """Reopen a previously declined pull request.

    Returns:
        JSON string with the reopened pull request.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        current = bb.get_pull_request(project_key, repo_slug, pr_id)
        version = current.get("version")
        result = bb.reopen_pull_request(project_key, repo_slug, pr_id, version=version)
        return json.dumps({"success": True, "pull_request": result}, indent=2)
    except Exception as e:
        logger.error(f"Error reopening PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Add Inline Comment to Pull Request", "readOnlyHint": False},
)
async def add_pr_inline_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    file_path: Annotated[
        str,
        Field(description="Repository-relative path of the file the comment anchors to"),
    ],
    line: Annotated[
        int,
        Field(description="Line number in the file (1-based)"),
    ],
    text: Annotated[str, Field(description="Comment text")],
    line_type: Annotated[
        str,
        Field(
            description=(
                "Line type the anchor refers to: ADDED, REMOVED, or CONTEXT"
            ),
            default="CONTEXT",
        ),
    ] = "CONTEXT",
    file_type: Annotated[
        str,
        Field(
            description=(
                "Diff side the anchor refers to: FROM (source) or TO (destination)"
            ),
            default="TO",
        ),
    ] = "TO",
    parent_id: Annotated[
        int | None,
        Field(description="Optional parent comment ID for threaded replies", default=None),
    ] = None,
) -> str:
    """Add an inline comment anchored to a specific file:line in a PR diff.

    Use ``CONTEXT`` for unchanged lines, ``ADDED`` for new lines on the
    destination side, ``REMOVED`` for deleted lines on the source side. The
    ``file_type`` follows the Bitbucket DC convention: ``TO`` is the
    destination/source-PR side, ``FROM`` is the target/base side.

    Returns:
        JSON string with the created comment.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        anchor: dict[str, Any] = {
            "path": file_path,
            "line": line,
            "lineType": line_type,
            "fileType": file_type,
        }
        result = bb.add_pull_request_comment(
            project_key,
            repo_slug,
            pr_id,
            text=text,
            parent_id=parent_id,
            anchor=anchor,
        )
        return json.dumps({"success": True, "comment": result}, indent=2)
    except Exception as e:
        logger.error(f"Error adding inline PR comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Update Pull Request Comment", "readOnlyHint": False},
)
async def update_pull_request_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    comment_id: Annotated[int, Field(description="The comment ID to update")],
    version: Annotated[int, Field(description="Current version of the comment")],
    text: Annotated[str, Field(description="New comment text")],
) -> str:
    """Edit an existing PR comment (general or inline).

    Returns:
        JSON string with the updated comment.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        result = bb.update_pr_comment(
            project_key, repo_slug, pr_id, comment_id, version=version, text=text
        )
        return json.dumps({"success": True, "comment": result}, indent=2)
    except Exception as e:
        logger.error(f"Error updating PR comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Delete Pull Request Comment", "readOnlyHint": False},
)
async def delete_pull_request_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    comment_id: Annotated[int, Field(description="The comment ID to delete")],
    version: Annotated[int, Field(description="Current version of the comment")],
) -> str:
    """Delete a PR comment.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_pr_comment(
            project_key, repo_slug, pr_id, comment_id, version=version
        )
        return json.dumps({"success": ok}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting PR comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "List My Pull Requests", "readOnlyHint": True},
)
async def list_my_pull_requests(
    ctx: Context,
    role: Annotated[
        str,
        Field(
            description="Role filter: REVIEWER (default) or AUTHOR.",
            default="REVIEWER",
        ),
    ] = "REVIEWER",
    state: Annotated[
        str,
        Field(
            description="PR state filter: OPEN (default), MERGED, DECLINED, ALL.",
            default="OPEN",
        ),
    ] = "OPEN",
    limit: Annotated[
        int,
        Field(description="Maximum number of PRs to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List PRs from the authenticated user's dashboard.

    Use ``role=REVIEWER`` for "what should I review?" and ``role=AUTHOR``
    for "what have I opened?".

    Returns:
        JSON string with matching pull requests.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        prs = bb.list_my_pull_requests(role=role, state=state, limit=limit)
        simplified = [
            {
                "id": pr.get("id"),
                "title": pr.get("title"),
                "state": pr.get("state"),
                "author": pr.get("author", {}).get("user", {}).get("displayName"),
                "from_branch": pr.get("fromRef", {}).get("displayId"),
                "to_branch": pr.get("toRef", {}).get("displayId"),
                "project_key": pr.get("toRef", {})
                .get("repository", {})
                .get("project", {})
                .get("key"),
                "repo_slug": pr.get("toRef", {}).get("repository", {}).get("slug"),
            }
            for pr in prs
        ]
        return json.dumps(
            {"success": True, "count": len(simplified), "pull_requests": simplified},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing my PRs: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Branch Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_branches"},
    annotations={"title": "List Branches", "readOnlyHint": True},
)
async def list_branches(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    filter_text: Annotated[
        str | None,
        Field(description="Optional text to filter branches by name", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of branches to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List branches in a Bitbucket repository.

    Returns:
        JSON string with list of branches.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        branches = bb.get_branches(project_key, repo_slug, filter_text=filter_text, limit=limit)
        simplified = [
            {
                "id": b.get("id"),
                "displayId": b.get("displayId"),
                "latestCommit": b.get("latestCommit"),
                "isDefault": b.get("isDefault", False),
            }
            for b in branches
        ]
        return json.dumps({"success": True, "count": len(simplified), "branches": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error listing branches: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_branches"},
    annotations={"title": "Create Branch", "readOnlyHint": False},
)
async def create_branch(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    branch_name: Annotated[str, Field(description="Name for the new branch")],
    start_point: Annotated[str, Field(description="Commit hash or branch name to branch from")],
) -> str:
    """Create a new branch in a Bitbucket repository.

    Returns:
        JSON string with the created branch details.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        branch = bb.create_branch(project_key, repo_slug, branch_name, start_point)
        return json.dumps({"success": True, "branch": branch}, indent=2)
    except Exception as e:
        logger.error(f"Error creating branch: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_branches"},
    annotations={"title": "Delete Branch", "readOnlyHint": False},
)
async def delete_branch(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    branch_name: Annotated[
        str,
        Field(
            description=(
                "Name of the branch to delete (without the 'refs/heads/' prefix)"
            )
        ),
    ],
    end_point: Annotated[
        str | None,
        Field(
            description=(
                "Optional commit hash; the delete will fail if the branch's tip "
                "no longer matches this hash (safety check against races)."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Delete a branch.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_branch(project_key, repo_slug, branch_name, end_point=end_point)
        return json.dumps({"success": ok, "branch": branch_name}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting branch: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_branches"},
    annotations={"title": "List Tags", "readOnlyHint": True},
)
async def list_tags(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    filter_text: Annotated[
        str | None,
        Field(description="Optional text to filter tags by name", default=None),
    ] = None,
    order_by: Annotated[
        str | None,
        Field(
            description="Optional ordering: ALPHABETICAL or MODIFICATION",
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of tags to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List tags in a Bitbucket repository.

    Returns:
        JSON string with list of tags (id, displayId, latestCommit).
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        tags = bb.get_tags(
            project_key,
            repo_slug,
            filter_text=filter_text,
            order_by=order_by,
            limit=limit,
        )
        simplified = [
            {
                "id": t.get("id"),
                "displayId": t.get("displayId"),
                "type": t.get("type"),
                "latestCommit": t.get("latestCommit"),
            }
            for t in tags
        ]
        return json.dumps(
            {"success": True, "count": len(simplified), "tags": simplified},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing tags: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Commit Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "List Commits", "readOnlyHint": True},
)
async def list_commits(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    until: Annotated[
        str | None,
        Field(description="Branch name or commit hash to list commits up to. Defaults to default branch.", default=None),
    ] = None,
    since: Annotated[
        str | None,
        Field(description="Branch name or commit hash to list commits from (exclusive)", default=None),
    ] = None,
    path: Annotated[
        str | None,
        Field(description="Optional file path to filter commits affecting this path", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of commits to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List commits in a Bitbucket repository.

    Returns:
        JSON string with list of commits.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        commits = bb.get_commits(project_key, repo_slug, until=until, since=since, path=path, limit=limit)
        simplified = [
            {
                "id": c.get("id"),
                "displayId": c.get("displayId"),
                "message": c.get("message"),
                "author": c.get("author", {}).get("name"),
                "authorTimestamp": c.get("authorTimestamp"),
            }
            for c in commits
        ]
        return json.dumps({"success": True, "count": len(simplified), "commits": simplified}, indent=2)
    except Exception as e:
        logger.error(f"Error listing commits: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Get Diff", "readOnlyHint": True},
)
async def get_diff(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    since: Annotated[
        str | None,
        Field(description="Start ref (branch or commit hash) for comparison", default=None),
    ] = None,
    until: Annotated[
        str | None,
        Field(description="End ref (branch or commit hash) for comparison", default=None),
    ] = None,
    commit_id: Annotated[
        str | None,
        Field(description="Specific commit hash to get diff for (alternative to since/until)", default=None),
    ] = None,
    path: Annotated[
        str | None,
        Field(description="Optional file path to limit diff to", default=None),
    ] = None,
    context_lines: Annotated[
        int,
        Field(description="Number of context lines around changes. Default 3.", default=3),
    ] = 3,
) -> str:
    """Get a diff between two refs or for a specific commit.

    Returns:
        JSON string with the diff content.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        diff = bb.get_diff(
            project_key, repo_slug,
            commit_id=commit_id, since=since, until=until,
            path=path, context_lines=context_lines,
        )
        return json.dumps({"success": True, "diff": diff}, indent=2)
    except Exception as e:
        logger.error(f"Error getting diff: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Get Commit", "readOnlyHint": True},
)
async def get_commit(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash (full SHA-1)")],
) -> str:
    """Fetch a single commit by SHA — message, author, parents, etc.

    Returns:
        JSON string with the commit object.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        commit = bb.get_commit(project_key, repo_slug, commit_id)
        return json.dumps({"success": True, "commit": commit}, indent=2)
    except Exception as e:
        logger.error(f"Error getting commit: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Get Commit Changes", "readOnlyHint": True},
)
async def get_commit_changes(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash (full SHA-1)")],
    limit: Annotated[
        int,
        Field(description="Maximum number of entries to return. Default 100.", default=100),
    ] = 100,
) -> str:
    """List the files changed in a commit with their change type.

    Returns:
        JSON string with the commit's changed paths.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        changes = bb.get_commit_changes(project_key, repo_slug, commit_id, limit=limit)
        simplified = [
            {
                "path": c.get("path", {}).get("toString")
                or c.get("path", {}).get("name"),
                "type": c.get("type"),
            }
            for c in changes
        ]
        return json.dumps(
            {"success": True, "count": len(simplified), "changes": simplified},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error getting commit changes: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Search Code", "readOnlyHint": True},
)
async def search_code(
    ctx: Context,
    query: Annotated[str, Field(description="Search query string")],
    project_key: Annotated[
        str | None,
        Field(description="Optional project key to limit search scope", default=None),
    ] = None,
    repo_slug: Annotated[
        str | None,
        Field(description="Optional repository slug to limit search scope (requires project_key)", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of results. Default 25.", default=25),
    ] = 25,
) -> str:
    """Search for code across Bitbucket repositories.

    Note: Requires Bitbucket DC code search (Elasticsearch) to be enabled.

    Returns:
        JSON string with search results.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        results = bb.search_code(query, project_key=project_key, repo_slug=repo_slug, limit=limit)
        return json.dumps({"success": True, "count": len(results), "results": results}, indent=2)
    except Exception as e:
        logger.error(f"Error searching code: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# User Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_users"},
    annotations={"title": "Get User", "readOnlyHint": True},
)
async def get_user(
    ctx: Context,
    user_slug: Annotated[
        str,
        Field(description="The user's slug (typically the username)"),
    ],
) -> str:
    """Fetch a Bitbucket DC user's profile by slug.

    Returns:
        JSON string with user fields (name, displayName, emailAddress, ...).
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        user = bb.get_user(user_slug)
        return json.dumps({"success": True, "user": user}, indent=2)
    except Exception as e:
        logger.error(f"Error getting user: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_users"},
    annotations={"title": "Search Users", "readOnlyHint": True},
)
async def search_users(
    ctx: Context,
    filter_text: Annotated[
        str | None,
        Field(
            description=(
                "Substring matched against name/displayName/email. "
                "Omit to list all visible users (typically rate-limited)."
            ),
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum number of users to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """Search for Bitbucket DC users — useful when adding reviewers.

    Returns:
        JSON string with simplified user records.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_search_users"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        users = bb.search_users(filter_text=filter_text, limit=limit)
        simplified = [
            {
                "name": u.get("name"),
                "displayName": u.get("displayName"),
                "email": u.get("emailAddress"),
                "active": u.get("active"),
                "slug": u.get("slug"),
            }
            for u in users
        ]
        return json.dumps(
            {"success": True, "count": len(simplified), "users": simplified},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error searching users: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Build Status Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_builds"},
    annotations={"title": "Get Commit Build Status", "readOnlyHint": True},
)
async def get_commit_build_status(
    ctx: Context,
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    limit: Annotated[
        int,
        Field(description="Maximum number of build statuses. Default 25.", default=25),
    ] = 25,
) -> str:
    """List CI build statuses reported against a commit.

    Returns:
        JSON string with build statuses (state, key, name, url, ...).
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        statuses = bb.get_commit_build_status(commit_id, limit=limit)
        return json.dumps(
            {"success": True, "count": len(statuses), "statuses": statuses},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error getting build status: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_builds"},
    annotations={"title": "Post Commit Build Status", "readOnlyHint": False},
)
async def post_commit_build_status(
    ctx: Context,
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    state: Annotated[
        str,
        Field(description="Build state: SUCCESSFUL, INPROGRESS, or FAILED"),
    ],
    key: Annotated[
        str,
        Field(description="Stable identifier for the build (e.g. CI job key)"),
    ],
    name: Annotated[
        str | None,
        Field(description="Optional human-readable build name", default=None),
    ] = None,
    url: Annotated[
        str | None,
        Field(description="Optional URL pointing back to the CI run", default=None),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional short description", default=None),
    ] = None,
) -> str:
    """Publish a CI build status against a commit.

    Returns:
        JSON string confirming publication.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.post_commit_build_status(
            commit_id,
            state=state,
            key=key,
            name=name,
            url=url,
            description=description,
        )
        return json.dumps({"success": ok, "commit_id": commit_id, "state": state}, indent=2)
    except Exception as e:
        logger.error(f"Error posting build status: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

# =============================================================================
# Code Insights (Reports & Annotations) Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_code_insights"},
    annotations={"title": "List Code Insight Reports", "readOnlyHint": True},
)
async def list_code_insight_reports(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    limit: Annotated[
        int,
        Field(description="Maximum reports per page. Default 25.", default=25),
    ] = 25,
) -> str:
    """List Code Insights reports attached to a commit.

    Reports are produced by quality scanners (SonarQube, Snyk, Trivy,
    Checkmarx) and surfaced inline on pull requests. Use this to see
    which quality gates have results for the head commit of a PR.

    Returns:
        JSON string with the list of reports.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        reports = bb.list_code_insight_reports(project_key, repo_slug, commit_id, limit=limit)
        return json.dumps({"success": True, "count": len(reports), "reports": reports}, indent=2)
    except Exception as e:
        logger.error(f"Error listing Code Insights reports: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_code_insights"},
    annotations={"title": "Get Code Insight Report", "readOnlyHint": True},
)
async def get_code_insight_report(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Report identifier (e.g. 'sonarqube')")],
) -> str:
    """Fetch a single Code Insights report by its stable key.

    Returns:
        JSON string with the report object.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        report = bb.get_code_insight_report(project_key, repo_slug, commit_id, report_key)
        return json.dumps({"success": True, "report": report}, indent=2)
    except Exception as e:
        logger.error(f"Error getting Code Insights report: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_code_insights"},
    annotations={"title": "Create or Update Code Insight Report", "readOnlyHint": False},
)
async def create_or_update_code_insight_report(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Stable identifier of the report")],
    title: Annotated[str, Field(description="Human-readable report title")],
    details: Annotated[
        str | None,
        Field(description="Optional long-form description", default=None),
    ] = None,
    result: Annotated[
        str | None,
        Field(description="Optional outcome: PASS or FAIL", default=None),
    ] = None,
    reporter: Annotated[
        str | None,
        Field(description="Optional reporter name (tool that produced the data)", default=None),
    ] = None,
    link: Annotated[
        str | None,
        Field(description="Optional URL pointing back to the source system", default=None),
    ] = None,
    logo_url: Annotated[
        str | None,
        Field(description="Optional logo URL displayed in the UI", default=None),
    ] = None,
    data: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Optional list of summary rows. Each item: "
                "{'title': str, 'type': 'NUMBER|PERCENTAGE|TEXT|DURATION|LINK', 'value': Any}"
            ),
            default=None,
        ),
    ] = None,
    report_type: Annotated[
        str | None,
        Field(
            description=(
                "Optional report type. Cloud requires this field; defaults to 'TEST' "
                "when omitted. Common values: 'TEST', 'COVERAGE', 'BUG', 'SECURITY', "
                "'DEPENDENCY', 'PERFORMANCE', 'STYLE', 'OTHER'."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create or replace a Code Insights report (idempotent PUT).

    Posting the same ``report_key`` again overwrites the previous report.

    Returns:
        JSON string with the created/updated report.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        report = bb.create_or_update_code_insight_report(
            project_key,
            repo_slug,
            commit_id,
            report_key,
            title=title,
            details=details,
            result=result,
            reporter=reporter,
            link=link,
            logo_url=logo_url,
            data=data,
            report_type=report_type,
        )
        return json.dumps({"success": True, "report": report}, indent=2)
    except Exception as e:
        logger.error(f"Error upserting Code Insights report: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_code_insights"},
    annotations={"title": "Delete Code Insight Report", "readOnlyHint": False},
)
async def delete_code_insight_report(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Report identifier to delete")],
) -> str:
    """Delete a Code Insights report and all its annotations.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_code_insight_report(project_key, repo_slug, commit_id, report_key)
        return json.dumps({"success": ok, "report_key": report_key}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting Code Insights report: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_code_insights"},
    annotations={"title": "List Code Insight Annotations", "readOnlyHint": True},
)
async def list_code_insight_annotations(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Report identifier")],
    limit: Annotated[
        int,
        Field(description="Maximum annotations per page. Default 100.", default=100),
    ] = 100,
) -> str:
    """List annotations attached to a Code Insights report.

    Annotations are the file/line-level findings (bugs, vulnerabilities,
    code smells) that render inline in the PR diff view.

    Returns:
        JSON string with the list of annotations.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        anns = bb.list_code_insight_annotations(
            project_key, repo_slug, commit_id, report_key, limit=limit
        )
        return json.dumps({"success": True, "count": len(anns), "annotations": anns}, indent=2)
    except Exception as e:
        logger.error(f"Error listing Code Insights annotations: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_code_insights"},
    annotations={"title": "Bulk Create Code Insight Annotations", "readOnlyHint": False},
)
async def bulk_create_code_insight_annotations(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Parent report identifier")],
    annotations: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Up to 1000 annotation objects. Each must include at minimum "
                "'externalId', 'message', 'severity' (LOW|MEDIUM|HIGH). "
                "Optional: 'path', 'line', 'type' (VULNERABILITY|CODE_SMELL|BUG), 'link'."
            )
        ),
    ],
) -> str:
    """Create up to 1000 Code Insights annotations for a report in one call.

    Returns:
        JSON string confirming success.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.bulk_create_code_insight_annotations(
            project_key, repo_slug, commit_id, report_key, annotations=annotations
        )
        return json.dumps({"success": ok, "count": len(annotations)}, indent=2)
    except Exception as e:
        logger.error(f"Error creating Code Insights annotations: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_code_insights"},
    annotations={"title": "Delete Code Insight Annotations", "readOnlyHint": False},
)
async def delete_code_insight_annotations(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="Full commit SHA-1")],
    report_key: Annotated[str, Field(description="Report identifier")],
    external_ids: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional list of specific annotation external IDs to delete. "
                "Omit to delete every annotation on the report."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Delete specific annotations, or all annotations on a report.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_code_insight_annotations(
            project_key, repo_slug, commit_id, report_key, external_ids=external_ids
        )
        return json.dumps({"success": ok}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting Code Insights annotations: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Pull Request Task (Blocker Comment) Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "List Pull Request Tasks", "readOnlyHint": True},
)
async def list_pr_tasks(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    state: Annotated[
        str | None,
        Field(description="Optional filter: OPEN or RESOLVED", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum tasks per page. Default 100.", default=100),
    ] = 100,
) -> str:
    """List action-item tasks on a pull request (blocker comments).

    Returns:
        JSON string with the list of tasks including state, anchor (if inline),
        author and text.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        tasks = bb.list_pr_tasks(project_key, repo_slug, pr_id, state=state, limit=limit)
        return json.dumps({"success": True, "count": len(tasks), "tasks": tasks}, indent=2)
    except Exception as e:
        logger.error(f"Error listing PR tasks: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Get Pull Request Task", "readOnlyHint": True},
)
async def get_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    task_id: Annotated[int, Field(description="The task (blocker comment) ID")],
) -> str:
    """Fetch a single PR task by ID.

    Returns:
        JSON string with the task object.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        task = bb.get_pr_task(project_key, repo_slug, pr_id, task_id)
        return json.dumps({"success": True, "task": task}, indent=2)
    except Exception as e:
        logger.error(f"Error getting PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Create Pull Request Task", "readOnlyHint": False},
)
async def create_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    text: Annotated[str, Field(description="Task description")],
    file_path: Annotated[
        str | None,
        Field(description="Optional file path to anchor an inline task", default=None),
    ] = None,
    line: Annotated[
        int | None,
        Field(description="Optional line number (required if file_path is set)", default=None),
    ] = None,
    line_type: Annotated[
        str,
        Field(
            description="Anchor line type: ADDED, REMOVED, or CONTEXT",
            default="CONTEXT",
        ),
    ] = "CONTEXT",
    file_type: Annotated[
        str,
        Field(
            description="Anchor side: FROM (base) or TO (source). Default TO.",
            default="TO",
        ),
    ] = "TO",
    parent_id: Annotated[
        int | None,
        Field(description="Optional parent comment ID for threaded tasks", default=None),
    ] = None,
) -> str:
    """Create a PR task (BLOCKER-severity comment).

    Omit ``file_path`` for a top-level PR task. Provide ``file_path`` and
    ``line`` together to create an inline task anchored to the diff.

    Returns:
        JSON string with the created task.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        anchor: dict[str, Any] | None = None
        if file_path is not None:
            if line is None:
                return json.dumps(
                    {"success": False, "error": "'line' is required when 'file_path' is provided"}
                )
            anchor = {
                "path": file_path,
                "line": line,
                "lineType": line_type,
                "fileType": file_type,
            }
        task = bb.create_pr_task(
            project_key, repo_slug, pr_id, text=text, anchor=anchor, parent_id=parent_id
        )
        return json.dumps({"success": True, "task": task}, indent=2)
    except Exception as e:
        logger.error(f"Error creating PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Resolve Pull Request Task", "readOnlyHint": False},
)
async def resolve_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    task_id: Annotated[int, Field(description="Task ID")],
    version: Annotated[int, Field(description="Current task version (optimistic locking)")],
) -> str:
    """Mark a PR task as RESOLVED.

    Returns:
        JSON string with the updated task.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        task = bb.resolve_pr_task(project_key, repo_slug, pr_id, task_id, version=version)
        return json.dumps({"success": True, "task": task}, indent=2)
    except Exception as e:
        logger.error(f"Error resolving PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Reopen Pull Request Task", "readOnlyHint": False},
)
async def reopen_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    task_id: Annotated[int, Field(description="Task ID")],
    version: Annotated[int, Field(description="Current task version")],
) -> str:
    """Move a PR task back to OPEN.

    Returns:
        JSON string with the updated task.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        task = bb.reopen_pr_task(project_key, repo_slug, pr_id, task_id, version=version)
        return json.dumps({"success": True, "task": task}, indent=2)
    except Exception as e:
        logger.error(f"Error reopening PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Update Pull Request Task", "readOnlyHint": False},
)
async def update_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    task_id: Annotated[int, Field(description="Task ID")],
    version: Annotated[int, Field(description="Current task version")],
    text: Annotated[
        str | None,
        Field(description="New task text (omit to keep)", default=None),
    ] = None,
    state: Annotated[
        str | None,
        Field(description="New state: OPEN or RESOLVED (omit to keep)", default=None),
    ] = None,
) -> str:
    """Edit a PR task's text and/or state in one call.

    Returns:
        JSON string with the updated task.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        task = bb.update_pr_task(
            project_key, repo_slug, pr_id, task_id, version=version, text=text, state=state
        )
        return json.dumps({"success": True, "task": task}, indent=2)
    except Exception as e:
        logger.error(f"Error updating PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pr_tasks"},
    annotations={"title": "Delete Pull Request Task", "readOnlyHint": False},
)
async def delete_pr_task(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request ID")],
    task_id: Annotated[int, Field(description="Task ID")],
    version: Annotated[int, Field(description="Current task version")],
) -> str:
    """Delete a PR task.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_pr_task(project_key, repo_slug, pr_id, task_id, version=version)
        return json.dumps({"success": ok, "task_id": task_id}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting PR task: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# File Write Tools (create / update / delete via commit)
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Put File Content (Create/Update)", "readOnlyHint": False},
)
async def put_file_content(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    file_path: Annotated[str, Field(description="Repository-relative path of the file")],
    content: Annotated[str, Field(description="New file content (text)")],
    message: Annotated[str, Field(description="Commit message")],
    branch: Annotated[str, Field(description="Branch to commit to (plain name, no refs/heads/)")],
    source_commit_id: Annotated[
        str | None,
        Field(
            description=(
                "Current commit ID of the file when updating, used for "
                "optimistic concurrency. Omit for new files."
            ),
            default=None,
        ),
    ] = None,
    source_branch: Annotated[
        str | None,
        Field(
            description=(
                "Optional source branch to create 'branch' from when "
                "'branch' does not yet exist."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create or update a file with a single commit (DC 7.2+).

    Use the same call for both creation and edit. When editing, pass the
    ``source_commit_id`` returned by a prior read to avoid overwriting
    concurrent changes.

    Returns:
        JSON string with the resulting commit.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        commit = bb.put_file_content(
            project_key,
            repo_slug,
            file_path,
            content=content,
            message=message,
            branch=branch,
            source_commit_id=source_commit_id,
            source_branch=source_branch,
        )
        return json.dumps({"success": True, "commit": commit}, indent=2)
    except Exception as e:
        logger.error(f"Error writing file {file_path}: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Delete File", "readOnlyHint": False},
)
async def delete_file(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    file_path: Annotated[str, Field(description="Repository-relative path to delete")],
    message: Annotated[str, Field(description="Commit message")],
    branch: Annotated[str, Field(description="Branch to commit to")],
    source_commit_id: Annotated[
        str | None,
        Field(description="Optional current file commit ID for optimistic concurrency", default=None),
    ] = None,
) -> str:
    """Delete a file from a branch with a single commit.

    Scoped to a single file — use Git / UI for larger cleanups.

    Returns:
        JSON string with the resulting commit object.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        commit = bb.delete_file(
            project_key,
            repo_slug,
            file_path,
            message=message,
            branch=branch,
            source_commit_id=source_commit_id,
        )
        return json.dumps({"success": True, "commit": commit}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Compare Refs Tools
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Compare Commits Between Refs", "readOnlyHint": True},
)
async def compare_commits(
    ctx: Context,
    project_key: Annotated[str, Field(description="Target project key (host of 'to_ref')")],
    repo_slug: Annotated[str, Field(description="Target repository slug")],
    from_ref: Annotated[str, Field(description="Source ref (branch, tag or commit)")],
    to_ref: Annotated[str, Field(description="Target ref (branch, tag or commit)")],
    from_project_key: Annotated[
        str | None,
        Field(description="Optional source project key when comparing across forks", default=None),
    ] = None,
    from_repo_slug: Annotated[
        str | None,
        Field(description="Optional source repo slug when comparing across forks", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum commits per page. Default 100.", default=100),
    ] = 100,
) -> str:
    """List commits reachable from ``from_ref`` but not ``to_ref``.

    Great for answering release questions like "what's in release/1.2 that
    isn't in main yet?" — much cleaner than walking commit logs manually.

    Returns:
        JSON string with the commit list.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        from_repo = (
            (from_project_key, from_repo_slug)
            if from_project_key and from_repo_slug
            else None
        )
        commits = bb.compare_commits(
            project_key, repo_slug, from_ref, to_ref, from_repo=from_repo, limit=limit
        )
        return json.dumps({"success": True, "count": len(commits), "commits": commits}, indent=2)
    except Exception as e:
        logger.error(f"Error comparing commits: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Compare File Changes Between Refs", "readOnlyHint": True},
)
async def compare_changes(
    ctx: Context,
    project_key: Annotated[str, Field(description="Target project key")],
    repo_slug: Annotated[str, Field(description="Target repository slug")],
    from_ref: Annotated[str, Field(description="Source ref")],
    to_ref: Annotated[str, Field(description="Target ref")],
    from_project_key: Annotated[
        str | None,
        Field(description="Optional source project key for fork compare", default=None),
    ] = None,
    from_repo_slug: Annotated[
        str | None,
        Field(description="Optional source repo slug for fork compare", default=None),
    ] = None,
    limit: Annotated[
        int,
        Field(description="Maximum change entries per page. Default 100.", default=100),
    ] = 100,
) -> str:
    """List files that differ between two refs.

    Returns:
        JSON string with the list of change entries.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        from_repo = (
            (from_project_key, from_repo_slug)
            if from_project_key and from_repo_slug
            else None
        )
        changes = bb.compare_changes(
            project_key, repo_slug, from_ref, to_ref, from_repo=from_repo, limit=limit
        )
        return json.dumps({"success": True, "count": len(changes), "changes": changes}, indent=2)
    except Exception as e:
        logger.error(f"Error comparing changes: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "Compare Diff Between Refs", "readOnlyHint": True},
)
async def compare_diff(
    ctx: Context,
    project_key: Annotated[str, Field(description="Target project key")],
    repo_slug: Annotated[str, Field(description="Target repository slug")],
    from_ref: Annotated[str, Field(description="Source ref")],
    to_ref: Annotated[str, Field(description="Target ref")],
    path: Annotated[
        str | None,
        Field(description="Optional path filter to limit diff to a single file", default=None),
    ] = None,
    from_project_key: Annotated[
        str | None,
        Field(description="Optional source project key for fork compare", default=None),
    ] = None,
    from_repo_slug: Annotated[
        str | None,
        Field(description="Optional source repo slug for fork compare", default=None),
    ] = None,
    context_lines: Annotated[
        int,
        Field(description="Diff context lines around changes. Default 3.", default=3),
    ] = 3,
) -> str:
    """Return a unified diff between two refs (optionally scoped to one file).

    Returns:
        JSON string containing the diff text.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        from_repo = (
            (from_project_key, from_repo_slug)
            if from_project_key and from_repo_slug
            else None
        )
        diff = bb.compare_diff(
            project_key,
            repo_slug,
            from_ref,
            to_ref,
            path=path,
            from_repo=from_repo,
            context_lines=context_lines,
        )
        return json.dumps({"success": True, "diff": diff}, indent=2)
    except Exception as e:
        logger.error(f"Error comparing diff: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Tag Create/Delete + Branch Restrictions Read
# =============================================================================


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_branches"},
    annotations={"title": "Create Tag", "readOnlyHint": False},
)
async def create_tag(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    tag_name: Annotated[str, Field(description="Tag name (without refs/tags/ prefix)")],
    start_point: Annotated[
        str,
        Field(description="Commit hash, branch, or tag to place the tag at"),
    ],
    message: Annotated[
        str | None,
        Field(
            description="Optional annotation message (omit for lightweight tag)",
            default=None,
        ),
    ] = None,
) -> str:
    """Create a lightweight or annotated tag.

    Returns:
        JSON string with the created tag.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        tag = bb.create_tag(project_key, repo_slug, tag_name, start_point, message=message)
        return json.dumps({"success": True, "tag": tag}, indent=2)
    except Exception as e:
        logger.error(f"Error creating tag: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_branches"},
    annotations={"title": "Delete Tag", "readOnlyHint": False},
)
async def delete_tag(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    tag_name: Annotated[str, Field(description="Tag name to delete")],
) -> str:
    """Delete a tag from a repository.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        ok = bb.delete_tag(project_key, repo_slug, tag_name)
        return json.dumps({"success": ok, "tag_name": tag_name}, indent=2)
    except Exception as e:
        logger.error(f"Error deleting tag: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_branches"},
    annotations={"title": "List Branch Restrictions", "readOnlyHint": True},
)
async def list_branch_restrictions(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    limit: Annotated[
        int,
        Field(description="Maximum restrictions per page. Default 100.", default=100),
    ] = 100,
) -> str:
    """List branch permission restrictions (read-only).

    Modifying or deleting restrictions is intentionally NOT exposed — it
    can lock contributors out of a repository and should be done by
    admins through the UI.

    Returns:
        JSON string with the list of branch restriction objects.
    """
    bb = await get_bitbucket_fetcher(ctx)
    try:
        restrictions = bb.list_branch_restrictions(project_key, repo_slug, limit=limit)
        return json.dumps(
            {"success": True, "count": len(restrictions), "restrictions": restrictions},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing branch restrictions: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Default Reviewers Tools (toolset: bitbucket_default_reviewers)
# =============================================================================
#
# Manage default-reviewer rules (aka "conditions") on a repository. Each tool
# runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#
# These tools wrap the ``/rest/default-reviewers/1.0/projects/{k}/repos/{r}/
# conditions`` endpoints exposed by the Default Reviewers plugin that ships
# with Bitbucket Data Center, so there is no DC version gate.


_DEFAULT_REVIEWERS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_default_reviewers",
}
_DEFAULT_REVIEWERS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_default_reviewers",
}


def _parse_matcher(value: str | dict[str, Any], *, field_name: str) -> dict[str, Any]:
    """Normalize a ref-matcher argument into the dict shape the DC API expects.

    Accepts either a dict (already-parsed, typically when called
    programmatically or via a JSON-object tool argument) or a JSON-encoded
    string. Raises ``ValueError`` on malformed input so the caller can return
    a structured error to the agent without issuing any HTTP request.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a JSON object or JSON-encoded string, "
            f"got {type(value).__name__}"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{field_name} is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{field_name} must decode to a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _parse_reviewers(value: str | list[Any]) -> list[dict[str, Any]]:
    """Normalize the ``reviewers`` argument into a list of user-ref dicts.

    Accepts either a list (already-parsed) or a JSON-encoded string. Each
    element must itself be a dict shaped as ``{"id": 42}`` or
    ``{"name": "jdoe"}`` per Bitbucket DC's user-reference schema.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"reviewers is not valid JSON: {exc.msg}") from exc
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise ValueError(
            f"reviewers must be a JSON array, got {type(parsed).__name__}"
        )
    normalized: list[dict[str, Any]] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValueError(
                f"reviewers[{i}] must be a JSON object "
                f"(e.g. {{\"name\": \"jdoe\"}}), got {type(entry).__name__}"
            )
        normalized.append(entry)
    return normalized


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_default_reviewers"},
    annotations={"title": "List Default Reviewer Rules", "readOnlyHint": True},
)
async def list_default_reviewers(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
) -> str:
    """List default-reviewer rules ("conditions") on a repository.

    Wraps ``GET /rest/default-reviewers/1.0/projects/{k}/repos/{r}/conditions``.
    Each returned condition pairs a source/target ref matcher with a list of
    reviewers and a required-approvals count.

    Returns:
        JSON string with the list of condition objects.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_DEFAULT_REVIEWERS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_default_reviewers"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        rules = bb.list_default_reviewers(project_key, repo_slug)
        return json.dumps(
            {"success": True, "count": len(rules), "rules": rules}, indent=2
        )
    except Exception as e:
        logger.error(f"Error listing default reviewer rules: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_default_reviewers"},
    annotations={"title": "Get Default Reviewer Rule", "readOnlyHint": True},
)
async def get_default_reviewer_rule(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    rule_id: Annotated[int, Field(description="The numeric condition (rule) id")],
) -> str:
    """Fetch a single default-reviewer rule by id.

    Wraps ``GET /rest/default-reviewers/1.0/projects/{k}/repos/{r}/conditions/{id}``.

    Returns:
        JSON string with the condition object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_DEFAULT_REVIEWERS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_get_default_reviewer_rule"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        rule = bb.get_default_reviewer_rule(project_key, repo_slug, rule_id)
        return json.dumps({"success": True, "rule": rule}, indent=2)
    except Exception as e:
        logger.error(f"Error getting default reviewer rule: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_default_reviewers"},
    annotations={"title": "Create Default Reviewer Rule", "readOnlyHint": False},
)
async def create_default_reviewer_rule(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    source_matcher: Annotated[
        str,
        Field(
            description=(
                "Source ref matcher as a JSON object string, e.g. "
                "'{\"id\": \"refs/heads/feature/*\", \"type\": {\"id\": \"PATTERN\"}}' or "
                "'{\"id\": \"ANY_REF_MATCHER_ID\", \"type\": {\"id\": \"ANY_REF\"}}'."
            )
        ),
    ],
    target_matcher: Annotated[
        str,
        Field(
            description=(
                "Target ref matcher as a JSON object string, shaped like "
                "'{\"id\": \"refs/heads/main\", \"type\": {\"id\": \"BRANCH\"}}'."
            )
        ),
    ],
    reviewers: Annotated[
        str,
        Field(
            description=(
                "JSON array of reviewer user references, e.g. "
                "'[{\"name\": \"jdoe\"}, {\"id\": 42}]'."
            )
        ),
    ],
    required_approvals: Annotated[
        int,
        Field(description="Number of approvals this rule requires", ge=0),
    ],
) -> str:
    """Create a default-reviewer rule on a repository.

    Wraps ``POST /rest/default-reviewers/1.0/projects/{k}/repos/{r}/conditions``.

    Returns:
        JSON string with the created condition object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_DEFAULT_REVIEWERS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_create_default_reviewer_rule"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        parsed_source = _parse_matcher(source_matcher, field_name="source_matcher")
        parsed_target = _parse_matcher(target_matcher, field_name="target_matcher")
        parsed_reviewers = _parse_reviewers(reviewers)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    try:
        rule = bb.create_default_reviewer_rule(
            project_key,
            repo_slug,
            source_matcher=parsed_source,
            target_matcher=parsed_target,
            reviewers=parsed_reviewers,
            required_approvals=required_approvals,
        )
        return json.dumps({"success": True, "rule": rule}, indent=2)
    except Exception as e:
        logger.error(f"Error creating default reviewer rule: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_default_reviewers"},
    annotations={"title": "Update Default Reviewer Rule", "readOnlyHint": False},
)
async def update_default_reviewer_rule(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    rule_id: Annotated[int, Field(description="The numeric condition (rule) id")],
    source_matcher: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement source ref matcher as a JSON object string; "
                "omit to leave the current matcher unchanged."
            ),
            default=None,
        ),
    ] = None,
    target_matcher: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement target ref matcher as a JSON object string."
            ),
            default=None,
        ),
    ] = None,
    reviewers: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement reviewers list as a JSON array string, "
                "e.g. '[{\"name\": \"jdoe\"}]'."
            ),
            default=None,
        ),
    ] = None,
    required_approvals: Annotated[
        int | None,
        Field(
            description="Optional replacement required-approvals count",
            default=None,
            ge=0,
        ),
    ] = None,
) -> str:
    """Update an existing default-reviewer rule.

    Wraps ``PUT /rest/default-reviewers/1.0/projects/{k}/repos/{r}/conditions/{id}``.
    Only the fields supplied in the call are forwarded in the PUT body; omitted
    fields leave the corresponding condition attribute untouched.

    Returns:
        JSON string with the updated condition object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_DEFAULT_REVIEWERS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_update_default_reviewer_rule"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    fields: dict[str, Any] = {}
    try:
        if source_matcher is not None:
            fields["sourceMatcher"] = _parse_matcher(
                source_matcher, field_name="source_matcher"
            )
        if target_matcher is not None:
            fields["targetMatcher"] = _parse_matcher(
                target_matcher, field_name="target_matcher"
            )
        if reviewers is not None:
            fields["reviewers"] = _parse_reviewers(reviewers)
        if required_approvals is not None:
            fields["requiredApprovals"] = required_approvals
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    if not fields:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "At least one of source_matcher, target_matcher, reviewers, "
                    "or required_approvals must be supplied."
                ),
            }
        )

    try:
        rule = bb.update_default_reviewer_rule(
            project_key, repo_slug, rule_id, **fields
        )
        return json.dumps({"success": True, "rule": rule}, indent=2)
    except Exception as e:
        logger.error(f"Error updating default reviewer rule: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_default_reviewers"},
    annotations={"title": "Delete Default Reviewer Rule", "readOnlyHint": False},
)
async def delete_default_reviewer_rule(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    rule_id: Annotated[int, Field(description="The numeric condition (rule) id")],
) -> str:
    """Delete a default-reviewer rule.

    Wraps ``DELETE /rest/default-reviewers/1.0/projects/{k}/repos/{r}/conditions/{id}``.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_DEFAULT_REVIEWERS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_delete_default_reviewer_rule"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.delete_default_reviewer_rule(project_key, repo_slug, rule_id)
        return json.dumps(
            {"success": True, "rule_id": rule_id, "deleted": True}, indent=2
        )
    except Exception as e:
        logger.error(f"Error deleting default reviewer rule: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

# =============================================================================
# Webhooks Tools (toolset: bitbucket_webhooks)
# =============================================================================
#
# Manage repository webhooks via ``/rest/api/latest/projects/{k}/repos/{r}/
# webhooks``. Each tool runs the uniform pre-HTTP guard prelude defined in
# the ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#   3. ``check_dc_version(required="5.4")`` — repository webhooks were
#      introduced in Bitbucket DC 5.4; older instances return a structured
#      ``dc_version_too_old`` error without issuing any HTTP call.
#
# Post-processing:
#
# - Every returned payload (list, get, create, update) is walked through
#   ``redact_secrets`` so the HMAC ``secret`` stored under
#   ``configuration.secret`` is never echoed back to the agent, even when
#   Bitbucket reflects it in the response body.
# - ``bitbucket_create_webhook`` additionally returns a ``build_receipt``
#   whose inverse is the matching ``bitbucket_delete_webhook`` invocation
#   so the agent can undo the creation in one call. The ``secret`` input
#   is forwarded to Bitbucket but is deliberately kept out of the receipt
#   ``recipient_scope`` (only ``url`` and ``events`` describe the
#   broadcast target).


_WEBHOOKS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_webhooks",
}
_WEBHOOKS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_webhooks",
}


def _parse_webhook_events(value: str | list[Any]) -> list[str]:
    """Normalize the ``events`` argument into a list of event-key strings.

    Accepts either a list (already-parsed, typically when called
    programmatically) or a JSON-encoded string. Each element must be a
    non-empty string event key (for example ``"repo:refs_changed"``).
    Raises ``ValueError`` on malformed input so the caller can return a
    structured error to the agent without issuing any HTTP request.
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"events is not valid JSON: {exc.msg}") from exc
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise ValueError(
            f"events must be a JSON array, got {type(parsed).__name__}"
        )
    normalized: list[str] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"events[{i}] must be a non-empty string event key "
                f"(e.g. 'repo:refs_changed'), got {entry!r}"
            )
        normalized.append(entry)
    if not normalized:
        raise ValueError("events must contain at least one event key")
    return normalized


def _parse_webhook_configuration(
    value: str | dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Normalize the ``configuration`` update argument into a dict.

    Accepts ``None`` (configuration left unchanged), a dict (already
    parsed), or a JSON-encoded string. Used by ``bitbucket_update_webhook``
    where the caller may want to replace the webhook configuration
    wholesale (for example to rotate the HMAC secret).
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"configuration must be a JSON object or JSON-encoded string, "
            f"got {type(value).__name__}"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"configuration is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"configuration must decode to a JSON object, "
            f"got {type(parsed).__name__}"
        )
    return parsed


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_webhooks"},
    annotations={"title": "List Webhooks", "readOnlyHint": True},
)
async def list_webhooks(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of webhooks to return. Default 25.",
            default=25,
            ge=1,
        ),
    ] = 25,
) -> str:
    """List repository webhooks.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/webhooks``. Any
    ``configuration.secret`` values present in the returned payload are
    replaced with the literal string ``"[REDACTED]"`` before being
    returned to the agent.

    Returns:
        JSON string with the list of webhook objects.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_WEBHOOKS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. DC version gate — repository webhooks are DC 5.4+.
    if err := dc_guards.check_dc_version(bb, required="5.4"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        webhooks = bb.list_webhooks(project_key, repo_slug, limit=limit)
        redacted = redact_secrets(webhooks)
        return json.dumps(
            {"success": True, "count": len(redacted), "webhooks": redacted},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing webhooks: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_webhooks"},
    annotations={"title": "Get Webhook", "readOnlyHint": True},
)
async def get_webhook(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    webhook_id: Annotated[int, Field(description="The numeric webhook id")],
) -> str:
    """Fetch a single webhook by id.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/webhooks/{id}``. The
    ``configuration.secret`` field, if present, is replaced with
    ``"[REDACTED]"`` before returning.

    Returns:
        JSON string with the webhook object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_WEBHOOKS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_dc_version(bb, required="5.4"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        webhook = bb.get_webhook(project_key, repo_slug, webhook_id)
        redacted = redact_secrets(webhook)
        return json.dumps({"success": True, "webhook": redacted}, indent=2)
    except Exception as e:
        logger.error(f"Error getting webhook: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_webhooks"},
    annotations={"title": "Create Webhook", "readOnlyHint": False},
)
async def create_webhook(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    name: Annotated[str, Field(description="Human-readable webhook name")],
    url: Annotated[
        str,
        Field(description="Target URL Bitbucket will POST webhook events to"),
    ],
    events: Annotated[
        str,
        Field(
            description=(
                "JSON array of event keys, e.g. "
                "'[\"repo:refs_changed\", \"pr:opened\"]'."
            )
        ),
    ],
    secret: Annotated[
        str | None,
        Field(
            description=(
                "Optional HMAC secret; forwarded to Bitbucket in the "
                "request body and never echoed back in the response."
            ),
            default=None,
        ),
    ] = None,
    active: Annotated[
        bool,
        Field(description="Whether the webhook is enabled on creation", default=True),
    ] = True,
) -> str:
    """Create a repository webhook.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/webhooks``. The
    ``secret`` argument, if supplied, is forwarded to Bitbucket in the
    request body but the server response has ``configuration.secret``
    redacted before it reaches the agent. A Reversible Receipt is
    returned so the caller can undo the creation with
    ``bitbucket_delete_webhook``.

    Returns:
        JSON string with the redacted webhook object and a ``receipt``
        referencing ``bitbucket_delete_webhook``.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_WEBHOOKS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_dc_version(bb, required="5.4"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        parsed_events = _parse_webhook_events(events)
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    try:
        created = bb.create_webhook(
            project_key,
            repo_slug,
            name=name,
            url=url,
            events=parsed_events,
            secret=secret,
            active=active,
        )
    except Exception as e:
        logger.error(f"Error creating webhook: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    # Post-process: redact any echoed secret before it reaches the agent.
    redacted = redact_secrets(created)

    # Build Reversible Receipt pointing at the inverse delete tool. The
    # receipt's ``recipient_scope`` summarizes the broadcast target (url
    # + events) without ever including the HMAC secret.
    webhook_id = created.get("id")
    receipt = dc_guards.build_receipt(
        object_id=str(webhook_id) if webhook_id is not None else "",
        inverse_tool="bitbucket_delete_webhook",
        inverse_args={
            "project_key": project_key,
            "repo_slug": repo_slug,
            "webhook_id": webhook_id,
        },
        note=None,
        recipient_scope={"url": url, "events": parsed_events},
    )

    return json.dumps(
        {"success": True, "webhook": redacted, "receipt": receipt}, indent=2
    )


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_webhooks"},
    annotations={"title": "Update Webhook", "readOnlyHint": False},
)
async def update_webhook(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    webhook_id: Annotated[int, Field(description="The numeric webhook id")],
    name: Annotated[
        str | None,
        Field(description="Optional replacement webhook name", default=None),
    ] = None,
    url: Annotated[
        str | None,
        Field(description="Optional replacement target URL", default=None),
    ] = None,
    events: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement JSON array of event keys, e.g. "
                "'[\"repo:refs_changed\"]'."
            ),
            default=None,
        ),
    ] = None,
    configuration: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement configuration object as a JSON "
                "string, e.g. '{\"secret\": \"new-hmac\"}'. The secret "
                "is forwarded to Bitbucket but redacted from the response."
            ),
            default=None,
        ),
    ] = None,
    active: Annotated[
        bool | None,
        Field(description="Optional replacement enabled flag", default=None),
    ] = None,
) -> str:
    """Update an existing webhook.

    Wraps ``PUT /rest/api/latest/projects/{k}/repos/{r}/webhooks/{id}``. Only
    the fields supplied in the call are forwarded in the PUT body. The
    server-side response has ``configuration.secret`` redacted before
    returning to the agent.

    Returns:
        JSON string with the updated (and redacted) webhook object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_WEBHOOKS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_dc_version(bb, required="5.4"):
        return json.dumps({"success": False, **err.to_dict()})

    fields: dict[str, Any] = {}
    try:
        if name is not None:
            fields["name"] = name
        if url is not None:
            fields["url"] = url
        if events is not None:
            fields["events"] = _parse_webhook_events(events)
        parsed_configuration = _parse_webhook_configuration(configuration)
        if parsed_configuration is not None:
            fields["configuration"] = parsed_configuration
        if active is not None:
            fields["active"] = active
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    if not fields:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "At least one of name, url, events, configuration, or "
                    "active must be supplied."
                ),
            }
        )

    try:
        updated = bb.update_webhook(project_key, repo_slug, webhook_id, **fields)
    except Exception as e:
        logger.error(f"Error updating webhook: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    redacted = redact_secrets(updated)
    return json.dumps({"success": True, "webhook": redacted}, indent=2)


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_webhooks"},
    annotations={"title": "Delete Webhook", "readOnlyHint": False},
)
async def delete_webhook(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    webhook_id: Annotated[int, Field(description="The numeric webhook id")],
) -> str:
    """Delete a webhook.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/webhooks/{id}``.

    Returns:
        JSON string confirming deletion.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_WEBHOOKS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_dc_version(bb, required="5.4"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.delete_webhook(project_key, repo_slug, webhook_id)
        return json.dumps(
            {"success": True, "webhook_id": webhook_id, "deleted": True},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error deleting webhook: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Required-Builds Merge-Check Tools (toolset: bitbucket_required_builds)
# =============================================================================
#
# Manage required-builds merge-check conditions via the bundled Bitbucket
# required-builds plugin at ``/rest/required-builds/latest/projects/{k}/
# repos/{r}/condition``. Each tool runs the uniform pre-HTTP guard prelude
# defined in the ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#
# Plugin availability (Requirement 3.4): when the required-builds plugin is
# absent or disabled, the endpoint returns ``404 Not Found``. The mixin
# raises :class:`RequiredBuildsPluginUnavailableError` on that signal and
# every tool here catches it, surfacing a structured ``plugin_unavailable``
# error naming the plugin so the agent can advise the operator to install
# or enable it.


from mcp_atlassian.bitbucket.required_builds import (  # noqa: E402
    RequiredBuildsPluginUnavailableError,
)


_REQUIRED_BUILDS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_required_builds",
}
_REQUIRED_BUILDS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_required_builds",
}


def _required_builds_plugin_unavailable_response(
    exc: RequiredBuildsPluginUnavailableError,
) -> str:
    """Render a structured ``plugin_unavailable`` error response.

    Centralized here so all three tools emit the same envelope naming the
    Bitbucket ``required-builds`` plugin and carrying the raised message
    as the ``details.reason``.
    """
    return json.dumps(
        {
            "success": False,
            "error_code": "plugin_unavailable",
            "message": (
                "Bitbucket required-builds plugin endpoint is unavailable. "
                "Install or enable the bundled required-builds plugin on "
                "the target Bitbucket DC instance."
            ),
            "details": {
                "plugin": "required-builds",
                "product": "bitbucket",
                "reason": str(exc),
            },
        },
        indent=2,
    )


def _parse_ref_matcher(value: str | dict[str, Any], *, field_name: str) -> dict[str, Any]:
    """Normalize a ref-matcher argument into the dict shape the plugin expects.

    Accepts either a dict (already-parsed) or a JSON-encoded string. Raises
    ``ValueError`` on malformed input so the caller can return a structured
    error to the agent without issuing any HTTP request.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(
            f"{field_name} must be a JSON object or JSON-encoded string, "
            f"got {type(value).__name__}"
        )
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{field_name} is not valid JSON: {exc.msg}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"{field_name} must decode to a JSON object, got {type(parsed).__name__}"
        )
    return parsed


def _parse_build_parent_keys(value: str | list[Any]) -> list[str]:
    """Normalize the ``build_parent_keys`` argument into a list of strings.

    Accepts either a list (already-parsed) or a JSON-encoded string. Each
    element must be a non-empty string (for example a Bamboo plan key
    like ``"PROJ-PLAN"``).
    """
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"build_parent_keys is not valid JSON: {exc.msg}"
            ) from exc
    else:
        parsed = value
    if not isinstance(parsed, list):
        raise ValueError(
            f"build_parent_keys must be a JSON array, got {type(parsed).__name__}"
        )
    normalized: list[str] = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(
                f"build_parent_keys[{i}] must be a non-empty string "
                f"(e.g. 'PROJ-PLAN'), got {entry!r}"
            )
        normalized.append(entry)
    if not normalized:
        raise ValueError(
            "build_parent_keys must contain at least one build-parent key"
        )
    return normalized


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_required_builds"},
    annotations={"title": "List Required Builds", "readOnlyHint": True},
)
async def list_required_builds(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of conditions to return. Default 100.",
            default=100,
            ge=1,
        ),
    ] = 100,
) -> str:
    """List required-build merge-check conditions on a repository.

    Wraps ``GET /rest/required-builds/latest/projects/{k}/repos/{r}/condition``.
    Each condition names one or more build-parent keys whose builds must
    succeed before a PR can merge, scoped to a ref matcher.

    Returns:
        JSON string with the list of condition objects. If the required-builds
        plugin is not installed on the target DC instance, returns a
        structured ``plugin_unavailable`` error naming the plugin.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_REQUIRED_BUILDS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_required_builds"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        conditions = bb.list_required_builds(project_key, repo_slug, limit=limit)
        return json.dumps(
            {
                "success": True,
                "count": len(conditions),
                "conditions": conditions,
            },
            indent=2,
        )
    except RequiredBuildsPluginUnavailableError as exc:
        return _required_builds_plugin_unavailable_response(exc)
    except Exception as e:
        logger.error(f"Error listing required-build conditions: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_required_builds"},
    annotations={"title": "Create Required Build", "readOnlyHint": False},
)
async def create_required_build(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    build_parent_keys: Annotated[
        str,
        Field(
            description=(
                "JSON array of build-parent keys (e.g. Bamboo plan keys) "
                "whose builds must all succeed before merge, e.g. "
                "'[\"PROJ-PLAN\", \"PROJ-INTEGRATION\"]'."
            )
        ),
    ],
    ref_matcher: Annotated[
        str,
        Field(
            description=(
                "Ref matcher as a JSON object string describing which "
                "branches the condition applies to, shaped like "
                "'{\"id\": \"refs/heads/main\", \"type\": {\"id\": \"BRANCH\"}}'."
            )
        ),
    ],
    exemption_matcher: Annotated[
        str | None,
        Field(
            description=(
                "Optional exemption matcher as a JSON object string "
                "naming users or groups permitted to bypass the gate. "
                "Omit to leave no exemptions."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create a required-build merge-check condition on a repository.

    Wraps ``POST /rest/required-builds/latest/projects/{k}/repos/{r}/condition``.

    Returns:
        JSON string with the created condition object. If the required-builds
        plugin is not installed, returns a structured ``plugin_unavailable``
        error naming the plugin.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_REQUIRED_BUILDS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_create_required_build"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        parsed_keys = _parse_build_parent_keys(build_parent_keys)
        parsed_ref = _parse_ref_matcher(ref_matcher, field_name="ref_matcher")
        parsed_exemption: dict[str, Any] | None = None
        if exemption_matcher is not None:
            parsed_exemption = _parse_ref_matcher(
                exemption_matcher, field_name="exemption_matcher"
            )
    except ValueError as exc:
        return json.dumps({"success": False, "error": str(exc)})

    try:
        condition = bb.create_required_build(
            project_key,
            repo_slug,
            build_parent_keys=parsed_keys,
            ref_matcher=parsed_ref,
            exemption_matcher=parsed_exemption,
        )
        return json.dumps({"success": True, "condition": condition}, indent=2)
    except RequiredBuildsPluginUnavailableError as exc:
        return _required_builds_plugin_unavailable_response(exc)
    except Exception as e:
        logger.error(f"Error creating required-build condition: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_required_builds"},
    annotations={"title": "Delete Required Build", "readOnlyHint": False},
)
async def delete_required_build(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    condition_id: Annotated[
        int, Field(description="The numeric required-build condition id")
    ],
) -> str:
    """Delete a required-build merge-check condition.

    Wraps ``DELETE /rest/required-builds/latest/projects/{k}/repos/{r}/
    condition/{id}``.

    Returns:
        JSON string confirming deletion. If the required-builds plugin is
        not installed, returns a structured ``plugin_unavailable`` error
        naming the plugin.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_REQUIRED_BUILDS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_delete_required_build"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.delete_required_build(project_key, repo_slug, condition_id)
        return json.dumps(
            {
                "success": True,
                "condition_id": condition_id,
                "deleted": True,
            },
            indent=2,
        )
    except RequiredBuildsPluginUnavailableError as exc:
        return _required_builds_plugin_unavailable_response(exc)
    except Exception as e:
        logger.error(f"Error deleting required-build condition: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Repository Admin Tools (toolset: bitbucket_repository_admin)
# =============================================================================
#
# Manage repositories via ``/rest/api/latest/projects/{k}/repos``. Each tool
# runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#      For ``bitbucket_fork_repository``, the gate is applied to the
#      destination project (where the fork will land), not the source.
#
# Repository deletion is intentionally NOT exposed (Requirement 4.4). Use
# the Bitbucket UI or an explicit admin CLI to delete a repository.


_REPOSITORY_ADMIN_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_repository_admin",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repository_admin"},
    annotations={"title": "Create Repository", "readOnlyHint": False},
)
async def create_repository(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key that will own the new repository")
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Display name for the repository. Bitbucket derives the "
                "repository slug from this name on creation."
            )
        ),
    ],
    scm: Annotated[
        str,
        Field(
            description=(
                "SCM to use. Bitbucket DC only supports 'git' today, which "
                "is the default."
            ),
            default="git",
        ),
    ] = "git",
    forkable: Annotated[
        bool,
        Field(
            description="Whether other users may fork the repository.",
            default=True,
        ),
    ] = True,
    public: Annotated[
        bool,
        Field(
            description="Whether the repository is publicly readable.",
            default=False,
        ),
    ] = False,
) -> str:
    """Create a repository under an existing project.

    Wraps ``POST /rest/api/latest/projects/{project_key}/repos``.

    Returns:
        JSON string with the created repository object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_REPOSITORY_ADMIN_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        repository = bb.create_repository(
            project_key,
            name=name,
            scm=scm,
            forkable=forkable,
            public=public,
        )
        return json.dumps({"success": True, "repository": repository}, indent=2)
    except Exception as e:
        logger.error(f"Error creating repository: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repository_admin"},
    annotations={"title": "Update Repository", "readOnlyHint": False},
)
async def update_repository(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    name: Annotated[
        str | None,
        Field(description="Optional replacement repository name", default=None),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional replacement repository description", default=None),
    ] = None,
    default_branch: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement default branch ref id "
                "(e.g. 'refs/heads/main')."
            ),
            default=None,
        ),
    ] = None,
    public: Annotated[
        bool | None,
        Field(description="Optional replacement public flag", default=None),
    ] = None,
    forkable: Annotated[
        bool | None,
        Field(description="Optional replacement forkable flag", default=None),
    ] = None,
) -> str:
    """Update mutable fields on an existing repository.

    Wraps ``PUT /rest/api/latest/projects/{project_key}/repos/{repo_slug}``.
    Only the fields supplied in the call are forwarded in the PUT body;
    omitted fields leave the corresponding repository attribute untouched.

    Returns:
        JSON string with the updated repository object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_REPOSITORY_ADMIN_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if default_branch is not None:
        fields["defaultBranch"] = default_branch
    if public is not None:
        fields["public"] = public
    if forkable is not None:
        fields["forkable"] = forkable

    if not fields:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "At least one of name, description, default_branch, "
                    "public, or forkable must be supplied."
                ),
            }
        )

    try:
        repository = bb.update_repository(project_key, repo_slug, **fields)
        return json.dumps({"success": True, "repository": repository}, indent=2)
    except Exception as e:
        logger.error(f"Error updating repository: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repository_admin"},
    annotations={"title": "Fork Repository", "readOnlyHint": False},
)
async def fork_repository(
    ctx: Context,
    source_project: Annotated[
        str, Field(description="Project key of the repository to fork from")
    ],
    source_slug: Annotated[
        str, Field(description="Slug of the repository to fork from")
    ],
    dest_project: Annotated[
        str,
        Field(
            description=(
                "Project key the fork will land in. Must be within "
                "BITBUCKET_PROJECTS_FILTER when the filter is configured."
            )
        ),
    ],
    name: Annotated[
        str | None,
        Field(
            description=(
                "Optional name for the forked repository. Omit to reuse "
                "the source repository's name."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Fork a repository into a different project.

    Wraps ``POST /rest/api/latest/projects/{source_project}/repos/{source_slug}``
    with the DC fork payload shape. Project-scope filtering is applied to
    the destination project (the fork's new home), not the source.

    Returns:
        JSON string with the created fork repository object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_REPOSITORY_ADMIN_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # Apply the project filter to the destination project: the fork lands
    # in dest_project, so that is the key that must be in scope.
    if err := dc_guards.check_project_filter(
        "bitbucket", dest_project, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_fork_repository"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        fork = bb.fork_repository(
            source_project,
            source_slug,
            dest_project=dest_project,
            name=name,
        )
        return json.dumps({"success": True, "repository": fork}, indent=2)
    except Exception as e:
        logger.error(f"Error forking repository: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Project Admin Tools (toolset: bitbucket_project_admin)
# =============================================================================
#
# Manage projects via ``/rest/api/latest/projects``. Each tool runs the
# uniform pre-HTTP guard prelude defined in the ``atlassian-dc-tool-parity``
# design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#      For ``bitbucket_create_project`` the new project key is evaluated;
#      for ``bitbucket_update_project`` the existing project key is evaluated.
#
# Project deletion is intentionally NOT exposed (Requirement 5.3). Use the
# Bitbucket UI or an explicit admin CLI to delete a project.


_PROJECT_ADMIN_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_project_admin",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_project_admin"},
    annotations={"title": "Create Project", "readOnlyHint": False},
)
async def create_project(
    ctx: Context,
    key: Annotated[
        str,
        Field(
            description=(
                "Project key (uppercase letters, digits, and underscores). "
                "Bitbucket uses this as the immutable project identifier."
            )
        ),
    ],
    name: Annotated[str, Field(description="Display name for the project")],
    description: Annotated[
        str | None,
        Field(description="Optional project description", default=None),
    ] = None,
    public: Annotated[
        bool,
        Field(
            description="Whether the project is publicly visible.",
            default=False,
        ),
    ] = False,
) -> str:
    """Create a new Bitbucket project.

    Wraps ``POST /rest/api/latest/projects``. The new project ``key`` is
    evaluated against ``BITBUCKET_PROJECTS_FILTER`` before the outbound
    HTTP call so a filtered-out key yields a structured ``filtered_out``
    error with zero side effects.

    Returns:
        JSON string with the created project object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_PROJECT_ADMIN_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # Gate the new project key against the scope filter so callers cannot
    # create out-of-scope projects through the admin toolset.
    if err := dc_guards.check_project_filter(
        "bitbucket", key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_create_project"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        project = bb.create_project(
            key=key,
            name=name,
            description=description,
            public=public,
        )
        return json.dumps({"success": True, "project": project}, indent=2)
    except Exception as e:
        logger.error(f"Error creating project: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_project_admin"},
    annotations={"title": "Update Project", "readOnlyHint": False},
)
async def update_project(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    name: Annotated[
        str | None,
        Field(description="Optional replacement project name", default=None),
    ] = None,
    description: Annotated[
        str | None,
        Field(description="Optional replacement project description", default=None),
    ] = None,
    avatar: Annotated[
        str | None,
        Field(
            description=(
                "Optional replacement avatar. Accepts the data-URI shape "
                "Bitbucket expects, e.g. 'data:image/png;base64,...'."
            ),
            default=None,
        ),
    ] = None,
    public: Annotated[
        bool | None,
        Field(description="Optional replacement public flag", default=None),
    ] = None,
) -> str:
    """Update mutable fields on an existing project.

    Wraps ``PUT /rest/api/latest/projects/{project_key}``. Only the fields
    supplied in the call are forwarded in the PUT body; omitted fields
    leave the corresponding project attribute untouched.

    Returns:
        JSON string with the updated project object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    if err := dc_guards.check_read_only(_PROJECT_ADMIN_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_update_project"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    fields: dict[str, Any] = {}
    if name is not None:
        fields["name"] = name
    if description is not None:
        fields["description"] = description
    if avatar is not None:
        fields["avatar"] = avatar
    if public is not None:
        fields["public"] = public

    if not fields:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "At least one of name, description, avatar, or public "
                    "must be supplied."
                ),
            }
        )

    try:
        project = bb.update_project(project_key, **fields)
        return json.dumps({"success": True, "project": project}, indent=2)
    except Exception as e:
        logger.error(f"Error updating project: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Pull-Request Comment Reactions (toolset: bitbucket_pull_requests, DC 8.8+)
# =============================================================================
#
# Add and remove emoji reactions on pull-request comments. Each tool runs
# the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#   3. ``check_dc_version(required="8.8")`` — comment reactions are only
#      available on Bitbucket DC 8.8 or newer. Earlier versions return a
#      structured ``dc_version_too_old`` error naming 8.8 as the minimum
#      version so the agent can explain the constraint to the operator.
#
# These tools live in the existing ``toolset:bitbucket_pull_requests``
# toolset alongside the other PR comment operations; no new toolset is
# introduced.


_PR_REACTIONS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_pull_requests",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Add PR Comment Reaction", "readOnlyHint": False},
)
async def add_pr_comment_reaction(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request id")],
    comment_id: Annotated[int, Field(description="The target comment id")],
    emoji: Annotated[
        str,
        Field(
            description=(
                "Emoji shortcode to add as a reaction, e.g. '+1', '-1', "
                "'smile', 'tada', 'heart'. The value is used verbatim as "
                "the final path segment of the reactions endpoint."
            )
        ),
    ],
) -> str:
    """Add an emoji reaction to a pull-request comment.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/pull-requests/
    {pr_id}/comments/{comment_id}/reactions/{emoji}``. Requires Bitbucket
    Data Center 8.8 or newer; earlier versions return a structured
    ``dc_version_too_old`` error before any outbound HTTP call.

    Returns:
        JSON string with the reaction object as returned by Bitbucket DC.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_PR_REACTIONS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_add_pr_comment_reaction"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 4. DC version gate — PR comment reactions are DC 8.8+.
    if err := dc_guards.check_dc_version(bb, required="8.8"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        reaction = bb.add_pr_comment_reaction(
            project_key, repo_slug, pr_id, comment_id, emoji
        )
        return json.dumps({"success": True, "reaction": reaction}, indent=2)
    except Exception as e:
        logger.error(f"Error adding PR comment reaction: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Remove PR Comment Reaction", "readOnlyHint": False},
)
async def remove_pr_comment_reaction(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request id")],
    comment_id: Annotated[int, Field(description="The target comment id")],
    emoji: Annotated[
        str,
        Field(
            description=(
                "Emoji shortcode to remove. Only the authenticated user's "
                "own reaction with this shortcode is removed."
            )
        ),
    ],
) -> str:
    """Remove the authenticated user's emoji reaction from a PR comment.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/pull-requests/
    {pr_id}/comments/{comment_id}/reactions/{emoji}``. Requires Bitbucket
    Data Center 8.8 or newer; earlier versions return a structured
    ``dc_version_too_old`` error before any outbound HTTP call.

    Returns:
        JSON string confirming the reaction was removed.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_PR_REACTIONS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_remove_pr_comment_reaction"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 4. DC version gate — PR comment reactions are DC 8.8+.
    if err := dc_guards.check_dc_version(bb, required="8.8"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.remove_pr_comment_reaction(
            project_key, repo_slug, pr_id, comment_id, emoji
        )
        return json.dumps(
            {
                "success": True,
                "comment_id": comment_id,
                "emoji": emoji,
                "removed": True,
            },
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error removing PR comment reaction: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

# =============================================================================
# Watch / Unwatch — PRs (toolset: bitbucket_pull_requests)
#                 — Repositories (toolset: bitbucket_repositories)
# =============================================================================
#
# Self-scoped watch / unwatch for pull requests and repositories. Each
# tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#
# The underlying mixin normalises "already watching" / "not watching"
# responses into structured ``already_watched`` / ``not_watched`` flags so
# repeated calls are idempotent at the tool surface (per Requirement 7.3
# and 7.4). No DC version gate applies — watch endpoints have been
# available since early DC releases.


_WATCH_PR_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_pull_requests",
}

_WATCH_REPO_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_repositories",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Watch Pull Request", "readOnlyHint": False},
)
async def watch_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request id")],
) -> str:
    """Start watching a pull request for the authenticated user.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/pull-requests/
    {pr_id}/watch``. Idempotent: if the PR is already being watched, the
    tool returns ``success`` with ``already_watched=true`` rather than
    raising.

    Returns:
        JSON string with ``success`` and ``already_watched`` flags.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_WATCH_PR_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = bb.watch_pr(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, **result}, indent=2)
    except Exception as e:
        logger.error(f"Error watching PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_pull_requests"},
    annotations={"title": "Unwatch Pull Request", "readOnlyHint": False},
)
async def unwatch_pull_request(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request id")],
) -> str:
    """Stop watching a pull request for the authenticated user.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/pull-requests/
    {pr_id}/watch``. Idempotent: if the PR is not currently being
    watched, the tool returns ``success`` with ``not_watched=true``
    rather than raising.

    Returns:
        JSON string with ``success`` and ``not_watched`` flags.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_WATCH_PR_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = bb.unwatch_pr(project_key, repo_slug, pr_id)
        return json.dumps({"success": True, **result}, indent=2)
    except Exception as e:
        logger.error(f"Error unwatching PR: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Watch Repository", "readOnlyHint": False},
)
async def watch_repository(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
) -> str:
    """Start watching a repository for the authenticated user.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/watch``.
    Idempotent: if the repository is already being watched, the tool
    returns ``success`` with ``already_watched=true`` rather than
    raising.

    Returns:
        JSON string with ``success`` and ``already_watched`` flags.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_WATCH_REPO_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = bb.watch_repo(project_key, repo_slug)
        return json.dumps({"success": True, **result}, indent=2)
    except Exception as e:
        logger.error(f"Error watching repository: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Unwatch Repository", "readOnlyHint": False},
)
async def unwatch_repository(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
) -> str:
    """Stop watching a repository for the authenticated user.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/watch``.
    Idempotent: if the repository is not currently being watched, the
    tool returns ``success`` with ``not_watched=true`` rather than
    raising.

    Returns:
        JSON string with ``success`` and ``not_watched`` flags.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_WATCH_REPO_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = bb.unwatch_repo(project_key, repo_slug)
        return json.dumps({"success": True, **result}, indent=2)
    except Exception as e:
        logger.error(f"Error unwatching repository: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Commit Comments (toolset: bitbucket_commits)
# =============================================================================
#
# List, create, update and delete comments attached to a specific commit.
# Commit comments live alongside the commit itself and are distinct from
# pull-request comments; a commit-level thread survives even if the PR
# containing the commit is later deleted.
#
# Each tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools when
#      ``READ_ONLY_MODE=true``; zero HTTP side effects on denial. The
#      read tool (``bitbucket_list_commit_comments``) runs the same call
#      for uniformity; because it has no ``write`` tag the guard is a
#      no-op.
#   2. ``check_project_filter`` — reject any project key that falls outside
#      ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound request.
#
# Authorization on delete (Requirement 8.5): Bitbucket DC permits only
# the original comment author (or an admin) to delete a commit comment
# and returns 401/403 otherwise. The mixin translates that status into
# :class:`NotCommentAuthorError` so this module can surface a structured
# ``not_comment_author`` error without having to inspect HTTP status
# codes inside the tool function.


from mcp_atlassian.bitbucket.commit_comments import (  # noqa: E402
    NotCommentAuthorError,
)


_COMMIT_COMMENTS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_commits",
}
_COMMIT_COMMENTS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_commits",
}


def _not_comment_author_response(exc: NotCommentAuthorError) -> str:
    """Render a structured ``not_comment_author`` error response.

    Bitbucket DC returns HTTP 401/403 when the authenticated user is
    neither the original commit-comment author nor an admin. The mixin
    translates that into :class:`NotCommentAuthorError`; this helper
    renders the structured error envelope defined by Requirement 8.5 so
    the agent can explain the constraint to the operator without seeing
    a raw HTTP status string.
    """
    return json.dumps(
        {
            "success": False,
            "error_code": "not_comment_author",
            "message": (
                "Only the original comment author (or a Bitbucket admin) "
                "may delete this commit comment."
            ),
            "details": {"reason": str(exc)},
        },
        indent=2,
    )


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_commits"},
    annotations={"title": "List Commit Comments", "readOnlyHint": True},
)
async def list_commit_comments(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key (e.g., 'PROJ')")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash")],
    path: Annotated[
        str | None,
        Field(
            description=(
                "Optional file path to scope results to inline comments "
                "on that path; omit for all comments (general + inline)."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """List comments attached to a commit.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/commits/
    {commit_id}/comments``. Returns every comment (general and inline)
    across all pages.

    Returns:
        JSON string with the list of comment objects.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_COMMIT_COMMENTS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        comments = bb.list_commit_comments(
            project_key, repo_slug, commit_id, path=path
        )
        return json.dumps(
            {"success": True, "count": len(comments), "comments": comments},
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing commit comments: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_commits"},
    annotations={"title": "Add Commit Comment", "readOnlyHint": False},
)
async def add_commit_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash")],
    text: Annotated[str, Field(description="Comment body")],
    path: Annotated[
        str | None,
        Field(
            description=(
                "Optional file path for inline anchoring. When any of "
                "``path``, ``line``, ``line_type`` or ``file_type`` is "
                "supplied, all four are bundled into the ``anchor`` "
                "object Bitbucket expects for inline comments."
            ),
            default=None,
        ),
    ] = None,
    line: Annotated[
        int | None,
        Field(
            description="Optional line number inside ``path``.",
            default=None,
        ),
    ] = None,
    line_type: Annotated[
        str | None,
        Field(
            description=(
                "Optional line diff type — one of 'ADDED', 'REMOVED', "
                "'CONTEXT'."
            ),
            default=None,
        ),
    ] = None,
    file_type: Annotated[
        str | None,
        Field(
            description="Optional file side — one of 'FROM' or 'TO'.",
            default=None,
        ),
    ] = None,
) -> str:
    """Create a general or inline comment on a commit.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/commits/
    {commit_id}/comments``. When any of ``path``, ``line``, ``line_type``
    or ``file_type`` is supplied, the comment is created as inline; when
    all four are omitted, the comment is created as a general commit
    comment.

    Returns:
        JSON string with the created comment object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check.
    if err := dc_guards.check_read_only(_COMMIT_COMMENTS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        comment = bb.add_commit_comment(
            project_key,
            repo_slug,
            commit_id,
            text=text,
            path=path,
            line=line,
            line_type=line_type,
            file_type=file_type,
        )
        return json.dumps({"success": True, "comment": comment}, indent=2)
    except Exception as e:
        logger.error(f"Error adding commit comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_commits"},
    annotations={"title": "Update Commit Comment", "readOnlyHint": False},
)
async def update_commit_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash")],
    comment_id: Annotated[int, Field(description="The comment id to update")],
    text: Annotated[str, Field(description="Replacement comment text")],
    version: Annotated[
        int,
        Field(description="Current comment version (optimistic locking)"),
    ],
) -> str:
    """Update the text of an existing commit comment.

    Wraps ``PUT /rest/api/latest/projects/{k}/repos/{r}/commits/
    {commit_id}/comments/{comment_id}``. ``version`` must match the
    comment's current version for Bitbucket's optimistic-locking guard
    to accept the update.

    Returns:
        JSON string with the updated comment object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check.
    if err := dc_guards.check_read_only(_COMMIT_COMMENTS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        comment = bb.update_commit_comment(
            project_key,
            repo_slug,
            commit_id,
            comment_id,
            text=text,
            version=version,
        )
        return json.dumps({"success": True, "comment": comment}, indent=2)
    except Exception as e:
        logger.error(f"Error updating commit comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_commits"},
    annotations={"title": "Delete Commit Comment", "readOnlyHint": False},
)
async def delete_commit_comment(
    ctx: Context,
    project_key: Annotated[str, Field(description="The project key")],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    commit_id: Annotated[str, Field(description="The commit hash")],
    comment_id: Annotated[int, Field(description="The comment id to delete")],
    version: Annotated[
        int,
        Field(description="Current comment version (optimistic locking)"),
    ],
) -> str:
    """Delete a commit comment.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/commits/
    {commit_id}/comments/{comment_id}``. Bitbucket permits only the
    original comment author (or an admin) to delete a commit comment;
    when the authenticated user is neither, the tool surfaces a
    structured ``not_comment_author`` error (Requirement 8.5) rather
    than leaking the raw HTTP status.

    Returns:
        JSON string confirming the deletion. On authorization failure,
        returns a structured ``not_comment_author`` error envelope.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check.
    if err := dc_guards.check_read_only(_COMMIT_COMMENTS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.delete_commit_comment(
            project_key,
            repo_slug,
            commit_id,
            comment_id,
            version=version,
        )
        return json.dumps(
            {
                "success": True,
                "comment_id": comment_id,
                "commit_id": commit_id,
                "deleted": True,
            },
            indent=2,
        )
    except NotCommentAuthorError as exc:
        return _not_comment_author_response(exc)
    except Exception as e:
        logger.error(f"Error deleting commit comment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Markup Preview (toolset: bitbucket_repositories)
# =============================================================================
#
# Render Bitbucket-flavoured markup (Markdown) to HTML via
# ``POST /rest/api/latest/markup/preview``. The operation is a pure
# read-side preview — Bitbucket evaluates the markup in the supplied
# rendering context and returns the resulting HTML without persisting
# anything. Because it is idempotent, the tool is tagged with ``read``
# (Requirement 9.1 / 9.2) and contributes no write HTTP traffic.
#
# Prelude:
#
#   1. ``check_read_only`` — no-op for read-tagged tools, included for
#      uniformity with the rest of the Bitbucket tool surface.
#   2. ``check_project_filter`` — only when ``project_key`` is supplied.
#      The repository context (``project_key``, ``repo_slug``, ``page_type``)
#      is optional: Bitbucket will still render the markup without it, so
#      the operator may call the tool without any repository scope. When
#      ``project_key`` is omitted the filter guard is skipped; when it is
#      provided it is enforced against ``BITBUCKET_PROJECTS_FILTER`` before
#      any outbound HTTP request (Requirement 43.1).


_MARKUP_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_repositories",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "Render Markup Preview", "readOnlyHint": True},
)
async def render_markup(
    ctx: Context,
    markup_text: Annotated[
        str,
        Field(
            description=(
                "Raw Bitbucket-flavoured markup (Markdown) to render to "
                "HTML. The call is idempotent — Bitbucket evaluates the "
                "markup and returns the rendered HTML without persisting "
                "anything."
            )
        ),
    ],
    project_key: Annotated[
        str | None,
        Field(
            description=(
                "Optional project key providing the rendering context. "
                "When supplied, Bitbucket resolves relative links, "
                "mentions and emoji as it would inside that project, and "
                "the ``BITBUCKET_PROJECTS_FILTER`` allow-list is enforced."
            ),
            default=None,
        ),
    ] = None,
    repo_slug: Annotated[
        str | None,
        Field(
            description=(
                "Optional repository slug that, together with "
                "``project_key``, scopes the rendering context to a "
                "specific repository. Ignored by Bitbucket unless "
                "``project_key`` is also provided."
            ),
            default=None,
        ),
    ] = None,
    page_type: Annotated[
        str,
        Field(
            description=(
                "Rendering surface hint — for example 'COMMENT', "
                "'PULL_REQUEST', or 'README'. Defaults to 'COMMENT', "
                "matching the Bitbucket DC default."
            ),
            default="COMMENT",
        ),
    ] = "COMMENT",
) -> str:
    """Render Bitbucket markup to HTML without persisting anything.

    Wraps ``POST /rest/api/latest/markup/preview``. The endpoint is a
    pure preview: no repository or comment state is created or mutated,
    which is why this tool is tagged ``read`` (Requirement 9.1, 9.2).

    The repository context is optional. When ``project_key`` is supplied,
    the ``BITBUCKET_PROJECTS_FILTER`` allow-list is enforced before any
    outbound HTTP request; when it is omitted, the filter guard is
    skipped because the call is not project-scoped.

    Returns:
        JSON string with the rendered HTML under the ``html`` key.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_MARKUP_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter — only when a project key has been supplied, since
    #    the repository context is optional on this endpoint.
    if project_key is not None:
        if err := dc_guards.check_project_filter(
            "bitbucket", project_key, bb.config.projects_filter
        ):
            return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_render_markup"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        html = bb.render_markup(
            markup_text=markup_text,
            project_key=project_key,
            repo_slug=repo_slug,
            page_type=page_type,
        )
        return json.dumps({"success": True, "html": html}, indent=2)
    except Exception as e:
        logger.error(f"Error rendering markup: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Repository Labels (toolset: bitbucket_repositories)
# =============================================================================
#
# List, add and remove repository-level labels on a Bitbucket DC repo
# (``/rest/api/latest/projects/{k}/repos/{r}/labels``). Labels are
# lightweight categorisation tags attached to a repository — they are
# not related to PR labels or to the Atlassian "label" custom fields in
# Jira.
#
# Each tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — belt-and-suspenders block for write tools
#      when ``READ_ONLY_MODE=true``; zero HTTP side effects on denial.
#      The read tool (``bitbucket_list_repository_labels``) runs the
#      same call for uniformity; because it has no ``write`` tag the
#      guard is a no-op.
#   2. ``check_project_filter`` — reject any project key that falls
#      outside ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound
#      request (Requirement 43.1).
#
# Idempotency (Requirement 10.4): ``bitbucket_add_repository_label``
# translates Bitbucket's 409 Conflict response (label already attached)
# into ``{"already_labeled": true}`` so repeated calls are safe and the
# tool surface never leaks a raw HTTP error for this specific case.


_REPO_LABELS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_repositories",
}
_REPO_LABELS_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_repositories",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_repositories"},
    annotations={"title": "List Repository Labels", "readOnlyHint": True},
)
async def list_repository_labels(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of labels to return per page.",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> str:
    """List labels attached to a Bitbucket DC repository.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/labels`` and
    flattens the paged response to plain label name strings
    (Requirement 10.1).

    Returns:
        JSON string with a ``labels`` array of label name strings.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_REPO_LABELS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_repository_labels"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        labels = bb.list_repo_labels(project_key, repo_slug, limit=limit)
        return json.dumps({"success": True, "labels": labels}, indent=2)
    except Exception as e:
        logger.error(f"Error listing repository labels: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Add Repository Label", "readOnlyHint": False},
)
async def add_repository_label(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    label: Annotated[
        str,
        Field(
            description=(
                "Label name to attach to the repository. If the label "
                "is already attached, the call is idempotent and "
                "returns ``already_labeled=true``."
            )
        ),
    ],
) -> str:
    """Attach a label to a Bitbucket DC repository (idempotent).

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/labels``.
    Bitbucket returns HTTP 409 when the label is already attached; the
    mixin maps that outcome to ``{"already_labeled": true}`` so repeated
    calls are safe (Requirement 10.2, 10.4).

    Returns:
        JSON string with ``success`` and an ``already_labeled`` flag.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_REPO_LABELS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_add_repository_label"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = bb.add_repo_label(project_key, repo_slug, label)
        return json.dumps({"success": True, **result}, indent=2)
    except Exception as e:
        logger.error(f"Error adding repository label: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_repositories"},
    annotations={"title": "Remove Repository Label", "readOnlyHint": False},
)
async def remove_repository_label(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    label: Annotated[
        str,
        Field(description="Label name to detach from the repository."),
    ],
) -> str:
    """Detach a label from a Bitbucket DC repository.

    Wraps ``DELETE /rest/api/latest/projects/{k}/repos/{r}/labels/{label}``
    (Requirement 10.3).

    Returns:
        JSON string with ``success`` and the removed label name.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (belt-and-suspenders).
    if err := dc_guards.check_read_only(_REPO_LABELS_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_remove_repository_label"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        bb.remove_repo_label(project_key, repo_slug, label)
        return json.dumps({"success": True, "label": label}, indent=2)
    except Exception as e:
        logger.error(f"Error removing repository label: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Deployments (toolset: bitbucket_deployments, DC 7.10+)
# =============================================================================
#
# Read-only visibility into deployments recorded against a repository
# via ``/rest/api/latest/projects/{k}/repos/{r}/deployments``. Bitbucket
# DC 7.10 introduced this endpoint, so every tool in this toolset runs
# a DC version gate that returns a structured ``dc_version_too_old``
# error on older instances before issuing any HTTP call
# (Requirement 11.4).
#
# Each tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — no-op for read-tagged tools, included for
#      uniformity with the rest of the Bitbucket tool surface.
#   2. ``check_project_filter`` — reject any project key that falls
#      outside ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound
#      request (Requirement 43.1).
#   3. ``check_dc_version(required="7.10")`` — deployments were
#      introduced in Bitbucket DC 7.10; older instances receive a
#      structured ``dc_version_too_old`` error without any HTTP call.
#
# Per Requirement 11.3, no Write_Tool is registered in this toolset —
# creating, updating or deleting deployments is intentionally out of
# scope.


_DEPLOYMENTS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_deployments",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_deployments"},
    annotations={"title": "List Deployments", "readOnlyHint": True},
)
async def list_deployments(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    environment: Annotated[
        str | None,
        Field(
            description=(
                "Optional environment name to filter by "
                "(e.g. 'production', 'staging')."
            ),
            default=None,
        ),
    ] = None,
    state: Annotated[
        str | None,
        Field(
            description=(
                "Optional deployment state to filter by — one of "
                "'SUCCESSFUL', 'FAILED', 'IN_PROGRESS', 'PENDING', "
                "'CANCELLED', 'ROLLED_BACK', 'UNKNOWN'."
            ),
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description="Maximum number of deployments to return per page.",
            default=25,
            ge=1,
            le=1000,
        ),
    ] = 25,
) -> str:
    """List deployments recorded against a Bitbucket DC repository.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/deployments``.
    Bitbucket DC 7.10+ is required; older instances receive a
    structured ``dc_version_too_old`` error (Requirement 11.4).

    Returns:
        JSON string with a ``deployments`` array of deployment objects.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_DEPLOYMENTS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_deployments"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 4. DC version gate — deployments are DC 7.10+.
    if err := dc_guards.check_dc_version(bb, required="7.10"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        deployments = bb.list_deployments(
            project_key,
            repo_slug,
            environment=environment,
            state=state,
            limit=limit,
        )
        return json.dumps(
            {
                "success": True,
                "count": len(deployments),
                "deployments": deployments,
            },
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error listing deployments: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_deployments"},
    annotations={"title": "Get Deployment", "readOnlyHint": True},
)
async def get_deployment(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    deployment_id: Annotated[
        str,
        Field(
            description=(
                "The deployment id (the ``key`` field returned by "
                "``bitbucket_list_deployments``)."
            )
        ),
    ],
) -> str:
    """Retrieve a single deployment by id.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/deployments/
    {deployment_id}``. Bitbucket DC 7.10+ is required; older instances
    receive a structured ``dc_version_too_old`` error (Requirement 11.4).

    Returns:
        JSON string with the ``deployment`` object.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_DEPLOYMENTS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_get_deployment"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 4. DC version gate — deployments are DC 7.10+.
    if err := dc_guards.check_dc_version(bb, required="7.10"):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        deployment = bb.get_deployment(project_key, repo_slug, deployment_id)
        return json.dumps(
            {"success": True, "deployment": deployment}, indent=2
        )
    except Exception as e:
        logger.error(f"Error getting deployment: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

# =============================================================================
# Pull-Request Participants (toolset: bitbucket_pull_requests)
# =============================================================================
#
# Dedicated read endpoint that returns participant slice of a pull
# request — role (AUTHOR / REVIEWER / PARTICIPANT), approval status, and
# last-reviewed-commit — without requiring the caller to parse the full
# PR payload (Requirement 12.1).
#
# The tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — no-op for read-tagged tools, included for
#      uniformity with the rest of the Bitbucket tool surface.
#   2. ``check_project_filter`` — reject any project key that falls
#      outside ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound
#      request (Requirement 43.1).
#
# No DC version gate applies — the participants sub-resource has been
# part of the Bitbucket DC PR REST surface for many releases.


_PR_PARTICIPANTS_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_pull_requests",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_pull_requests"},
    annotations={"title": "List Pull Request Participants", "readOnlyHint": True},
)
async def list_pull_request_participants(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    pr_id: Annotated[int, Field(description="The pull request id")],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of participants to return per page.",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> str:
    """List participants of a pull request with role and approval status.

    Wraps ``GET /rest/api/latest/projects/{k}/repos/{r}/pull-requests/
    {pr_id}/participants``. Returns reviewers and other participants
    with their ``role`` (``AUTHOR`` / ``REVIEWER`` / ``PARTICIPANT``),
    ``approved`` flag, ``status`` (``APPROVED`` / ``NEEDS_WORK`` /
    ``UNAPPROVED``) and ``lastReviewedCommit`` when present — without
    requiring the full PR body to be fetched (Requirement 12.1).

    Returns:
        JSON string with a ``participants`` array.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_PR_PARTICIPANTS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_list_pull_request_participants"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        participants = bb.list_pr_participants(
            project_key, repo_slug, pr_id, limit=limit
        )
        return json.dumps(
            {
                "success": True,
                "count": len(participants),
                "participants": participants,
            },
            indent=2,
        )
    except Exception as e:
        logger.error(
            f"Error listing pull request participants: {e}", exc_info=True
        )
        return json.dumps({"success": False, "error": str(e)})

# =============================================================================
# Cherry-Pick (toolset: bitbucket_commits, Req 13)
# =============================================================================
#
# Apply an existing commit onto a target branch through Bitbucket DC's
# cherry-pick helper under ``/rest/api/latest/projects/{k}/repos/{r}/
# cherry-pick``. The tool is registered under the existing
# ``toolset:bitbucket_commits`` and tagged ``write`` — it runs the
# uniform pre-HTTP guard prelude defined in the ``atlassian-dc-tool-
# parity`` design:
#
#   1. ``check_read_only`` — reject write calls when
#      ``READ_ONLY_MODE=true`` (Requirement 41.1).
#   2. ``check_project_filter`` — reject any project key that falls
#      outside ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound
#      request (Requirement 43.1).
#
# On success, the tool returns a Reversible_Receipt whose ``object_id``
# carries the resulting commit hash on the target branch (Requirement
# 13.3). Cherry-pick is not a one-call-undo operation — reverting a
# pick requires an out-of-band revert commit or branch surgery — so the
# receipt's ``inverse_tool`` / ``inverse_args`` are ``None`` and a
# human-readable ``note`` explains the non-retractable nature. The
# ``recipient_scope`` summarises the broadcast target (source commit +
# destination branch) so downstream consumers can audit what was
# picked onto where without re-parsing the mixin response.
#
# When Bitbucket responds with 409 and an ``errors[].conflicts`` body,
# the mixin raises :class:`CherryPickConflictError`; the tool catches it
# and returns a structured ``cherry_pick_conflict`` error whose
# ``details.conflicts`` echoes the paths reported by Bitbucket
# (Requirement 13.2).


from mcp_atlassian.bitbucket.cherry_pick import (  # noqa: E402
    CherryPickConflictError,
)


_CHERRY_PICK_WRITE_TAGS: set[str] = {
    "bitbucket",
    "write",
    "toolset:bitbucket_commits",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "write", "toolset:bitbucket_commits"},
    annotations={"title": "Cherry-Pick Commit", "readOnlyHint": False},
)
async def cherry_pick_commit(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
    source_commit: Annotated[
        str,
        Field(
            description=(
                "Commit hash to cherry-pick — forwarded to Bitbucket as "
                "the ``commitId`` request field."
            )
        ),
    ],
    target_branch: Annotated[
        str,
        Field(
            description=(
                "Branch to apply the pick onto — forwarded as "
                "``destinationBranch``. May be given as either a short "
                "name ('main') or a full ref ('refs/heads/main')."
            )
        ),
    ],
    message: Annotated[
        str | None,
        Field(
            description=(
                "Optional override for the new commit's message. When "
                "omitted, Bitbucket reuses the source commit's message."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Cherry-pick a commit onto a target branch.

    Wraps ``POST /rest/api/latest/projects/{k}/repos/{r}/cherry-pick``.
    On success, returns the resulting commit hash on ``target_branch``
    inside a Reversible_Receipt (Requirement 13.3). On a 409 conflict
    response carrying ``errors[].conflicts``, returns a structured
    ``cherry_pick_conflict`` error with the conflicting paths
    (Requirement 13.2).

    Returns:
        JSON string with the created commit object and a ``receipt``
        containing the resulting commit hash, or a structured
        ``cherry_pick_conflict`` error on conflict.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check.
    if err := dc_guards.check_read_only(_CHERRY_PICK_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_cherry_pick_commit"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        picked = bb.cherry_pick_commit(
            project_key,
            repo_slug,
            source_commit=source_commit,
            target_branch=target_branch,
            message=message,
        )
    except CherryPickConflictError as exc:
        return json.dumps(
            {
                "success": False,
                "error_code": "cherry_pick_conflict",
                "message": str(exc),
                "details": {
                    "source_commit": source_commit,
                    "target_branch": target_branch,
                    "conflicts": exc.conflicts,
                },
            },
            indent=2,
        )
    except Exception as e:
        logger.error(f"Error cherry-picking commit: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})

    # Build Reversible_Receipt: the resulting commit hash on the target
    # branch is the ``id`` field of the upstream response. Cherry-pick
    # is not one-call-reversible, so ``inverse_tool`` / ``inverse_args``
    # are ``None`` and the ``note`` explains why.
    new_commit_id = picked.get("id") if isinstance(picked, dict) else None
    receipt = dc_guards.build_receipt(
        object_id=str(new_commit_id) if new_commit_id is not None else "",
        inverse_tool=None,
        inverse_args=None,
        note=(
            "Cherry-picks rewrite history on the target branch and are "
            "not retractable through a single inverse tool call; undo "
            "requires an out-of-band revert commit or branch surgery."
        ),
        recipient_scope={
            "source_commit": source_commit,
            "target_branch": target_branch,
        },
    )

    return json.dumps(
        {"success": True, "commit": picked, "receipt": receipt}, indent=2
    )

# =============================================================================
# Branching Model (toolset: bitbucket_branches, Req 14)
# =============================================================================
#
# Read-only accessor for the repository's branching-model configuration
# (development / production refs plus the feature / release / hotfix /
# bugfix prefix matchers) exposed by the Branch-Utils plugin at
# ``/rest/branch-utils/latest/projects/{k}/repos/{r}/branchmodel``.
#
# The tool runs the uniform pre-HTTP guard prelude defined in the
# ``atlassian-dc-tool-parity`` design:
#
#   1. ``check_read_only`` — no-op for read-tagged tools, included for
#      uniformity with the rest of the Bitbucket tool surface.
#   2. ``check_project_filter`` — reject any project key that falls
#      outside ``BITBUCKET_PROJECTS_FILTER`` before issuing the outbound
#      request (Requirement 43.1).
#
# Per Requirement 14.2, no Write_Tool that modifies the branching model
# is registered — changing prefixes or dev/prod pointers is an admin
# action that can invalidate in-flight PRs and stays in the Bitbucket UI.


_BRANCHING_MODEL_READ_TAGS: set[str] = {
    "bitbucket",
    "read",
    "toolset:bitbucket_branches",
}


@bitbucket_mcp.tool(
    tags={"bitbucket", "read", "toolset:bitbucket_branches"},
    annotations={"title": "Get Branching Model", "readOnlyHint": True},
)
async def get_branching_model(
    ctx: Context,
    project_key: Annotated[
        str, Field(description="The project key (e.g., 'PROJ')")
    ],
    repo_slug: Annotated[str, Field(description="The repository slug")],
) -> str:
    """Return the repository's branching-model configuration.

    Wraps ``GET /rest/branch-utils/latest/projects/{k}/repos/{r}/
    branchmodel``. The response contains the ``development`` /
    ``production`` branch references plus the prefix matchers for
    feature / release / hotfix / bugfix branch types (Requirement 14.1).

    Returns:
        JSON string with the ``branching_model`` object. Returns an
        empty object when the Branch-Utils plugin is disabled and the
        server responds with a non-dict payload.
    """
    bb = await get_bitbucket_fetcher(ctx)

    # 1. Read-only check (no-op for read tools; included for uniformity).
    if err := dc_guards.check_read_only(_BRANCHING_MODEL_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    # 2. Project filter.
    if err := dc_guards.check_project_filter(
        "bitbucket", project_key, bb.config.projects_filter
    ):
        return json.dumps({"success": False, **err.to_dict()})

    # 3. Mode guard — DC-only tool; short-circuit on Cloud with zero HTTP.
    if err := dc_guards.check_mode_supported(
        bb.is_cloud, "dc", "bitbucket_get_branching_model"
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        model = bb.get_branching_model(project_key, repo_slug)
        return json.dumps(
            {"success": True, "branching_model": model}, indent=2
        )
    except Exception as e:
        logger.error(f"Error getting branching model: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})
