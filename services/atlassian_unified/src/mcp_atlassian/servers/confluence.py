"""Confluence FastMCP server instance and tool definitions."""

import base64
import json
import logging
import mimetypes
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from mcp.types import BlobResourceContents, EmbeddedResource, ImageContent, TextContent
from pydantic import BeforeValidator, Field

from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.models.confluence import ConfluenceAttachment
from mcp_atlassian.servers.dependencies import get_confluence_fetcher
from mcp_atlassian.utils.decorators import (
    check_write_access,
)
from mcp_atlassian.utils.media import (
    ATTACHMENT_MAX_BYTES,
    fetch_and_encode_attachment,
    is_image_attachment,
)
from mcp_atlassian.utils.urls import resolve_relative_url

logger = logging.getLogger(__name__)


confluence_mcp = FastMCP(
    name="Confluence MCP Service",
    instructions="Provides tools for interacting with Atlassian Confluence.",
)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Search Content", "readOnlyHint": True},
)
async def search(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Search query - can be either a simple text (e.g. 'project documentation') or a CQL query string. "
                "Simple queries use 'siteSearch' by default, to mimic the WebUI search, with an automatic fallback "
                "to 'text' search if not supported. Examples of CQL:\n"
                "- Basic search: 'type=page AND space=DEV'\n"
                "- Personal space search: 'space=\"~username\"' (note: personal space keys starting with ~ must be quoted)\n"
                "- Search by title: 'title~\"Meeting Notes\"'\n"
                "- Use siteSearch: 'siteSearch ~ \"important concept\"'\n"
                "- Use text search: 'text ~ \"important concept\"'\n"
                "- Recent content: 'created >= \"2023-01-01\"'\n"
                "- Content with specific label: 'label=documentation'\n"
                "- Recently modified content: 'lastModified > startOfMonth(\"-1M\")'\n"
                "- Content modified this year: 'creator = currentUser() AND lastModified > startOfYear()'\n"
                "- Content you contributed to recently: 'contributor = currentUser() AND lastModified > startOfWeek()'\n"
                "- Content watched by user: 'watcher = \"user@domain.com\" AND type = page'\n"
                '- Exact phrase in content: \'text ~ "\\"Urgent Review Required\\"" AND label = "pending-approval"\'\n'
                '- Title wildcards: \'title ~ "Minutes*" AND (space = "HR" OR space = "Marketing")\'\n'
                'Note: Special identifiers need proper quoting in CQL: personal space keys (e.g., "~username"), '
                "reserved words, numeric IDs, and identifiers with special characters."
            )
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results (1-50)",
            default=10,
            ge=1,
            le=50,
        ),
    ] = 10,
    spaces_filter: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comma-separated list of space keys to filter results by. "
                "Overrides the environment variable CONFLUENCE_SPACES_FILTER if provided. "
                "Use empty string to disable filtering."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Search Confluence content using simple terms or CQL.

    Args:
        ctx: The FastMCP context.
        query: Search query - can be simple text or a CQL query string.
        limit: Maximum number of results (1-50).
        spaces_filter: Comma-separated list of space keys to filter by.

    Returns:
        JSON string representing a list of simplified Confluence page objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    # Check if the query is a simple search term or already a CQL query
    if query and not any(
        x in query for x in ["=", "~", ">", "<", " AND ", " OR ", "currentUser()"]
    ):
        original_query = query
        try:
            query = f'siteSearch ~ "{original_query}"'
            logger.info(
                f"Converting simple search term to CQL using siteSearch: {query}"
            )
            pages = confluence_fetcher.search(
                query, limit=limit, spaces_filter=spaces_filter
            )
        except Exception as e:
            logger.warning(f"siteSearch failed ('{e}'), falling back to text search.")
            query = f'text ~ "{original_query}"'
            logger.info(f"Falling back to text search with CQL: {query}")
            pages = confluence_fetcher.search(
                query, limit=limit, spaces_filter=spaces_filter
            )
    else:
        pages = confluence_fetcher.search(
            query, limit=limit, spaces_filter=spaces_filter
        )
    search_results = [page.to_simplified_dict() for page in pages]
    return json.dumps(search_results, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page", "readOnlyHint": True},
)
async def get_page(
    ctx: Context,
    page_id: Annotated[
        str | None,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be found in the page URL). "
                "For example, in the URL 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title', "
                "the page ID is '123456789'. "
                "Provide this OR both 'title' and 'space_key'. If page_id is provided, title and space_key will be ignored."
            ),
            default=None,
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    title: Annotated[
        str | None,
        Field(
            description=(
                "The exact title of the Confluence page. Use this with 'space_key' if 'page_id' is not known."
            ),
            default=None,
        ),
    ] = None,
    space_key: Annotated[
        str | None,
        Field(
            description=(
                "The key of the Confluence space where the page resides (e.g., 'DEV', 'TEAM'). Required if using 'title'."
            ),
            default=None,
        ),
    ] = None,
    include_metadata: Annotated[
        bool,
        Field(
            description="Whether to include page metadata such as creation date, last update, version, and labels.",
            default=True,
        ),
    ] = True,
    convert_to_markdown: Annotated[
        bool,
        Field(
            description=(
                "Whether to convert page to markdown (true) or keep it in raw HTML format (false). "
                "Raw HTML can reveal macros (like dates) not visible in markdown, but CAUTION: "
                "using HTML significantly increases token usage in AI responses."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """Get content of a specific Confluence page by its ID, or by its title and space key.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID. If provided, 'title' and 'space_key' are ignored.
        title: The exact title of the page. Must be used with 'space_key'.
        space_key: The key of the space. Must be used with 'title'.
        include_metadata: Whether to include page metadata.
        convert_to_markdown: Convert content to markdown (true) or keep raw HTML (false).

    Returns:
        JSON string representing the page content and/or metadata, or an error if not found or parameters are invalid.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    page_object = None

    if page_id:
        if title or space_key:
            logger.warning(
                "page_id was provided; title and space_key parameters will be ignored."
            )
        try:
            page_id_str = str(page_id)
            page_object = confluence_fetcher.get_page_content(
                page_id_str, convert_to_markdown=convert_to_markdown
            )
        except Exception as e:
            logger.error(f"Error fetching page by ID '{page_id}': {e}")
            return json.dumps(
                {"error": f"Failed to retrieve page by ID '{page_id}': {e}"},
                indent=2,
                ensure_ascii=False,
            )
    elif title and space_key:
        page_object = confluence_fetcher.get_page_by_title(
            space_key, title, convert_to_markdown=convert_to_markdown
        )
        if not page_object:
            return json.dumps(
                {
                    "error": f"Page with title '{title}' not found in space '{space_key}'."
                },
                indent=2,
                ensure_ascii=False,
            )
    else:
        raise ValueError(
            "Either 'page_id' OR both 'title' and 'space_key' must be provided."
        )

    if not page_object:
        return json.dumps(
            {"error": "Page not found with the provided identifiers."},
            indent=2,
            ensure_ascii=False,
        )

    if include_metadata:
        result = {"metadata": page_object.to_simplified_dict()}
    else:
        result = {"content": {"value": page_object.content}}

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page Children", "readOnlyHint": True},
)
async def get_page_children(
    ctx: Context,
    parent_id: Annotated[
        str,
        Field(
            description="The ID of the parent page whose children you want to retrieve"
        ),
    ],
    expand: Annotated[
        str,
        Field(
            description="Fields to expand in the response (e.g., 'version', 'body.storage')",
            default="version",
        ),
    ] = "version",
    limit: Annotated[
        int,
        Field(
            description="Maximum number of child items to return (1-50)",
            default=25,
            ge=1,
            le=50,
        ),
    ] = 25,
    include_content: Annotated[
        bool,
        Field(
            description="Whether to include the page content in the response",
            default=False,
        ),
    ] = False,
    convert_to_markdown: Annotated[
        bool,
        Field(
            description="Whether to convert page content to markdown (true) or keep it in raw HTML format (false). Only relevant if include_content is true.",
            default=True,
        ),
    ] = True,
    start: Annotated[
        int,
        Field(description="Starting index for pagination (0-based)", default=0, ge=0),
    ] = 0,
    include_folders: Annotated[
        bool,
        Field(
            description="Whether to include child folders in addition to child pages",
            default=True,
        ),
    ] = True,
) -> str:
    """Get child pages and folders of a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        parent_id: The ID of the parent page.
        expand: Fields to expand.
        limit: Maximum number of child items.
        include_content: Whether to include page content.
        convert_to_markdown: Convert content to markdown if include_content is true.
        start: Starting index for pagination.
        include_folders: Whether to include child folders (default: True).

    Returns:
        JSON string representing a list of child page and folder objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    if include_content and "body" not in expand:
        expand = f"{expand},body.storage" if expand else "body.storage"

    try:
        pages = confluence_fetcher.get_page_children(
            page_id=parent_id,
            start=start,
            limit=limit,
            expand=expand,
            convert_to_markdown=convert_to_markdown,
            include_folders=include_folders,
        )
        child_pages = [page.to_simplified_dict() for page in pages]
        result = {
            "parent_id": parent_id,
            "count": len(child_pages),
            "limit_requested": limit,
            "start_requested": start,
            "results": child_pages,
        }
    except Exception as e:
        logger.error(
            f"Error getting/processing children for page ID {parent_id}: {e}",
            exc_info=True,
        )
        result = {"error": f"Failed to get child pages: {e}"}

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Space Page Tree", "readOnlyHint": True},
)
async def get_space_page_tree(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(description="Space key"),
    ],
    limit: Annotated[
        int,
        Field(
            description="Max pages to fetch",
            default=100,
            ge=1,
            le=1000,
        ),
    ] = 100,
) -> str:
    """Get page hierarchy for a Confluence space as a flat list.

    Returns pages with parent_id and depth attributes for token-efficient
    processing. Filter by depth to focus on relevant sections, or find
    pages by title. Much more efficient than rendering full ASCII trees.

    Use this to understand space organization before creating/moving pages.

    Args:
        ctx: The FastMCP context.
        space_key: Space key identifier.
        limit: Maximum pages to fetch (start with 100 for faster results).

    Returns:
        JSON with space_key, total_pages, and pages array containing
        {id, title, parent_id, position, depth} for each page.
        Root pages have parent_id: null and depth: 0.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    tree_data = confluence_fetcher.get_space_page_tree(space_key=space_key, limit=limit)

    result: dict[str, object] = dict(tree_data)

    # has_more is computed by the fetcher from the API's _links.next signal
    if tree_data.get("has_more"):
        result["hint"] = (
            f"Results truncated at {limit} pages. Increase limit to see more."
        )

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_comments"},
    annotations={"title": "Get Comments", "readOnlyHint": True},
)
async def get_comments(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be parsed from URL, "
                "e.g. from 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title' "
                "-> '123456789')"
            )
        ),
    ],
) -> str:
    """Get comments for a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID.

    Returns:
        JSON string representing a list of comment objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    comments = confluence_fetcher.get_page_comments(page_id)
    formatted_comments = [comment.to_simplified_dict() for comment in comments]
    return json.dumps(formatted_comments, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_labels"},
    annotations={"title": "Get Labels", "readOnlyHint": True},
)
async def get_labels(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content ID (page, blog post, or attachment). "
                "For pages: numeric ID from URL (e.g., '123456789'). "
                "For attachments: ID with 'att' prefix (e.g., 'att123456789'). "
                "Works with any Confluence content type that supports labels."
            )
        ),
    ],
) -> str:
    """Get labels for Confluence content (pages, blog posts, or attachments).

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content ID (page or attachment).

    Returns:
        JSON string representing a list of label objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    labels = confluence_fetcher.get_page_labels(page_id)
    formatted_labels = [label.to_simplified_dict() for label in labels]
    return json.dumps(formatted_labels, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_labels"},
    annotations={"title": "Add Label", "destructiveHint": True},
)
@check_write_access
async def add_label(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content ID to label. "
                "For pages/blogs: numeric ID (e.g., '123456789'). "
                "For attachments: ID with 'att' prefix (e.g., 'att123456789'). "
                "Use get_attachments to find attachment IDs."
            )
        ),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "Label name to add (lowercase, no spaces). "
                "Examples: 'draft', 'reviewed', 'confidential', 'v1.0'. "
                "Labels help organize and categorize content."
            )
        ),
    ],
) -> str:
    """Add label to Confluence content (pages, blog posts, or attachments).

    Useful for:
    - Categorizing attachments (e.g., 'screenshot', 'diagram', 'legal-doc')
    - Tracking status (e.g., 'approved', 'needs-review', 'archived')
    - Filtering content by topic or version

    Args:
        ctx: The FastMCP context.
        page_id: Content ID (page or attachment).
        name: Label name to add.

    Returns:
        JSON string representing the updated list of label objects.

    Raises:
        ValueError: If in read-only mode or Confluence client is unavailable.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    labels = confluence_fetcher.add_page_label(page_id, name)
    formatted_labels = [label.to_simplified_dict() for label in labels]
    return json.dumps(formatted_labels, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_pages"},
    annotations={"title": "Create Page", "destructiveHint": True},
)
@check_write_access
async def create_page(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(
            description="The key of the space to create the page in (usually a short uppercase code like 'DEV', 'TEAM', or 'DOC')"
        ),
    ],
    title: Annotated[str, Field(description="The title of the page")],
    content: Annotated[
        str,
        Field(
            description="The content of the page. Format depends on content_format parameter. Can be Markdown (default), wiki markup, or storage format"
        ),
    ],
    parent_id: Annotated[
        str | None,
        Field(
            description="(Optional) parent page ID. If provided, this page will be created as a child of the specified page",
            default=None,
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    content_format: Annotated[
        str,
        Field(
            description="(Optional) The format of the content parameter. Options: 'markdown' (default), 'wiki', or 'storage'. Wiki format uses Confluence wiki markup syntax",
            default="markdown",
        ),
    ] = "markdown",
    enable_heading_anchors: Annotated[
        bool,
        Field(
            description="(Optional) Whether to enable automatic heading anchor generation. Only applies when content_format is 'markdown'",
            default=False,
        ),
    ] = False,
    include_content: Annotated[
        bool,
        Field(
            description="(Optional) Whether to include page content in the response. Defaults to false since callers already have the content at create time",
            default=False,
        ),
    ] = False,
    emoji: Annotated[
        str | None,
        Field(
            description="(Optional) Page title emoji (icon shown in navigation). Can be any emoji character like '📝', '🚀', '📚'. Set to null/None to remove.",
            default=None,
        ),
    ] = None,
) -> str:
    """Create a new Confluence page.

    Args:
        ctx: The FastMCP context.
        space_key: The key of the space.
        title: The title of the page.
        content: The content of the page (format depends on content_format).
        parent_id: Optional parent page ID.
        content_format: The format of the content ('markdown', 'wiki', or 'storage').
        enable_heading_anchors: Whether to enable heading anchors (markdown only).
        include_content: Whether to include page content in the response.
        emoji: Optional page title emoji (icon shown in navigation).

    Returns:
        JSON string representing the created page object.

    Raises:
        ValueError: If in read-only mode, Confluence client is unavailable, or invalid content_format.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # Validate content_format
    if content_format not in ["markdown", "wiki", "storage"]:
        raise ValueError(
            f"Invalid content_format: {content_format}. Must be 'markdown', 'wiki', or 'storage'"
        )

    # Determine parameters based on content format
    if content_format == "markdown":
        is_markdown = True
        content_representation = None  # Will be converted to storage
    else:
        is_markdown = False
        content_representation = content_format  # Pass 'wiki' or 'storage' directly

    page = confluence_fetcher.create_page(
        space_key=space_key,
        title=title,
        body=content,
        parent_id=parent_id,
        is_markdown=is_markdown,
        enable_heading_anchors=enable_heading_anchors
        if content_format == "markdown"
        else False,
        content_representation=content_representation,
        emoji=emoji,
    )
    result = page.to_simplified_dict()
    if not include_content:
        result.pop("content", None)
    return json.dumps(
        {"message": "Page created successfully", "page": result},
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_pages"},
    annotations={"title": "Update Page", "destructiveHint": True},
)
@check_write_access
async def update_page(
    ctx: Context,
    page_id: Annotated[str, Field(description="The ID of the page to update")],
    title: Annotated[str, Field(description="The new title of the page")],
    content: Annotated[
        str,
        Field(
            description="The new content of the page. Format depends on content_format parameter"
        ),
    ],
    is_minor_edit: Annotated[
        bool, Field(description="Whether this is a minor edit", default=False)
    ] = False,
    version_comment: Annotated[
        str | None, Field(description="Optional comment for this version", default=None)
    ] = None,
    parent_id: Annotated[
        str | None,
        Field(description="Optional the new parent page ID", default=None),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    content_format: Annotated[
        str,
        Field(
            description="(Optional) The format of the content parameter. Options: 'markdown' (default), 'wiki', or 'storage'. Wiki format uses Confluence wiki markup syntax",
            default="markdown",
        ),
    ] = "markdown",
    enable_heading_anchors: Annotated[
        bool,
        Field(
            description="(Optional) Whether to enable automatic heading anchor generation. Only applies when content_format is 'markdown'",
            default=False,
        ),
    ] = False,
    include_content: Annotated[
        bool,
        Field(
            description="(Optional) Whether to include page content in the response. Defaults to false since callers already have the content at update time",
            default=False,
        ),
    ] = False,
    emoji: Annotated[
        str | None,
        Field(
            description="(Optional) Page title emoji (icon shown in navigation). Can be any emoji character like '📝', '🚀', '📚'. Set to null/None to remove.",
            default=None,
        ),
    ] = None,
) -> str:
    """Update an existing Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to update.
        title: The new title of the page.
        content: The new content of the page (format depends on content_format).
        is_minor_edit: Whether this is a minor edit.
        version_comment: Optional comment for this version.
        parent_id: Optional new parent page ID.
        content_format: The format of the content ('markdown', 'wiki', or 'storage').
        enable_heading_anchors: Whether to enable heading anchors (markdown only).
        include_content: Whether to include page content in the response.
        emoji: Optional page title emoji (icon shown in navigation).

    Returns:
        JSON string representing the updated page object.

    Raises:
        ValueError: If Confluence client is not configured, available, or invalid content_format.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # Validate content_format
    if content_format not in ["markdown", "wiki", "storage"]:
        raise ValueError(
            f"Invalid content_format: {content_format}. Must be 'markdown', 'wiki', or 'storage'"
        )

    # Determine parameters based on content format
    if content_format == "markdown":
        is_markdown = True
        content_representation = None  # Will be converted to storage
    else:
        is_markdown = False
        content_representation = content_format  # Pass 'wiki' or 'storage' directly

    updated_page = confluence_fetcher.update_page(
        page_id=page_id,
        title=title,
        body=content,
        is_minor_edit=is_minor_edit,
        version_comment=version_comment,
        is_markdown=is_markdown,
        parent_id=parent_id,
        enable_heading_anchors=enable_heading_anchors
        if content_format == "markdown"
        else False,
        content_representation=content_representation,
        emoji=emoji,
    )
    page_data = updated_page.to_simplified_dict()
    if not include_content:
        page_data.pop("content", None)
    return json.dumps(
        {"message": "Page updated successfully", "page": page_data},
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_pages"},
    annotations={"title": "Delete Page", "destructiveHint": True},
)
@check_write_access
async def delete_page(
    ctx: Context,
    page_id: Annotated[str, Field(description="The ID of the page to delete")],
) -> str:
    """Delete an existing Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to delete.

    Returns:
        JSON string indicating success or failure.

    Raises:
        ValueError: If Confluence client is not configured or available.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        result = confluence_fetcher.delete_page(page_id=page_id)
        if result:
            response = {
                "success": True,
                "message": f"Page {page_id} deleted successfully",
            }
        else:
            response = {
                "success": False,
                "message": f"Unable to delete page {page_id}. API request completed but deletion unsuccessful.",
            }
    except Exception as e:
        logger.error(f"Error deleting Confluence page {page_id}: {str(e)}")
        response = {
            "success": False,
            "message": f"Error deleting page {page_id}",
            "error": str(e),
        }

    return json.dumps(response, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_pages"},
    annotations={"title": "Move Page", "destructiveHint": True},
)
@check_write_access
async def move_page(
    ctx: Context,
    page_id: Annotated[str, Field(description="ID of the page to move")],
    target_parent_id: Annotated[
        str,
        Field(
            description=(
                "Content ID of the target parent page. The moved page "
                "becomes a child of (or a sibling adjacent to) this page "
                "depending on the ``position`` argument."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    position: Annotated[
        str,
        Field(
            description=(
                "Position of ``page_id`` relative to ``target_parent_id``: "
                "``'append'`` (default — move under the target as its last "
                "child), ``'above'`` (sibling ordered before the target), "
                "or ``'below'`` (sibling ordered after the target)."
            ),
            default="append",
        ),
    ] = "append",
) -> str:
    """Move a Confluence page to a new parent using the DC long-task endpoint.

    Write_Tool for Requirements 31.1 and 31.4. Wraps
    ``PUT /rest/api/content/{page_id}/move/{position}/{target_parent_id}``
    via :meth:`PageMoveCopyMixin.move_page`. The DC endpoint is synchronous
    for trivial moves within the same space and returns a long-task
    descriptor ``{"longTaskId": "..."}`` for larger moves. When a long-task
    id is present, this tool surfaces it under ``long_task_id`` so callers
    can poll progress via ``confluence_get_long_task`` (Req 31.4).

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped — the page's space key cannot be resolved
    cheaply from the content id alone without an extra HTTP round-trip,
    which Req 43 explicitly permits for content-id-only endpoints.

    Args:
        ctx: The FastMCP context.
        page_id: Content id of the page to move.
        target_parent_id: Content id of the destination parent page.
        position: Where to place ``page_id`` relative to
            ``target_parent_id`` (``'append'``, ``'above'``, or
            ``'below'``).

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "target_parent_id": ..., "position": ..., "long_task_id": ... |
        None, "response": ...}`` on success. ``long_task_id`` is the
        DC-returned ``longTaskId`` when the move is asynchronous, or
        ``None`` when DC completed the move synchronously. On failure the
        tool returns ``{"success": False, "error_code": ..., ...}`` for
        guard rejections or ``{"success": False, "error": ...}`` for
        upstream errors.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_pages"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        response = confluence.move_page(
            page_id,
            target_parent_id=target_parent_id,
            position=position,
        )
    except Exception as e:
        logger.error(
            f"Error moving Confluence page {page_id!r} to parent "
            f"{target_parent_id!r} (position={position!r}): {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    long_task_id = response.get("longTaskId") if isinstance(response, dict) else None

    return json.dumps(
        {
            "success": True,
            "page_id": page_id,
            "target_parent_id": target_parent_id,
            "position": position,
            "long_task_id": long_task_id,
            "response": response,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_pages"},
    annotations={"title": "Copy Page Tree", "destructiveHint": True},
)
@check_write_access
async def copy_page_tree(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Content ID of the root page whose subtree should be copied."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    target_parent_id: Annotated[
        str,
        Field(
            description=(
                "Content ID of the page under which the copied subtree "
                "should be attached. Must not equal ``page_id`` and must "
                "not be a descendant of ``page_id``; otherwise the tool "
                "returns a structured ``invalid_target`` error before "
                "issuing any write call (Req 31.3)."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    title_prefix: Annotated[
        str | None,
        Field(
            description=(
                "Optional string prepended to every copied page's title. "
                "DC auto-resolves title collisions when this is omitted."
            ),
            default=None,
        ),
    ] = None,
    copy_permissions: Annotated[
        bool,
        Field(
            description=(
                "Copy per-page read/update restrictions from each source "
                "page to its target. Defaults to False so the copy starts "
                "with the destination space's normal permissions."
            ),
            default=False,
        ),
    ] = False,
    copy_attachments: Annotated[
        bool,
        Field(
            description=(
                "Copy each source page's attachments alongside its storage "
                "body. Defaults to True."
            ),
            default=True,
        ),
    ] = True,
    copy_labels: Annotated[
        bool,
        Field(
            description=(
                "Copy each source page's labels to its target. Defaults to "
                "False so the copied tree starts with a clean label set."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """Copy a Confluence page and its descendants under a new parent.

    Write_Tool for Requirements 31.2, 31.3, and 31.4. Wraps
    ``POST /rest/api/content/{page_id}/pagehierarchy/copy`` via
    :meth:`PageMoveCopyMixin.copy_page_tree`. The mixin performs an
    ancestor-of-self precheck via
    ``GET /rest/api/content/{target_parent_id}?expand=ancestors`` *before*
    issuing the POST — if ``target_parent_id`` equals ``page_id`` or
    appears in the target's ancestor chain, the mixin raises
    :class:`ValueError` with an ``invalid_target:`` prefix and zero write
    HTTP is issued (Req 31.3). This tool maps that prefix to the
    structured ``invalid_target`` error envelope listed in the feature's
    error-code allowlist.

    For non-trivial trees DC returns a long-task descriptor
    ``{"longTaskId": "..."}``; this tool surfaces it under
    ``long_task_id`` so callers can poll progress via
    ``confluence_get_long_task`` (Req 31.4).

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) because the page's space key cannot be
    resolved cheaply from the content id alone.

    Args:
        ctx: The FastMCP context.
        page_id: Content id of the root page whose subtree should be
            copied.
        target_parent_id: Content id of the destination parent page.
        title_prefix: Optional prefix prepended to every copied page's
            title.
        copy_permissions: Copy per-page restrictions from source to
            target.
        copy_attachments: Copy source attachments to targets.
        copy_labels: Copy source labels to targets.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "target_parent_id": ..., "long_task_id": ... | None, "response":
        ...}`` on success. ``long_task_id`` is the DC-returned
        ``longTaskId`` when the copy is asynchronous, or ``None`` when DC
        completed the copy synchronously. On ancestor-of-self rejection
        the tool returns ``{"success": False, "error_code":
        "invalid_target", "message": ..., "details": {"page_id": ...,
        "target_parent_id": ...}}`` with no write HTTP issued. On other
        failures it returns ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import StructuredError, check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_pages"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        response = confluence.copy_page_tree(
            page_id,
            target_parent_id=target_parent_id,
            title_prefix=title_prefix,
            copy_permissions=copy_permissions,
            copy_attachments=copy_attachments,
            copy_labels=copy_labels,
        )
    except ValueError as ve:
        message = str(ve)
        if message.startswith("invalid_target:"):
            err = StructuredError(
                error_code="invalid_target",
                message=message.split("invalid_target:", 1)[1].strip()
                or "Target is ancestor-of-self",
                details={
                    "page_id": page_id,
                    "target_parent_id": target_parent_id,
                },
            )
            return json.dumps({"success": False, **err.to_dict()})
        # Any other ValueError (e.g. validation errors from the mixin)
        # is surfaced without the structured-error envelope.
        logger.error(
            f"Validation error copying page tree {page_id!r} to parent "
            f"{target_parent_id!r}: {ve}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": message})
    except Exception as e:
        logger.error(
            f"Error copying page tree {page_id!r} to parent "
            f"{target_parent_id!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    long_task_id = response.get("longTaskId") if isinstance(response, dict) else None

    return json.dumps(
        {
            "success": True,
            "page_id": page_id,
            "target_parent_id": target_parent_id,
            "long_task_id": long_task_id,
            "response": response,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_comments"},
    annotations={"title": "Add Comment", "destructiveHint": True},
)
@check_write_access
async def add_comment(
    ctx: Context,
    page_id: Annotated[
        str, Field(description="The ID of the page to add a comment to")
    ],
    body: Annotated[str, Field(description="The comment content in Markdown format")],
) -> str:
    """Add a comment to a Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: The ID of the page to add a comment to.
        body: The comment content in Markdown format.

    Returns:
        JSON string representing the created comment.

    Raises:
        ValueError: If in read-only mode or Confluence client is unavailable.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        comment = confluence_fetcher.add_comment(page_id=page_id, content=body)
        if comment:
            comment_data = comment.to_simplified_dict()
            response = {
                "success": True,
                "message": "Comment added successfully",
                "comment": comment_data,
            }
        else:
            response = {
                "success": False,
                "message": f"Unable to add comment to page {page_id}. API request completed but comment creation unsuccessful.",
            }
    except Exception as e:
        logger.error(f"Error adding comment to Confluence page {page_id}: {str(e)}")
        response = {
            "success": False,
            "message": f"Error adding comment to page {page_id}",
            "error": str(e),
        }

    return json.dumps(response, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_comments"},
    annotations={"title": "Reply to Comment", "destructiveHint": True},
)
@check_write_access
async def reply_to_comment(
    ctx: Context,
    comment_id: Annotated[
        str, Field(description="The ID of the parent comment to reply to")
    ],
    body: Annotated[str, Field(description="The reply content in Markdown format")],
) -> str:
    """Reply to an existing comment thread on a Confluence page.

    Args:
        ctx: The FastMCP context.
        comment_id: The ID of the parent comment to reply to.
        body: The reply content in Markdown format.

    Returns:
        JSON string representing the created reply comment.

    Raises:
        ValueError: If in read-only mode or Confluence client is unavailable.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        comment = confluence_fetcher.reply_to_comment(
            comment_id=comment_id, content=body
        )
        if comment:
            comment_data = comment.to_simplified_dict()
            response = {
                "success": True,
                "message": "Reply added successfully",
                "comment": comment_data,
            }
        else:
            response = {
                "success": False,
                "message": f"Unable to reply to comment {comment_id}. API request completed but reply creation unsuccessful.",
            }
    except Exception as e:
        logger.error(f"Error replying to comment {comment_id}: {str(e)}")
        response = {
            "success": False,
            "message": f"Error replying to comment {comment_id}",
            "error": str(e),
        }

    return json.dumps(response, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_users"},
    annotations={"title": "Search User", "readOnlyHint": True},
)
async def search_user(
    ctx: Context,
    query: Annotated[
        str,
        Field(
            description=(
                "Search query - a CQL query string for user search. "
                "Examples of CQL:\n"
                "- Basic user lookup by full name: 'user.fullname ~ \"First Last\"'\n"
                'Note: Special identifiers need proper quoting in CQL: personal space keys (e.g., "~username"), '
                "reserved words, numeric IDs, and identifiers with special characters."
            )
        ),
    ],
    limit: Annotated[
        int,
        Field(
            description="Maximum number of results (1-50)",
            default=10,
            ge=1,
            le=50,
        ),
    ] = 10,
    group_name: Annotated[
        str,
        Field(
            description=(
                "Group to search within on Server/DC instances "
                "(default: 'confluence-users'). "
                "Ignored on Cloud."
            ),
            default="confluence-users",
        ),
    ] = "confluence-users",
) -> str:
    """Search Confluence users using CQL (Cloud) or group member API (Server/DC).

    Args:
        ctx: The FastMCP context.
        query: Search query - a CQL query string for user search.
        limit: Maximum number of results (1-50).
        group_name: Group to search within on Server/DC.

    Returns:
        JSON string representing a list of simplified Confluence user search result objects.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    # If the query doesn't look like CQL, wrap it as a user fullname search
    if query and not any(
        x in query for x in ["=", "~", ">", "<", " AND ", " OR ", "user."]
    ):
        # Simple search term - search by fullname
        query = f'user.fullname ~ "{query}"'
        logger.info(f"Converting simple search term to user CQL: {query}")

    try:
        user_results = confluence_fetcher.search_user(
            query, limit=limit, group_name=group_name
        )
        search_results = [user.to_simplified_dict() for user in user_results]
        return json.dumps(search_results, indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error during user search: {e}", exc_info=False)
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error searching users: {str(e)}")
        return json.dumps(
            {
                "error": f"An unexpected error occurred while searching for users: {str(e)}"
            },
            indent=2,
            ensure_ascii=False,
        )


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page History", "readOnlyHint": True},
)
async def get_page_history(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be found in the page URL). "
                "For example, in 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title', "
                "the page ID is '123456789'."
            )
        ),
    ],
    version: Annotated[
        int,
        Field(
            description="The version number of the page to retrieve",
            ge=1,
        ),
    ],
    convert_to_markdown: Annotated[
        bool,
        Field(
            description=(
                "Whether to convert page to markdown (true) or keep it in raw HTML format (false). "
                "Raw HTML can reveal macros (like dates) not visible in markdown, but CAUTION: "
                "using HTML significantly increases token usage in AI responses."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """Get a historical version of a specific Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID.
        version: The version number to retrieve.
        convert_to_markdown: Convert content to markdown (true) or keep raw HTML (false).

    Returns:
        JSON string representing the page content at the specified version.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        page = confluence_fetcher.get_page_history(
            page_id=page_id,
            version=version,
            convert_to_markdown=convert_to_markdown,
        )
        result = page.to_simplified_dict()
        return json.dumps(result, indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error getting page history: {e}")
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error getting page history for page {page_id} version {version}: {e}"
        )
        return json.dumps(
            {
                "error": f"Failed to get page history: {e}",
                "page_id": page_id,
                "version": version,
            },
            indent=2,
            ensure_ascii=False,
        )


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page Version Diff", "readOnlyHint": True},
)
async def get_page_diff(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be found in the page URL). "
                "For example, in 'https://example.atlassian.net/wiki/spaces/TEAM/"
                "pages/123456789/Page+Title', the page ID is '123456789'."
            )
        ),
    ],
    from_version: Annotated[
        int,
        Field(
            description="Source version number",
            ge=1,
        ),
    ],
    to_version: Annotated[
        int,
        Field(
            description="Target version number",
            ge=1,
        ),
    ],
) -> str:
    """Get a unified diff between two versions of a Confluence page.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page ID.
        from_version: Source version number.
        to_version: Target version number.

    Returns:
        JSON string with page info and unified diff.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        result = confluence_fetcher.get_page_version_diff(
            page_id=page_id,
            from_version=from_version,
            to_version=to_version,
        )
        return json.dumps(result, indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error getting page diff: {e}")
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error getting diff for page {page_id} "
            f"(v{from_version} -> v{to_version}): {e}"
        )
        return json.dumps(
            {
                "error": f"Failed to get page diff: {e}",
                "page_id": page_id,
                "from_version": from_version,
                "to_version": to_version,
            },
            indent=2,
            ensure_ascii=False,
        )


@confluence_mcp.tool(
    tags={"confluence", "read", "analytics", "toolset:confluence_analytics"},
    annotations={"title": "Get Page Views", "readOnlyHint": True},
)
async def get_page_views(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID, can be found in the page URL). "
                "For example, in 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title', "
                "the page ID is '123456789'."
            )
        ),
    ],
    include_title: Annotated[
        bool,
        Field(description="Whether to fetch and include the page title"),
    ] = True,
) -> str:
    """Get view statistics for a Confluence page.

    Note: This tool is only available for Confluence Cloud. Server/Data Center
    instances do not support the Analytics API.

    Args:
        ctx: The FastMCP context.
        page_id: The Confluence page ID.
        include_title: Whether to include the page title in the response.

    Returns:
        JSON string with page view statistics including total views and last viewed date.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    try:
        result = confluence_fetcher.get_page_views(
            page_id=page_id,
            include_title=include_title,
        )
        return json.dumps(result.to_simplified_dict(), indent=2, ensure_ascii=False)
    except MCPAtlassianAuthenticationError as e:
        logger.error(f"Authentication error getting page views: {e}")
        return json.dumps(
            {
                "error": "Authentication failed. Please check your credentials.",
                "details": str(e),
            },
            indent=2,
            ensure_ascii=False,
        )
    except ValueError as e:
        logger.error(f"Error getting page views for {page_id}: {e}")
        return json.dumps(
            {"error": str(e), "page_id": page_id},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Unexpected error getting page views for {page_id}: {e}")
        return json.dumps(
            {"error": f"Failed to get page views: {e}", "page_id": page_id},
            indent=2,
            ensure_ascii=False,
        )


# ===== Attachment Operations =====


@confluence_mcp.tool(
    tags={"confluence", "write", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Upload Attachment", "destructiveHint": True},
)
@check_write_access
async def upload_attachment(
    ctx: Context,
    content_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the Confluence content (page or blog post) to attach the file to. "
                "Page IDs can be found in the page URL or by using the search/get_page tools. "
                "Example: '123456789'"
            )
        ),
    ],
    file_path: Annotated[
        str,
        Field(
            description=(
                "Full path to the file to upload. Can be absolute (e.g., '/home/user/document.pdf' or 'C:\\Users\\name\\file.docx') "
                "or relative to the current working directory (e.g., './uploads/document.pdf'). "
                "If a file with the same name already exists, a new version will be created."
            )
        ),
    ],
    comment: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) A comment describing this attachment or version. "
                "Visible in the attachment history. Example: 'Updated Q4 2024 figures'"
            ),
            default=None,
        ),
    ] = None,
    minor_edit: Annotated[
        bool,
        Field(
            description=(
                "(Optional) Whether this is a minor edit. If true, watchers are not notified. "
                "Default is false."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """Upload an attachment to Confluence content (page or blog post).

    If the attachment already exists (same filename), a new version is created.
    This is useful for:
    - Attaching documents, images, or files to a page
    - Updating existing attachments with new versions
    - Adding supporting materials to documentation

    Args:
        ctx: The FastMCP context.
        content_id: The ID of the content to attach to.
        file_path: Path to the file to upload.
        comment: Optional comment for the attachment.
        minor_edit: Whether this is a minor edit (no notifications).

    Returns:
        JSON string with upload confirmation and attachment metadata.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    result = confluence_fetcher.upload_attachment(
        content_id=content_id,
        file_path=file_path,
        comment=comment,
        minor_edit=minor_edit,
    )

    return json.dumps(
        {"message": "Attachment uploaded successfully", "attachment": result},
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Upload Multiple Attachments", "destructiveHint": True},
)
@check_write_access
async def upload_attachments(
    ctx: Context,
    content_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the Confluence content (page or blog post) to attach files to. "
                "Example: '123456789'. If uploading multiple files with the same names, "
                "new versions will be created automatically."
            )
        ),
    ],
    file_paths: Annotated[
        str,
        Field(
            description=(
                "Comma-separated list of file paths to upload. Can be absolute or relative paths. "
                "Examples: './file1.pdf,./file2.png' or 'C:\\docs\\report.docx,D:\\image.jpg'. "
                "All files uploaded with same comment/minor_edit settings."
            )
        ),
    ],
    comment: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Comment for all uploaded attachments. Visible in version history. "
                "Example: 'Q4 2024 batch upload'"
            ),
            default=None,
        ),
    ] = None,
    minor_edit: Annotated[
        bool,
        Field(
            description=(
                "(Optional) Whether this is a minor edit. If true, watchers are not notified. "
                "Default is false."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """Upload multiple attachments to Confluence content in a single operation.

    More efficient than calling upload_attachment multiple times. If files with the
    same names exist, new versions are created automatically.

    Useful for:
    - Bulk uploading documentation assets (diagrams, screenshots, etc.)
    - Adding multiple related files to a page at once
    - Batch updating existing attachments with new versions

    Args:
        ctx: The FastMCP context.
        content_id: The ID of the content to attach to.
        file_paths: List of file paths to upload.
        comment: Optional comment for the attachments.
        minor_edit: Whether this is a minor edit.

    Returns:
        JSON string with upload results for each file.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    paths_list = [p.strip() for p in file_paths.split(",") if p.strip()]

    results = confluence_fetcher.upload_attachments(
        content_id=content_id,
        file_paths=paths_list,
        comment=comment,
        minor_edit=minor_edit,
    )

    return json.dumps(
        {
            "message": f"Uploaded {len(results)} attachment(s) successfully",
            "attachments": results,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "read", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Get Content Attachments", "readOnlyHint": True},
)
async def get_attachments(
    ctx: Context,
    content_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the Confluence content (page or blog post) to list attachments for. "
                "Example: '123456789'"
            )
        ),
    ],
    start: Annotated[
        int,
        Field(
            description=(
                "(Optional) Starting index for pagination. Use 0 for the first page. "
                "To get the next page, add the 'limit' value to 'start'. Default: 0"
            ),
            default=0,
        ),
    ] = 0,
    limit: Annotated[
        int,
        Field(
            description=(
                "(Optional) Maximum number of attachments to return per request (1-100). "
                "Use pagination (start/limit) for large attachment lists. Default: 50"
            ),
            default=50,
            ge=1,
            le=100,
        ),
    ] = 50,
    filename: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter results to only attachments matching this filename. "
                "Exact match only. Example: 'report.pdf'"
            ),
            default=None,
        ),
    ] = None,
    media_type: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Filter by MIME type. "
                "**Note**: Confluence API returns 'application/octet-stream' for most binary files "
                "(PNG, JPG, PDF) instead of specific MIME types like 'image/png'. "
                "For more reliable filtering, use the 'filename' parameter. "
                "Examples: 'application/octet-stream' (binary files), 'application/pdf', "
                "'application/vnd.openxmlformats-officedocument.wordprocessingml.document' (for .docx)"
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """List all attachments for a Confluence content item (page or blog post).

    Returns metadata about attachments including:
    - Attachment ID, title, and file type
    - File size and download URL
    - Creation/modification dates
    - Version information

    **Important**: Confluence API returns 'application/octet-stream' as the media type
    for most binary files (PNG, JPG, PDF) instead of specific types like 'image/png'.
    For filtering by file type, using the 'filename' parameter is more reliable
    (e.g., filename='*.png' pattern matching if supported, or exact filename).

    Useful for:
    - Discovering what files are attached to a page
    - Getting attachment IDs for download operations
    - Checking if a specific file exists
    - Listing images/documents for processing

    Args:
        ctx: The FastMCP context.
        content_id: The ID of the content.
        start: Starting index for pagination.
        limit: Maximum number of results (1-100).
        filename: Optional exact filename filter.
        media_type: Optional MIME type filter (note: most binaries return 'application/octet-stream').

    Returns:
        JSON string with list of attachments and metadata.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    result = confluence_fetcher.get_content_attachments(
        content_id=content_id,
        start=start,
        limit=limit,
        filename=filename,
        media_type=media_type,
    )

    return json.dumps(result, indent=2, ensure_ascii=False)


@confluence_mcp.tool(
    tags={"confluence", "read", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Download Attachment", "readOnlyHint": True},
)
async def download_attachment(
    ctx: Context,
    attachment_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the attachment to download (e.g., 'att123456789'). "
                "Find attachment IDs using get_attachments tool. "
                "Example workflow: get_attachments(content_id) → use returned ID here."
            )
        ),
    ],
) -> TextContent | EmbeddedResource:
    """Download an attachment from Confluence as an embedded resource.

    Returns the attachment content as a base64-encoded embedded resource so
    that it is available over the MCP protocol without requiring filesystem
    access on the server. Files larger than 50 MB are not downloaded inline;
    a descriptive error message is returned instead.

    Args:
        ctx: The FastMCP context.
        attachment_id: The ID of the attachment.

    Returns:
        An EmbeddedResource with base64-encoded content, or a TextContent
        with an error or size-exceeded message.
    """

    confluence_fetcher = await get_confluence_fetcher(ctx)

    try:
        v2_adapter = confluence_fetcher._v2_adapter

        if v2_adapter:
            attachment_data = v2_adapter.get_attachment_by_id(attachment_id)
        else:
            base_url = confluence_fetcher.config.url.rstrip("/")
            url = f"{base_url}/rest/api/content/{attachment_id}"
            resp_meta = confluence_fetcher.confluence._session.get(url)
            resp_meta.raise_for_status()
            attachment_data = resp_meta.json()

        download_url = attachment_data.get("_links", {}).get("download")
        if not download_url:
            return TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": (
                            f"Could not find download URL for attachment {attachment_id}"
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        download_url = resolve_relative_url(download_url, confluence_fetcher.config.url)

        filename = attachment_data.get("title") or attachment_id
        mime_type = (
            attachment_data.get("extensions", {}).get("mediaType")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        file_size = attachment_data.get("extensions", {}).get("fileSize")

        if file_size is not None and file_size > ATTACHMENT_MAX_BYTES:
            return TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "file_size": file_size,
                        "error": (
                            f"Attachment '{filename}' is {file_size} bytes which exceeds "
                            "the 50 MB inline limit. Retrieve it directly from Confluence."
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        data_bytes = confluence_fetcher.fetch_attachment_content(download_url)
        if data_bytes is None:
            return TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": (f"Failed to download attachment {attachment_id}"),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        if len(data_bytes) > ATTACHMENT_MAX_BYTES:
            return TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "file_size": len(data_bytes),
                        "error": (
                            f"Attachment '{filename}' is {len(data_bytes)} bytes which "
                            "exceeds the 50 MB inline limit. Retrieve it directly from "
                            "Confluence."
                        ),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )

        encoded = base64.b64encode(data_bytes).decode("ascii")
        return EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"attachment:///{attachment_id}/{filename}",
                mimeType=mime_type,
                blob=encoded,
            ),
        )

    except Exception as e:
        return TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": False,
                    "error": f"Error downloading attachment: {str(e)}",
                },
                indent=2,
                ensure_ascii=False,
            ),
        )


@confluence_mcp.tool(
    tags={"confluence", "read", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Download All Content Attachments", "readOnlyHint": True},
)
async def download_content_attachments(
    ctx: Context,
    content_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the Confluence content (page or blog post) to download attachments from. "
                "Example: '123456789'"
            )
        ),
    ],
) -> list[TextContent | EmbeddedResource]:
    """Download all attachments for a Confluence content item as embedded resources.

    Returns attachment contents as base64-encoded embedded resources so that
    they are available over the MCP protocol without requiring filesystem
    access on the server. Files larger than 50 MB are skipped with an error
    entry in the summary.

    Args:
        ctx: The FastMCP context.
        content_id: The ID of the content.

    Returns:
        A list with a text summary followed by one EmbeddedResource per
        successfully downloaded attachment.
    """

    confluence_fetcher = await get_confluence_fetcher(ctx)
    contents: list[TextContent | EmbeddedResource] = []

    attachments_result = confluence_fetcher.get_content_attachments(content_id)

    if not attachments_result.get("success"):
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(attachments_result, indent=2, ensure_ascii=False),
            )
        )
        return contents

    attachment_data = attachments_result.get("attachments", [])

    if not attachment_data:
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "content_id": content_id,
                        "message": f"No attachments found for content {content_id}",
                        "downloaded": 0,
                        "failed": [],
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
        )
        return contents

    fetched: list[dict[str, object]] = []
    failed: list[dict[str, object]] = []

    for att_dict in attachment_data:
        if not isinstance(att_dict, dict):
            continue
        attachment = ConfluenceAttachment.from_api_response(att_dict)

        if not attachment.download_url:
            failed.append(
                {
                    "filename": attachment.title or "unknown",
                    "error": "No download URL available",
                }
            )
            continue

        filename = attachment.title or "unknown"

        if (
            attachment.file_size is not None
            and attachment.file_size > ATTACHMENT_MAX_BYTES
        ):
            failed.append(
                {
                    "filename": filename,
                    "error": (
                        f"File is {attachment.file_size} bytes "
                        "which exceeds the 50 MB inline limit."
                    ),
                }
            )
            continue

        download_url = resolve_relative_url(
            attachment.download_url, confluence_fetcher.config.url
        )

        encoded, mime_type, fetched_bytes = fetch_and_encode_attachment(
            fetch_fn=confluence_fetcher.fetch_attachment_content,
            url=download_url,
            filename=filename,
            mime_type=attachment.media_type,
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
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"attachment:///{content_id}/{filename}",
                    mimeType=mime_type,
                    blob=encoded,
                ),
            )
        )

    summary: dict[str, object] = {
        "success": True,
        "content_id": content_id,
        "total": len(attachment_data),
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


@confluence_mcp.tool(
    tags={"confluence", "write", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Delete Attachment", "destructiveHint": True},
)
@check_write_access
async def delete_attachment(
    ctx: Context,
    attachment_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the attachment to delete. Attachment IDs can be found using the "
                "get_attachments tool. Example: 'att123456789'. "
                "**Warning**: This permanently deletes the attachment and all its versions."
            )
        ),
    ],
) -> str:
    """Permanently delete an attachment from Confluence.

    **Warning**: This action cannot be undone! The attachment and ALL its versions will be
    permanently deleted.

    Use this tool to:
    - Remove outdated or incorrect attachments
    - Clean up duplicate files
    - Delete sensitive information that was accidentally uploaded

    Best practices:
    - Verify the attachment ID before deletion using get_attachments
    - Consider downloading the attachment first as a backup
    - Check with content owners before deleting shared attachments

    Args:
        ctx: The FastMCP context.
        attachment_id: The ID of the attachment to delete.

    Returns:
        JSON string confirming deletion with attachment ID.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)

    confluence_fetcher.delete_attachment(attachment_id=attachment_id)

    return json.dumps(
        {
            "message": "Attachment deleted successfully",
            "attachment_id": attachment_id,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "read", "attachments", "toolset:confluence_attachments"},
    annotations={"title": "Get Page Images", "readOnlyHint": True},
)
async def get_page_images(
    ctx: Context,
    content_id: Annotated[
        str,
        Field(
            description=(
                "The ID of the Confluence page or blog post to retrieve "
                "images from. Example: '123456789'"
            )
        ),
    ],
) -> list[TextContent | ImageContent]:
    """Get all images attached to a Confluence page as inline image content.

    Filters attachments to images only (PNG, JPEG, GIF, WebP, SVG, BMP)
    and returns them as base64-encoded ImageContent that clients can
    render directly. Non-image attachments are excluded.

    Files with ambiguous MIME types (application/octet-stream) are
    detected by filename extension as a fallback. Images larger than
    50 MB are skipped with an error entry in the summary.

    Args:
        ctx: The FastMCP context.
        content_id: The ID of the content.

    Returns:
        A list with a text summary followed by one ImageContent per
        successfully downloaded image.
    """
    confluence_fetcher = await get_confluence_fetcher(ctx)
    contents: list[TextContent | ImageContent] = []

    attachments_result = confluence_fetcher.get_content_attachments(content_id)

    if not attachments_result.get("success"):
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(attachments_result, indent=2, ensure_ascii=False),
            )
        )
        return contents

    attachment_data = attachments_result.get("attachments", [])

    # Filter to image attachments
    image_attachments: list[tuple[dict[str, object], str]] = []
    for att_dict in attachment_data:
        if not isinstance(att_dict, dict):
            continue
        media_type = att_dict.get("extensions", {}).get("mediaType") or att_dict.get(
            "metadata", {}
        ).get("mediaType")
        filename = att_dict.get("title")
        is_img, resolved_mime = is_image_attachment(media_type, filename)
        if is_img:
            image_attachments.append((att_dict, resolved_mime))

    if not image_attachments:
        contents.append(
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": True,
                        "content_id": content_id,
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

    for att_dict, resolved_mime in image_attachments:
        attachment = ConfluenceAttachment.from_api_response(att_dict)
        filename = attachment.title or "unknown"

        if (
            attachment.file_size is not None
            and attachment.file_size > ATTACHMENT_MAX_BYTES
        ):
            failed.append(
                {
                    "filename": filename,
                    "error": (
                        f"Image is {attachment.file_size} bytes "
                        "which exceeds the 50 MB inline limit."
                    ),
                }
            )
            continue

        download_url = attachment.download_url or ""
        if not download_url:
            failed.append({"filename": filename, "error": "No download URL"})
            continue

        download_url = resolve_relative_url(download_url, confluence_fetcher.config.url)

        encoded, _, fetched_bytes = fetch_and_encode_attachment(
            fetch_fn=confluence_fetcher.fetch_attachment_content,
            url=download_url,
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
        "content_id": content_id,
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


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_users"},
    annotations={"title": "Get Current User", "readOnlyHint": True},
)
async def get_current_user(
    ctx: Context,
    include_full_profile: Annotated[
        bool,
        Field(
            description=(
                "When True, return the full user payload. When False, "
                "return only the most useful fields (accountId/key/name, "
                "displayName, email). Default True."
            ),
            default=True,
        ),
    ] = True,
) -> str:
    """Return the authenticated user's profile.

    Useful as the entry point for "my pages", "pages I edited", or any
    workflow that needs the user's account/key/displayName.

    Returns:
        JSON string with the current user info.
    """
    confluence = await get_confluence_fetcher(ctx)
    try:
        info = confluence.get_current_user_info()
        if not include_full_profile and isinstance(info, dict):
            info = {
                k: info.get(k)
                for k in (
                    "accountId",
                    "userKey",
                    "username",
                    "displayName",
                    "email",
                )
                if info.get(k) is not None
            }
        return json.dumps({"success": True, "user": info}, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error fetching current Confluence user: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_spaces"},
    annotations={"title": "List Confluence Spaces", "readOnlyHint": True},
)
async def list_spaces(
    ctx: Context,
    start: Annotated[
        int,
        Field(description="Pagination offset. Default 0.", default=0),
    ] = 0,
    limit: Annotated[
        int,
        Field(description="Maximum number of spaces to return. Default 25.", default=25),
    ] = 25,
) -> str:
    """List Confluence spaces visible to the authenticated user.

    Returns:
        JSON string with the spaces collection.
    """
    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.get_spaces(start=start, limit=limit)
        return json.dumps(
            {"success": True, "spaces": result}, indent=2, ensure_ascii=False
        )
    except Exception as e:
        logger.error(f"Error listing Confluence spaces: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_spaces"},
    annotations={"title": "Get User-Contributed Spaces", "readOnlyHint": True},
)
async def get_user_contributed_spaces(
    ctx: Context,
    limit: Annotated[
        int,
        Field(description="Maximum number of results. Default 250.", default=250),
    ] = 250,
) -> str:
    """List spaces the authenticated user has recently contributed to.

    Returns:
        JSON string with a dict of space-key -> {key, name}.
    """
    confluence = await get_confluence_fetcher(ctx)
    try:
        spaces = confluence.get_user_contributed_spaces(limit=limit)
        return json.dumps(
            {"success": True, "count": len(spaces), "spaces": spaces},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error listing contributed spaces: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page Ancestors", "readOnlyHint": True},
)
async def get_page_ancestors(
    ctx: Context,
    page_id: Annotated[str, Field(description="The page ID")],
) -> str:
    """List the ancestor pages of a Confluence page (parent chain).

    Order is hierarchical: immediate parent first, root last.

    Returns:
        JSON string with the ancestor pages.
    """
    confluence = await get_confluence_fetcher(ctx)
    try:
        ancestors = confluence.get_page_ancestors(page_id)
        simplified = [a.to_simplified_dict() for a in ancestors]
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "count": len(simplified),
                "ancestors": simplified,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(f"Error fetching page ancestors: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Content Restrictions (Req 28 — toolset:confluence_restrictions)
# =============================================================================


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_restrictions"},
    annotations={"title": "List Content Restrictions", "readOnlyHint": True},
)
async def list_content_restrictions(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content ID (page or blog post). "
                "Numeric ID from URL (e.g., '123456789')."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """List current read/update restrictions on a Confluence page or blog post.

    Read_Tool for Requirement 28.1. Wraps
    ``GET /rest/api/content/{content_id}/restriction/byOperation`` and returns
    the full response payload so callers can inspect both the ``read`` and
    ``update`` operation entries (each with their ``user`` / ``group``
    principal lists).

    The space filter is intentionally skipped here (Req 43 allows it for
    content-id-only endpoints) because a page's space cannot be resolved
    cheaply from the content id alone without a second HTTP call.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the target page or blog post.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "restrictions": ...}``
        on success, or ``{"success": False, "error": ...}`` on failure.
    """
    # Defer the dc_guards import so the module is importable in environments
    # where the helper module has not been picked up yet (matches the
    # lazy-import style used elsewhere for optional cross-cutting helpers).
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_restrictions"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        restrictions = confluence.list_content_restrictions(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "restrictions": restrictions,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing content restrictions for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_restrictions"},
    annotations={"title": "Set Content Restrictions", "destructiveHint": True},
)
async def set_content_restrictions(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content ID (page or blog post) to restrict. "
                "Numeric ID from URL (e.g., '123456789')."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    read_users: Annotated[
        list[str] | None,
        Field(
            description=(
                "Usernames permitted to read the content. Empty or None means "
                "no user-level read restriction (group-level reads, if any, "
                "still apply)."
            ),
            default=None,
        ),
    ] = None,
    read_groups: Annotated[
        list[str] | None,
        Field(
            description="Group names permitted to read the content.",
            default=None,
        ),
    ] = None,
    update_users: Annotated[
        list[str] | None,
        Field(
            description="Usernames permitted to update the content.",
            default=None,
        ),
    ] = None,
    update_groups: Annotated[
        list[str] | None,
        Field(
            description="Group names permitted to update the content.",
            default=None,
        ),
    ] = None,
) -> str:
    """Replace the read/update restrictions on a Confluence page or blog post.

    Write_Tool for Requirements 28.2 and 28.4. Wraps
    ``PUT /rest/api/content/{content_id}/restriction`` via
    ``RestrictionsMixin.set_content_restrictions``, which captures the prior
    restriction state before the PUT so the server layer can build a
    reversible receipt (Req 28.4).

    The space filter is intentionally skipped (Req 43) because the page's
    space cannot be resolved cheaply from the content id alone. The
    read-only guard runs as a belt-and-suspenders precheck before any
    outbound HTTP call.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the target page or blog post.
        read_users: Usernames permitted to read the content.
        read_groups: Group names permitted to read the content.
        update_users: Usernames permitted to update the content.
        update_groups: Group names permitted to update the content.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "prior_state": ..., "new_state": ..., "receipt": {...}}`` on success.
        The ``receipt`` carries the prior-state snapshot in ``inverse_args``
        so callers can restore the exact previous restrictions by invoking
        ``confluence_set_content_restrictions`` with those args, or
        ``confluence_clear_content_restrictions`` when no prior principals
        were recorded.
    """
    from mcp_atlassian.utils.dc_guards import build_receipt, check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_restrictions"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.set_content_restrictions(
            page_id,
            read_users=read_users,
            read_groups=read_groups,
            update_users=update_users,
            update_groups=update_groups,
        )
    except Exception as e:
        logger.error(
            f"Error setting content restrictions for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    prior_state = result.get("prior_state", {})
    new_state = result.get("new_state", {})

    # Extract prior-state principal lists so the inverse-tool invocation
    # carries the exact arguments needed to restore the previous state.
    prior_read_users, prior_read_groups = _extract_restriction_principals(
        prior_state, "read"
    )
    prior_update_users, prior_update_groups = _extract_restriction_principals(
        prior_state, "update"
    )

    prior_was_empty = not (
        prior_read_users
        or prior_read_groups
        or prior_update_users
        or prior_update_groups
    )

    if prior_was_empty:
        # No prior restrictions recorded — inverse is to clear rather than
        # re-apply an empty allow-list (which would still PUT a body).
        inverse_tool: str = "confluence_clear_content_restrictions"
        inverse_args: dict[str, Any] = {"page_id": page_id}
    else:
        inverse_tool = "confluence_set_content_restrictions"
        inverse_args = {
            "page_id": page_id,
            "read_users": prior_read_users,
            "read_groups": prior_read_groups,
            "update_users": prior_update_users,
            "update_groups": prior_update_groups,
        }

    receipt = build_receipt(
        object_id=page_id,
        inverse_tool=inverse_tool,
        inverse_args=inverse_args,
        note="Prior restriction state snapshot",
        recipient_scope=None,
    )

    return json.dumps(
        {
            "success": True,
            "page_id": page_id,
            "prior_state": prior_state,
            "new_state": new_state,
            "receipt": receipt,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_restrictions"},
    annotations={"title": "Clear Content Restrictions", "destructiveHint": True},
)
async def clear_content_restrictions(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content ID (page or blog post) whose restrictions "
                "should be cleared. Numeric ID from URL (e.g., '123456789')."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Remove every restriction from a Confluence page or blog post.

    Write_Tool for Requirement 28.3. Wraps
    ``DELETE /rest/api/content/{content_id}/restriction`` via
    ``RestrictionsMixin.clear_content_restrictions``. After a successful
    call any user with Confluence's normal space-level permissions may
    read and update the content.

    The read-only guard runs as a belt-and-suspenders precheck before any
    outbound HTTP call. The space filter is intentionally skipped (Req 43)
    because the page's space cannot be resolved cheaply from the content
    id alone.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the target page or blog post.

    Returns:
        JSON string with ``{"success": True, "page_id": ...}`` on success,
        or ``{"success": False, "error": ...}`` on failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_restrictions"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        confluence.clear_content_restrictions(page_id)
        return json.dumps(
            {"success": True, "page_id": page_id},
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error clearing content restrictions for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


def _extract_restriction_principals(
    restrictions_payload: dict, operation: str
) -> tuple[list[str], list[str]]:
    """Pull the user/group principal names for a single restriction operation.

    Confluence returns restrictions in the shape::

        {
          "results": [
            {"operation": "read", "restrictions": {
              "user":  {"results": [{"username": "..."}, ...]},
              "group": {"results": [{"name": "..."}, ...]}
            }},
            {"operation": "update", "restrictions": {...}}
          ]
        }

    This helper extracts the raw name lists for the requested ``operation``
    so :func:`set_content_restrictions` can thread the prior state into the
    reversible-receipt ``inverse_args``.

    Args:
        restrictions_payload: The Confluence response body (from
            ``list_content_restrictions``). Missing keys and unexpected
            shapes are tolerated — the helper returns empty lists rather
            than raising so a quirky prior-state payload cannot break the
            write response.
        operation: ``"read"`` or ``"update"``.

    Returns:
        A ``(user_names, group_names)`` tuple where each list may be empty.
    """
    if not isinstance(restrictions_payload, dict):
        return [], []

    results = restrictions_payload.get("results", [])
    if not isinstance(results, list):
        return [], []

    for entry in results:
        if not isinstance(entry, dict):
            continue
        if entry.get("operation") != operation:
            continue
        restrictions = entry.get("restrictions", {})
        if not isinstance(restrictions, dict):
            return [], []

        user_results = (
            restrictions.get("user", {}).get("results", [])
            if isinstance(restrictions.get("user"), dict)
            else []
        )
        group_results = (
            restrictions.get("group", {}).get("results", [])
            if isinstance(restrictions.get("group"), dict)
            else []
        )

        user_names = [
            u.get("username")
            for u in user_results
            if isinstance(u, dict) and isinstance(u.get("username"), str)
        ]
        group_names = [
            g.get("name")
            for g in group_results
            if isinstance(g, dict) and isinstance(g.get("name"), str)
        ]
        return user_names, group_names

    return [], []


# =============================================================================
# Content Watchers (Req 29 — toolset:confluence_watchers)
# =============================================================================


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_watchers"},
    annotations={"title": "List Page Watchers", "readOnlyHint": True},
)
async def list_page_watchers(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "whose watcher list should be returned."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """List users watching a Confluence page.

    Read_Tool for Requirement 29.1. Wraps
    ``GET /rest/api/content/{page_id}/notification/child-created`` via
    :meth:`WatchersMixin.list_page_watchers` — Confluence DC exposes that
    notification-subscribers collection as the page's watcher list in the
    UI and there is no dedicated "list watchers" endpoint.

    The space filter is intentionally skipped (Req 43) because the page's
    space cannot be resolved cheaply from the content id alone without a
    second HTTP call.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id whose watchers to list.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "watchers": [...]}``
        on success, or ``{"success": False, "error": ...}`` on failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_watchers"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        watchers = confluence.list_page_watchers(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "watchers": watchers,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing watchers for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_watchers"},
    annotations={"title": "Watch Page (Self)", "destructiveHint": False},
)
async def watch_page_self(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "to add the authenticated user as a watcher of."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Add the authenticated user as a watcher of a Confluence page.

    Write_Tool for Requirement 29.2. Self-scoped only — there is no
    corresponding tool that watches a page on behalf of another user
    (Req 29.4).

    The underlying mixin is idempotent (Property 9): a repeat call on a
    page the user is already watching returns ``{"already_watching": True}``
    without issuing a second POST. This wrapper exposes that flag as a
    structured response field so callers can detect no-op runs.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id to watch.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "already_watching": bool}`` on success, or
        ``{"success": False, "error": ...}`` on failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_watchers"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.watch_page_self(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "already_watching": bool(result.get("already_watching", False)),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error watching page {page_id} (self): {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_watchers"},
    annotations={"title": "Unwatch Page (Self)", "destructiveHint": False},
)
async def unwatch_page_self(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "to remove the authenticated user from as a watcher."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Remove the authenticated user from the watcher list of a Confluence page.

    Write_Tool for Requirement 29.3. Self-scoped only — there is no
    corresponding tool that unwatches on behalf of another user
    (Req 29.4).

    The underlying mixin is idempotent (Property 9): a call on a page
    the user is not currently watching returns
    ``{"already_watching": False}`` without issuing a DELETE. On the
    success path (the user was watching) it returns
    ``{"already_watching": True}`` to report the pre-call state.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id to unwatch.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "already_watching": bool}`` on success (where ``already_watching``
        reports the pre-call state — ``True`` means the watch was removed
        by this call, ``False`` means the user was not watching and no
        DELETE was issued), or ``{"success": False, "error": ...}`` on
        failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_watchers"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.unwatch_page_self(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "already_watching": bool(result.get("already_watching", False)),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error unwatching page {page_id} (self): {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Space Permissions (Req 30 — toolset:confluence_space_admin)
# =============================================================================


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_space_admin"},
    annotations={"title": "List Space Permissions", "readOnlyHint": True},
)
async def list_space_permissions(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(
            description=(
                "Confluence space key (e.g. 'DOCS', 'TEAM') whose permission "
                "entries should be returned."
            )
        ),
    ],
) -> str:
    """List permissions configured on a Confluence space.

    Read_Tool for Requirement 30.1. Wraps
    ``GET /rest/api/space/{space_key}?expand=permissions`` via
    :meth:`SpacePermissionsMixin.list_space_permissions` and returns the
    raw list of permission entries so callers can inspect each operation
    (``read``, ``create``, ``delete``...) alongside its target principal
    (user, group, or anonymous).

    Requirement 30.2 explicitly excludes grant/revoke/modify behaviour from
    the ``confluence_space_admin`` toolset — no Write_Tool is registered
    here. The space-filter precheck is wired per design: when
    ``CONFLUENCE_SPACES_FILTER`` is configured, only keys in the allow-list
    may be inspected, and mismatches return a structured ``filtered_out``
    error before any outbound HTTP call.

    Args:
        ctx: The FastMCP context.
        space_key: Confluence space key whose permissions to list.

    Returns:
        JSON string with ``{"success": True, "space_key": ...,
        "permissions": [...]}`` on success, or
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call or the upstream request fails.
    """
    from mcp_atlassian.utils.dc_guards import (
        check_project_filter,
        check_read_only,
    )

    tool_tags = {"confluence", "read", "toolset:confluence_space_admin"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)

    if err := check_project_filter(
        "confluence",
        space_key,
        confluence.config.spaces_filter,
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        permissions = confluence.list_space_permissions(space_key)
        return json.dumps(
            {
                "success": True,
                "space_key": space_key,
                "permissions": permissions,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing permissions for space {space_key!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Templates and Blueprints (Req 32 — toolset:confluence_templates)
# =============================================================================


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_templates"},
    annotations={"title": "List Templates", "readOnlyHint": True},
)
async def list_templates(
    ctx: Context,
    space_key: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) Confluence space key to scope the listing "
                "(e.g. 'DOCS', 'TEAM'). When omitted, the global "
                "(instance-wide) templates are returned."
            ),
            default=None,
        ),
    ] = None,
    blueprint: Annotated[
        bool,
        Field(
            description=(
                "When true, list blueprint templates (built-in plus "
                "app-contributed) from /rest/api/template/blueprint. "
                "When false (default), list user-authored page templates "
                "from /rest/api/template/page."
            ),
            default=False,
        ),
    ] = False,
) -> str:
    """List page templates or blueprints available to the caller.

    Read_Tool for Requirement 32.1. Wraps
    :meth:`TemplatesMixin.list_templates`, which routes to one of
    ``GET /rest/api/template/page`` (user-authored templates) or
    ``GET /rest/api/template/blueprint`` (blueprint templates) based on
    the ``blueprint`` flag. When ``space_key`` is provided it is
    forwarded as the ``spaceKey`` query parameter so the listing is
    scoped to that space; when omitted, DC returns the global template
    set.

    The space filter is applied only when ``space_key`` is supplied:
    if ``CONFLUENCE_SPACES_FILTER`` is configured, the requested space
    must be in the allow-list. Calls without a ``space_key`` are
    global-scoped and pass through without a per-space check.

    Args:
        ctx: The FastMCP context.
        space_key: Optional Confluence space key to scope the listing.
        blueprint: When ``True`` list blueprint templates; default lists
            user-authored page templates.

    Returns:
        JSON string with ``{"success": True, "space_key": ...,
        "blueprint": ..., "templates": [...]}`` on success, or
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call or the upstream request fails.
    """
    from mcp_atlassian.utils.dc_guards import (
        check_project_filter,
        check_read_only,
    )

    tool_tags = {"confluence", "read", "toolset:confluence_templates"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)

    if space_key is not None:
        if err := check_project_filter(
            "confluence",
            space_key,
            confluence.config.spaces_filter,
        ):
            return json.dumps({"success": False, **err.to_dict()})

    try:
        templates = confluence.list_templates(
            space_key=space_key,
            blueprint=blueprint,
        )
        return json.dumps(
            {
                "success": True,
                "space_key": space_key,
                "blueprint": blueprint,
                "templates": templates,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing templates (space_key={space_key!r}, "
            f"blueprint={blueprint}): {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_templates"},
    annotations={"title": "Create Page From Template", "destructiveHint": True},
)
async def create_page_from_template(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(
            description=(
                "Key of the Confluence space to create the page in "
                "(e.g. 'DOCS', 'TEAM')."
            )
        ),
    ],
    title: Annotated[
        str,
        Field(description="Title for the new page."),
    ],
    template_id: Annotated[
        str,
        Field(
            description=(
                "Identifier of the template (page template or blueprint) "
                "whose body will seed the new page."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    parent_id: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) content id of the parent page. When omitted "
                "the page is created at the space root."
            ),
            default=None,
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ] = None,
    context: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "(Optional) blueprint-context variables forwarded verbatim "
                "on the create request. Leave null for plain page "
                "templates (DC ignores ``context`` for those)."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """Create a Confluence page seeded from a template or blueprint.

    Write_Tool for Requirement 32.2. Wraps
    :meth:`TemplatesMixin.create_page_from_template`, which fetches the
    template body via ``GET /rest/api/template/{template_id}`` and then
    posts ``POST /rest/api/content?expand=body.storage`` with the
    template's storage body as the new page's initial content.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call, and
    ``check_project_filter`` enforces ``CONFLUENCE_SPACES_FILTER``
    against ``space_key`` (always — creates target a specific space).

    Args:
        ctx: The FastMCP context.
        space_key: Key of the Confluence space to create the page in.
        title: Title for the new page.
        template_id: Identifier of the template seeding the new page.
        parent_id: Optional parent page id.
        context: Optional blueprint-context variables.

    Returns:
        JSON string with ``{"success": True, "space_key": ...,
        "page": {...}}`` on success, where ``page`` is the DC content
        response for the newly created page. On failure returns
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call, or ``{"success": False, "error": ...}`` when
        the upstream request fails.
    """
    from mcp_atlassian.utils.dc_guards import (
        check_project_filter,
        check_read_only,
    )

    tool_tags = {"confluence", "write", "toolset:confluence_templates"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)

    if err := check_project_filter(
        "confluence",
        space_key,
        confluence.config.spaces_filter,
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        page = confluence.create_page_from_template(
            space_key=space_key,
            title=title,
            template_id=template_id,
            parent_id=parent_id,
            context=context,
        )
        return json.dumps(
            {
                "success": True,
                "space_key": space_key,
                "page": page,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error creating page from template_id={template_id!r} "
            f"in space_key={space_key!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Page Properties (Req 33 — toolset:confluence_page_properties)
# =============================================================================


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_page_properties"},
    annotations={"title": "List Page Properties", "readOnlyHint": True},
)
async def list_page_properties(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "whose content properties should be listed."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """List all content properties defined on a Confluence page.

    Read_Tool for Requirement 33.1. Wraps
    ``GET /rest/api/content/{page_id}/property`` via
    :meth:`PagePropertiesMixin.list_page_properties`, which unwraps the
    DC ``results`` envelope so the caller receives a plain list.

    The space filter is intentionally skipped (Req 43) because the
    endpoint is content-id-only — resolving the page's space would
    require an extra HTTP call that defeats the purpose of a cheap
    listing. Callers that need to constrain access by space should do
    so at a higher layer.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id whose properties to list.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "properties": [...]}`` on success, or
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call, or ``{"success": False, "error": ...}`` on
        upstream failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_page_properties"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        properties = confluence.list_page_properties(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "properties": properties,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing page properties for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_page_properties"},
    annotations={"title": "Get Page Property", "readOnlyHint": True},
)
async def get_page_property(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "whose content property should be fetched."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    key: Annotated[
        str,
        Field(
            description=(
                "The property key to look up on the page "
                "(e.g. 'agent-state', 'last-sync')."
            )
        ),
    ],
) -> str:
    """Fetch a single Confluence page property by key.

    Read_Tool for Requirement 33.1. Wraps
    ``GET /rest/api/content/{page_id}/property/{key}`` via
    :meth:`PagePropertiesMixin.get_page_property`, which catches DC's
    404 for a missing property and surfaces it as a structured
    ``found: False`` response rather than an error. Callers can
    therefore distinguish "property absent" from "request failed"
    without parsing HTTP details.

    The space filter is intentionally skipped (Req 43) — the endpoint
    is content-id-only.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id whose property to fetch.
        key: The property key to look up.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "key": ...,
        "found": True, "property": {...}}`` when the property exists,
        ``{"success": True, "page_id": ..., "key": ..., "found": False,
        "property": None}`` when DC reports the property absent, or
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call, or ``{"success": False, "error": ...}`` on
        upstream failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_page_properties"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        prop = confluence.get_page_property(page_id, key)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "key": key,
                "found": prop is not None,
                "property": prop,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error fetching page property key={key!r} for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_page_properties"},
    annotations={"title": "Set Page Property", "destructiveHint": False},
)
async def set_page_property(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "on which to create or update the content property."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    key: Annotated[
        str,
        Field(
            description=(
                "The property key to create or update "
                "(e.g. 'agent-state', 'last-sync')."
            )
        ),
    ],
    value: Annotated[
        Any,
        Field(
            description=(
                "The JSON-serializable value to store under the key. May "
                "be any shape Confluence accepts: object, array, string, "
                "number, or boolean."
            )
        ),
    ],
) -> str:
    """Idempotently create or update a Confluence page property.

    Write_Tool for Requirement 33.2 (and Requirement 33.4 — idempotence,
    Property 9). Wraps :meth:`PagePropertiesMixin.set_page_property`,
    which probes for the existing property first: on absent it ``POST``s
    a new record; on present it reads the current ``version.number`` and
    ``PUT``s with an incremented version. Calling the tool twice with
    the same ``(page_id, key, value)`` therefore leaves the server-side
    state the same as a single invocation (the version counter bumps,
    but the stored value and key do not change).

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) — the endpoint is content-id-only
    and resolving the page's space would require an extra HTTP call.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id on which to set the property.
        key: The property key to create or update.
        value: The JSON-serializable value to store.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "key": ...,
        "property": {...}}`` on success, or
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call, or ``{"success": False, "error": ...}`` on
        upstream failure.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_page_properties"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        prop = confluence.set_page_property(page_id, key, value)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "key": key,
                "property": prop,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error setting page property key={key!r} on page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_page_properties"},
    annotations={"title": "Delete Page Property", "destructiveHint": True},
)
async def delete_page_property(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. '123456789') "
                "whose content property should be deleted."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    key: Annotated[
        str,
        Field(
            description=(
                "The property key to remove from the page."
            )
        ),
    ],
) -> str:
    """Delete a Confluence page property by key.

    Write_Tool for Requirement 33.3. Wraps
    ``DELETE /rest/api/content/{page_id}/property/{key}`` via
    :meth:`PagePropertiesMixin.delete_page_property`. A 404 from DC
    (the property is already absent) is surfaced as an upstream error
    rather than silently ignored, so the caller can distinguish
    "removed" from "was never set".

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck. The space filter is intentionally skipped (Req 43) — the
    endpoint is content-id-only.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id whose property to delete.
        key: The property key to remove.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "key": ...}``
        on success, or ``{"success": False, "error_code": ..., ...}``
        when a guard rejects the call, or
        ``{"success": False, "error": ...}`` on upstream failure
        (including a 404 when the property does not exist).
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_page_properties"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        confluence.delete_page_property(page_id, key)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "key": key,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error deleting page property key={key!r} on page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Page and Space Archive (Req 34 — toolset:confluence_archive)
# =============================================================================
#
# Three Write_Tools wrap :class:`ConfluenceArchiveMixin` against the DC
# archive endpoints so agents can retire content without permanently
# destroying it. The toolset deliberately does not include a
# ``confluence_delete_space`` or cascading page-tree-delete tool
# (Requirements 34.4 and 34.5 forbid both).
#
# Prelude per design: ``check_read_only`` is a belt-and-suspenders
# precheck against ``READ_ONLY_MODE`` before any outbound HTTP call, and
# ``check_project_filter`` enforces ``CONFLUENCE_SPACES_FILTER`` on
# ``confluence_archive_space`` where the target space is a direct
# argument. The two page-scoped tools intentionally skip the space
# filter (Req 43) because the page's space cannot be resolved cheaply
# from the content id alone.
#
# ``confluence_archive_page`` returns a reversible receipt pointing at
# ``confluence_restore_archived_page`` so agents can undo the archive
# with a single follow-up call (Requirement 34.6 / Property 8).


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_archive"},
    annotations={"title": "Archive Page", "destructiveHint": True},
)
async def archive_page(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID to archive (numeric ID from URL, e.g. "
                "'123456789'). Archiving hides the page from default "
                "listings but does not delete it — the content and history "
                "remain recoverable via ``confluence_restore_archived_page``."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Archive a Confluence page without permanently deleting it.

    Write_Tool for Requirement 34.1 (and Requirement 34.6 — reversible
    receipt). Wraps ``POST /rest/api/content/archive`` via
    :meth:`ConfluenceArchiveMixin.archive_page`, which submits a
    single-entry ``pages`` batch so the behavior is deterministic: one
    call archives exactly the one page identified by ``page_id``.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) — the endpoint is content-id-only
    and resolving the page's space would require an extra HTTP call.

    The response carries a reversible receipt pointing at
    ``confluence_restore_archived_page`` so the agent can undo the
    archive with a single follow-up call (Req 34.6 / Property 8).

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the page to archive.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "archived": True, "response": {...}, "receipt": {...}}`` on
        success, where ``receipt`` is shaped per
        :func:`build_receipt` and names
        ``confluence_restore_archived_page`` as the inverse tool with
        ``{"page_id": page_id}`` as the inverse args. On failure
        returns ``{"success": False, "error_code": ..., ...}`` when a
        guard rejects the call, or ``{"success": False, "error": ...}``
        on upstream failure.
    """
    from mcp_atlassian.utils.dc_guards import build_receipt, check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_archive"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.archive_page(page_id)
    except Exception as e:
        logger.error(
            f"Error archiving Confluence page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    receipt = build_receipt(
        object_id=str(page_id),
        inverse_tool="confluence_restore_archived_page",
        inverse_args={"page_id": str(page_id)},
        note=None,
        recipient_scope=None,
    )

    return json.dumps(
        {
            "success": True,
            "page_id": str(page_id),
            "archived": bool(result.get("archived", True)),
            "response": result.get("response", {}),
            "receipt": receipt,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_archive"},
    annotations={"title": "Restore Archived Page", "destructiveHint": False},
)
async def restore_archived_page(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID of the archived page to restore "
                "(numeric ID from URL, e.g. '123456789'). The page must "
                "currently be in the archived state; calling this tool on "
                "a live page will return an upstream 404."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Restore a previously archived Confluence page to the current state.

    Write_Tool for Requirement 34.2 (the inverse of
    ``confluence_archive_page``). Wraps
    ``PUT /rest/api/content/{page_id}?status=archived`` via
    :meth:`ConfluenceArchiveMixin.restore_archived_page`, which first
    reads the archived page's version number and then issues the update
    with the incremented version and ``status=current``.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) — the endpoint is content-id-only.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the archived page to restore.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "restored": True, "version": <new-version>, "response": {...}}``
        on success. On failure returns
        ``{"success": False, "error_code": ..., ...}`` when a guard
        rejects the call, or ``{"success": False, "error": ...}`` on
        upstream failure (for example when the page is not in the
        archived collection).
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "write", "toolset:confluence_archive"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.restore_archived_page(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": str(page_id),
                "restored": bool(result.get("restored", True)),
                "version": result.get("version"),
                "response": result.get("response", {}),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error restoring archived Confluence page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_archive"},
    annotations={"title": "Archive Space", "destructiveHint": True},
)
async def archive_space(
    ctx: Context,
    space_key: Annotated[
        str,
        Field(
            description=(
                "Key of the Confluence space to archive (e.g. 'DOCS', "
                "'TEAM'). Archiving hides the space from default listings "
                "and prevents further edits but does not delete it — "
                "Requirements 34.4 and 34.5 forbid a permanent-delete "
                "tool, so space restoration must be done via the "
                "Confluence administration UI."
            )
        ),
    ],
) -> str:
    """Archive a Confluence space (DC 7.0+) without permanently deleting it.

    Write_Tool for Requirement 34.3. Wraps
    ``PUT /rest/api/space/{space_key}/archive`` via
    :meth:`ConfluenceArchiveMixin.archive_space`, which sets the space
    status to ``archived``. No request body is required.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call, and
    ``check_project_filter`` enforces ``CONFLUENCE_SPACES_FILTER``
    against ``space_key`` (the space key is a direct argument, so the
    filter is derivable and always applied per Req 43).

    No reversible receipt is emitted: the REST API does not expose a
    matching unarchive path, so space restoration is handled through the
    Confluence administration UI rather than through this toolset.

    Args:
        ctx: The FastMCP context.
        space_key: Key of the Confluence space to archive.

    Returns:
        JSON string with ``{"success": True, "space_key": ...,
        "archived": True, "response": {...}}`` on success. On failure
        returns ``{"success": False, "error_code": ..., ...}`` when a
        guard rejects the call, or ``{"success": False, "error": ...}``
        on upstream failure (for example 403 when the caller is not a
        space administrator, or 501 when the DC version predates the
        archive endpoint).
    """
    from mcp_atlassian.utils.dc_guards import (
        check_project_filter,
        check_read_only,
    )

    tool_tags = {"confluence", "write", "toolset:confluence_archive"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)

    if err := check_project_filter(
        "confluence",
        space_key,
        confluence.config.spaces_filter,
    ):
        return json.dumps({"success": False, **err.to_dict()})

    try:
        result = confluence.archive_space(space_key)
        return json.dumps(
            {
                "success": True,
                "space_key": str(space_key),
                "archived": bool(result.get("archived", True)),
                "response": result.get("response", {}),
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error archiving Confluence space {space_key}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# CQL Advanced Search (Req 35 — toolset:confluence_search)
# =============================================================================
#
# ``confluence_cql_search`` exposes :meth:`CQLAdvancedMixin.cql_search` as a
# structured Read_Tool with explicit pagination and an opt-in ``order_by`` /
# ``order_dir`` sort. The prelude follows the standard DC-guard pattern used
# across this feature:
#
#   1. ``check_read_only`` runs first as a belt-and-suspenders precheck so
#      even a misconfigured tag set cannot slip past when the server is in
#      read-only mode.
#   2. ``order_by`` is validated against ``SORTABLE_FIELDS`` *before* any
#      outbound HTTP. An unknown field resolves to a structured
#      ``invalid_order_by`` error (Req 35.2 + design Property 13).
#   3. When ``CONFLUENCE_SPACES_FILTER`` is configured, the CQL string is
#      routed through :meth:`CQLAdvancedMixin.rewrite_cql_for_space_filter`
#      so the outbound query is bounded by the allow-list. Disjoint space
#      references surface as a structured ``filtered_out`` error without any
#      outbound call (Req 35.3 + design Property 14).


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_search"},
    annotations={"title": "CQL Advanced Search", "readOnlyHint": True},
)
async def cql_search(
    ctx: Context,
    cql: Annotated[
        str,
        Field(
            description=(
                "Confluence CQL query string (e.g. 'type = page AND "
                "space = DOCS'). Passed through to the upstream search "
                "endpoint unchanged except for an optional appended "
                "``order by`` clause and, when "
                "``CONFLUENCE_SPACES_FILTER`` is configured, a space "
                "allow-list restriction."
            )
        ),
    ],
    order_by: Annotated[
        str | None,
        Field(
            description=(
                "(Optional) CQL-sortable field name used to append an "
                "``order by <field> <dir>`` clause to the query. Must be "
                "one of 'title', 'created', 'lastmodified', 'space', "
                "'id', 'type'. Any other value is rejected with a "
                "structured ``invalid_order_by`` error before any "
                "outbound HTTP is issued (Req 35.2)."
            ),
            default=None,
        ),
    ] = None,
    order_dir: Annotated[
        str,
        Field(
            description=(
                "Sort direction applied when ``order_by`` is provided. "
                "Accepts 'asc' (default) or 'desc' (case-insensitive)."
            ),
            default="asc",
        ),
    ] = "asc",
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of results to return. Defaults to 25, "
                "matching the Confluence CQL default page size."
            ),
            default=25,
            ge=1,
            le=100,
        ),
    ] = 25,
    start: Annotated[
        int,
        Field(
            description="Zero-based offset into the result set for pagination.",
            default=0,
            ge=0,
        ),
    ] = 0,
) -> str:
    """Run a CQL search with explicit sort and space-filter awareness.

    Read_Tool for Requirement 35. Wraps
    :meth:`CQLAdvancedMixin.cql_search` and applies the standard DC
    prelude: ``check_read_only`` runs first as a belt-and-suspenders
    precheck; ``order_by`` is validated against
    :data:`~mcp_atlassian.confluence.cql_advanced.SORTABLE_FIELDS` so
    unknown fields resolve to a structured ``invalid_order_by`` error
    *before* any outbound HTTP (Req 35.2); and when
    ``CONFLUENCE_SPACES_FILTER`` is configured the CQL string is routed
    through :meth:`CQLAdvancedMixin.rewrite_cql_for_space_filter` so the
    outbound query is bounded by the allow-list, or — when the referenced
    spaces are disjoint from the allow-list — surfaces a structured
    ``filtered_out`` error with zero outbound HTTP (Req 35.3).

    Args:
        ctx: The FastMCP context.
        cql: Confluence CQL query string.
        order_by: Optional CQL-sortable field. Must be a member of
            :data:`SORTABLE_FIELDS`; otherwise the call resolves to a
            structured ``invalid_order_by`` error.
        order_dir: Sort direction when ``order_by`` is provided
            ('asc' default, 'desc' accepted, case-insensitive).
        limit: Maximum results (1–100, default 25).
        start: Zero-based offset for pagination (default 0).

    Returns:
        JSON string with ``{"success": True, "cql": <effective cql>,
        "order_by": ..., "order_dir": ..., "limit": ..., "start": ...,
        "results": {...}}`` on success, where ``results`` is the raw CQL
        search payload returned by Confluence (containing ``results``,
        ``_links``, ``size``, ``totalSize``). On guard rejection returns
        ``{"success": False, "error_code": ..., "message": ...,
        "details": {...}}`` with no outbound HTTP issued. Other upstream
        failures return ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.confluence.cql_advanced import SORTABLE_FIELDS
    from mcp_atlassian.utils.dc_guards import (
        StructuredError,
        check_read_only,
    )

    tool_tags = {"confluence", "read", "toolset:confluence_search"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    # Pre-validate ``order_by`` against the documented CQL-sortable set
    # so unknown fields resolve to a structured ``invalid_order_by``
    # error before we even instantiate the fetcher — guaranteeing zero
    # outbound HTTP (Req 35.2, Property 13).
    if order_by is not None and order_by not in SORTABLE_FIELDS:
        err = StructuredError(
            error_code="invalid_order_by",
            message=(
                f"order_by {order_by!r} is not a CQL-sortable field. "
                f"Allowed fields: {sorted(SORTABLE_FIELDS)}."
            ),
            details={
                "order_by": order_by,
                "allowed_fields": sorted(SORTABLE_FIELDS),
            },
        )
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)

    # Apply the space-filter rewrite when ``CONFLUENCE_SPACES_FILTER`` is
    # configured. The mixin raises ``ValueError`` with a ``filtered_out:``
    # prefix when the CQL references spaces disjoint from the allow-list;
    # we map that onto the structured ``filtered_out`` error envelope so
    # no outbound HTTP is issued (Req 35.3, Property 14).
    effective_cql = cql
    filter_env = confluence.config.spaces_filter
    if filter_env:
        allowed_spaces = [
            token.strip() for token in filter_env.split(",") if token.strip()
        ]
        if allowed_spaces:
            try:
                effective_cql = confluence.rewrite_cql_for_space_filter(
                    cql, allowed_spaces
                )
            except ValueError as ve:
                message = str(ve)
                if message.startswith("filtered_out:"):
                    err = StructuredError(
                        error_code="filtered_out",
                        message=message.split("filtered_out:", 1)[1].strip()
                        or "CQL references spaces outside the allow-list.",
                        details={
                            "cql": cql,
                            "allowed_spaces": allowed_spaces,
                        },
                    )
                    return json.dumps({"success": False, **err.to_dict()})
                # Any other ValueError surfaces without the structured
                # envelope so the caller can see the underlying cause.
                logger.error(
                    f"Validation error rewriting CQL {cql!r} for space "
                    f"filter {allowed_spaces!r}: {ve}",
                    exc_info=True,
                )
                return json.dumps({"success": False, "error": message})

    try:
        results = confluence.cql_search(
            effective_cql,
            order_by=order_by,
            order_dir=order_dir,
            limit=limit,
            start=start,
        )
    except ValueError as ve:
        # The mixin also defends itself against ``invalid_order_by`` /
        # bad ``order_dir``; map those onto the structured envelope as a
        # second line of defence even though the pre-check above should
        # have caught the ``order_by`` case.
        message = str(ve)
        if message.startswith("invalid_order_by:"):
            err = StructuredError(
                error_code="invalid_order_by",
                message=message.split("invalid_order_by:", 1)[1].strip()
                or "order_by field is not CQL-sortable.",
                details={
                    "order_by": order_by,
                    "order_dir": order_dir,
                    "allowed_fields": sorted(SORTABLE_FIELDS),
                },
            )
            return json.dumps({"success": False, **err.to_dict()})
        logger.error(
            f"Validation error running CQL search cql={cql!r}, "
            f"order_by={order_by!r}: {ve}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": message})
    except Exception as e:
        logger.error(
            f"Error running CQL search cql={cql!r}, order_by={order_by!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "cql": effective_cql,
            "order_by": order_by,
            "order_dir": order_dir,
            "limit": limit,
            "start": start,
            "results": results,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Inline Tasks (Req 36 — toolset:confluence_tasks)
# =============================================================================
#
# Requirement 36 exposes a single Read_Tool —
# ``confluence_list_inline_tasks`` — that surfaces the open inline tasks
# attached to a Confluence page, including assignee and due-date fields.
# Req 36.2 explicitly forbids any Write_Tool in ``toolset:confluence_tasks``
# in this feature, so this section registers exactly one read tool.
#
# The prelude follows the standard DC-guard pattern used for the other
# read-only tools in this server (`list_page_properties`, `cql_search`,
# `get_page_property`):
#
#   1. ``check_read_only`` runs first as a belt-and-suspenders precheck so
#      a misconfigured tag set cannot slip past when the server is in
#      read-only mode (even though this tool is tagged ``read``).
#   2. The space filter (``CONFLUENCE_SPACES_FILTER``) is intentionally
#      skipped (Req 43): the upstream endpoint is content-id-only, so
#      resolving the page's space would require an extra HTTP call that
#      defeats the purpose of a cheap listing. Callers that need to
#      constrain access by space should do so at a higher layer.


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_tasks"},
    annotations={"title": "List Inline Tasks", "readOnlyHint": True},
)
async def list_inline_tasks(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID (numeric ID from URL, e.g. "
                "'123456789') whose open inline tasks should be listed. "
                "Backed by the DC ``mywork`` plugin's "
                "``GET /rest/mywork/latest/task?pageId={page_id}`` "
                "endpoint; pages on instances without the plugin resolve "
                "to an empty list rather than an error."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """List open inline tasks on a Confluence page.

    Read_Tool for Requirement 36.1. Wraps
    :meth:`InlineTasksMixin.list_inline_tasks`, which targets the DC
    ``mywork`` plugin's inline-task endpoint and normalizes both the
    bare-array and ``{"results": [...]}`` response shapes. Each entry
    carries the task id, completion status, assignee, due date, source
    page, and task body (pass-through from DC), giving meeting hosts
    the follow-up context required by Requirement 36.1 without a second
    round-trip.

    Requirement 36.2 explicitly forbids any Write_Tool in
    ``toolset:confluence_tasks`` in this feature; this module therefore
    registers exactly one read tool.

    The space filter is intentionally skipped (Req 43) because the
    ``mywork`` endpoint is content-id-only — resolving the page's space
    would require an extra HTTP call that defeats the purpose of a
    cheap listing. Callers that need to constrain access by space
    should do so at a higher layer.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence page id whose inline tasks to list.

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "tasks":
        [...]}`` on success, where ``tasks`` is the list of inline-task
        dicts returned by DC (or an empty list when the page has no
        inline tasks, the ``mywork`` plugin is unavailable, or the
        response cannot be interpreted). On guard rejection returns
        ``{"success": False, "error_code": ..., "message": ...,
        "details": {...}}`` with no outbound HTTP issued. Other upstream
        failures return ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_tasks"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        tasks = confluence.list_inline_tasks(page_id)
        return json.dumps(
            {
                "success": True,
                "page_id": page_id,
                "tasks": tasks,
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error listing inline tasks for page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})


# =============================================================================
# Page Likes (Req 37 — toolset:confluence_likes, plugin-gated)
# =============================================================================
#
# Requirement 37 exposes two Write_Tools — ``confluence_like_page`` and
# ``confluence_unlike_page`` — that add or remove the authenticated user's
# like on a Confluence page via the plugin-bundled REST endpoint family
# ``/rest/likes/1.0/content/{page_id}/likes``. The endpoints are provided
# by the bundled Confluence Likes plugin rather than the core content REST
# surface; on DC instances where the plugin is absent or disabled, every
# request to that path returns ``404 Not Found``. The mixin raises
# :class:`LikesPluginUnavailableError` on that signal, and each tool here
# catches it and surfaces a structured ``plugin_unavailable`` error naming
# the plugin so the agent can advise the operator to install or enable it
# (Requirement 37.2).
#
# The prelude follows the standard DC-guard pattern used across this
# feature:
#
#   1. ``check_read_only`` runs first as a belt-and-suspenders precheck
#      so even a misconfigured tag set cannot slip past when the server
#      is in read-only mode.
#   2. The space filter (``CONFLUENCE_SPACES_FILTER``) is intentionally
#      skipped (Req 43) — the upstream endpoint is content-id-only, so
#      resolving the page's space would require an extra HTTP call
#      before the write. Callers that need to constrain writes by space
#      should do so at a higher layer.
#
# Idempotency (Requirement 37.3): ``confluence_like_page`` returns
# ``{"already_liked": bool}`` so calling the tool twice on a page the
# user has already liked is reported as a success — the end state
# matches the caller's intent even though the second call made no
# change on the server.


from mcp_atlassian.confluence.likes import (  # noqa: E402
    LikesPluginUnavailableError,
)


_CONFLUENCE_LIKES_WRITE_TAGS: set[str] = {
    "confluence",
    "write",
    "toolset:confluence_likes",
}


def _confluence_likes_plugin_unavailable_response(
    exc: LikesPluginUnavailableError,
) -> str:
    """Render a structured ``plugin_unavailable`` error response.

    Centralized here so both like tools emit the same envelope naming the
    Confluence Likes plugin and carrying the raised message as
    ``details.reason`` (Requirement 37.2).
    """
    return json.dumps(
        {
            "success": False,
            "error_code": "plugin_unavailable",
            "message": (
                "Confluence Likes plugin endpoint is unavailable. Install "
                "or enable the bundled Confluence Likes plugin on the "
                "target Confluence DC instance."
            ),
            "details": {
                "plugin": "confluence-likes",
                "product": "confluence",
                "reason": str(exc),
            },
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_likes"},
    annotations={"title": "Like Page", "destructiveHint": False},
)
async def like_page(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID to like (numeric ID from URL, e.g. "
                "'123456789'). The like is recorded for the authenticated "
                "user against the bundled Confluence Likes plugin at "
                "``POST /rest/likes/1.0/content/{page_id}/likes``."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Like a Confluence page on behalf of the authenticated user.

    Write_Tool for Requirement 37.1. Wraps
    :meth:`LikesMixin.like_page`, which issues
    ``POST /rest/likes/1.0/content/{page_id}/likes`` with no request
    body against the plugin-bundled Likes endpoint family.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) — the endpoint is content-id-only
    and resolving the page's space would require an extra HTTP call
    before the write.

    Idempotency (Requirement 37.3): when the user has already liked the
    page, the mixin translates the upstream ``409 Conflict`` into a
    successful ``{"already_liked": True}`` result so the tool call is
    idempotent — calling it twice leaves server-observable state equal
    to calling it once.

    Plugin availability (Requirement 37.2): when the Confluence Likes
    plugin is absent or disabled, the mixin raises
    :class:`LikesPluginUnavailableError` (the endpoint returned 404).
    The tool catches it and surfaces a structured ``plugin_unavailable``
    error naming the plugin so the agent can advise the operator to
    install or enable it.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the page to like.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "already_liked": bool}`` on success, where ``already_liked`` is
        ``False`` when this call added the like and ``True`` when the
        user had already liked the page (idempotent no-op). On guard
        rejection returns ``{"success": False, "error_code": ...,
        "message": ..., "details": {...}}`` with no outbound HTTP
        issued. When the Likes plugin is unavailable, returns the
        structured ``plugin_unavailable`` envelope described above.
        Other upstream failures return
        ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    if err := check_read_only(_CONFLUENCE_LIKES_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        result = confluence.like_page(page_id)
    except LikesPluginUnavailableError as exc:
        return _confluence_likes_plugin_unavailable_response(exc)
    except Exception as e:
        logger.error(
            f"Error liking Confluence page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "page_id": str(page_id),
            "already_liked": bool(result.get("already_liked", False)),
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "write", "toolset:confluence_likes"},
    annotations={"title": "Unlike Page", "destructiveHint": False},
)
async def unlike_page(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence page ID to unlike (numeric ID from URL, e.g. "
                "'123456789'). The authenticated user's like is removed "
                "against the bundled Confluence Likes plugin at "
                "``DELETE /rest/likes/1.0/content/{page_id}/likes``."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Remove the authenticated user's like from a Confluence page.

    Write_Tool for Requirement 37.1 (the inverse of
    ``confluence_like_page``). Wraps :meth:`LikesMixin.unlike_page`,
    which issues ``DELETE /rest/likes/1.0/content/{page_id}/likes``
    against the plugin-bundled Likes endpoint family.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) — the endpoint is content-id-only.

    Plugin availability (Requirement 37.2): when the Confluence Likes
    plugin is absent or disabled, the mixin raises
    :class:`LikesPluginUnavailableError` (the endpoint returned 404).
    The tool catches it and surfaces a structured ``plugin_unavailable``
    error naming the plugin so the agent can advise the operator to
    install or enable it.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the page to unlike.

    Returns:
        JSON string with ``{"success": True, "page_id": ...,
        "unliked": True}`` on success. On guard rejection returns
        ``{"success": False, "error_code": ..., "message": ...,
        "details": {...}}`` with no outbound HTTP issued. When the
        Likes plugin is unavailable, returns the structured
        ``plugin_unavailable`` envelope naming the plugin. Other
        upstream failures return ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    if err := check_read_only(_CONFLUENCE_LIKES_WRITE_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        confluence.unlike_page(page_id)
    except LikesPluginUnavailableError as exc:
        return _confluence_likes_plugin_unavailable_response(exc)
    except Exception as e:
        logger.error(
            f"Error unliking Confluence page {page_id}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "page_id": str(page_id),
            "unliked": True,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Long-Task Polling (Req 38 — toolset:confluence_pages)
# =============================================================================
#
# Requirement 38 exposes a single Read_Tool — ``confluence_get_long_task`` —
# that lets an orchestrator poll the status of an asynchronous Confluence
# operation, most commonly a page move or a page-tree copy that returned
# a ``longTaskId`` from ``confluence_move_page`` / ``confluence_copy_page_tree``
# (Req 31.4). The tool wraps
# :meth:`LongTasksMixin.get_long_task`, which issues
# ``GET /rest/api/longtask/{long_task_id}`` and returns the DC status dict
# verbatim so every field (percentage complete, finished / successful
# flags, elapsed / remaining time, message records, additional details)
# is surfaced without re-serialization.
#
# The tool is registered under ``toolset:confluence_pages`` rather than a
# dedicated ``toolset:confluence_long_tasks`` so that agents which have
# already enabled page moves and copies get polling as part of the same
# capability bundle — the three tools form one end-to-end workflow and
# splitting them across toolsets would force operators to enable two
# toolsets to complete a single asynchronous operation.
#
# Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
# precheck before any outbound HTTP call. Even though this tool is tagged
# ``read`` and would not be blocked by ``READ_ONLY_MODE`` in the first
# place, running the guard keeps the precheck shape uniform across every
# tool in this server so a future change that flips the tag set cannot
# accidentally slip past the read-only gate. The space filter is
# intentionally skipped (Req 43): long-task records are not scoped to a
# space — they belong to whatever user initiated the asynchronous
# operation — so resolving a space key would require reading the
# ``additionalDetails`` payload, which is provider-specific and not
# guaranteed to be populated.
#
# Error handling (Req 38.2): the mixin raises
# :class:`LongTaskNotFoundError` on a 404 response, which DC returns both
# for ids it never issued and for completed tasks whose records have
# aged out of the long-task registry. The tool catches it and surfaces a
# structured ``long_task_not_found`` error naming the offending id so
# the agent can distinguish it from transport or auth failures. Other
# upstream failures fall through to the generic ``{"success": False,
# "error": ...}`` envelope.


from mcp_atlassian.confluence.long_tasks import (  # noqa: E402
    LongTaskNotFoundError,
)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Long Task Status", "readOnlyHint": True},
)
async def get_long_task(
    ctx: Context,
    long_task_id: Annotated[
        str,
        Field(
            description=(
                "Confluence long-task ID to poll (the value returned as "
                "``longTaskId`` by ``confluence_move_page`` or "
                "``confluence_copy_page_tree`` when the operation was "
                "dispatched asynchronously). Backed by "
                "``GET /rest/api/longtask/{long_task_id}``."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
) -> str:
    """Poll the status of a Confluence long-running task.

    Read_Tool for Requirement 38.1. Wraps
    :meth:`LongTasksMixin.get_long_task`, which issues
    ``GET /rest/api/longtask/{long_task_id}`` and returns the DC
    status envelope verbatim. The envelope carries at least the task
    id, ``percentageComplete`` (0-100), ``finished`` and ``successful``
    booleans, ``elapsedTime`` / ``remainingTime`` (milliseconds), the
    accumulated ``messages`` list, and an ``additionalDetails``
    payload whose contents depend on the originating operation (for
    example the destination page id for a page move).

    This tool lives in ``toolset:confluence_pages`` alongside
    ``confluence_move_page`` and ``confluence_copy_page_tree`` so that
    enabling page-level operations also enables the polling primitive
    needed to complete their asynchronous variants — the three tools
    form one end-to-end workflow.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) because long-task records are not
    scoped to a space; they belong to whatever user initiated the
    asynchronous operation.

    Error handling (Requirement 38.2): when Confluence returns 404 for
    the supplied id — which it does both for ids it never issued and
    for completed tasks whose records have aged out of the long-task
    registry — the mixin raises :class:`LongTaskNotFoundError` and
    this tool surfaces a structured ``long_task_not_found`` error
    naming the offending id so the agent can distinguish it from
    transport or auth failures.

    Args:
        ctx: The FastMCP context.
        long_task_id: Confluence long-task id returned by a prior
            asynchronous move or copy operation.

    Returns:
        JSON string with ``{"success": True, "long_task_id": ...,
        "status": {...}}`` on success, where ``status`` is the DC
        long-task status dict. On a 404 from the upstream endpoint
        returns the structured ``long_task_not_found`` envelope
        ``{"success": False, "error_code": "long_task_not_found",
        "message": ..., "details": {"long_task_id": ...}}`` with no
        further HTTP issued. On guard rejection returns
        ``{"success": False, "error_code": ..., "message": ...,
        "details": {...}}`` with no outbound HTTP issued. Other
        upstream failures return ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_pages"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        status = confluence.get_long_task(long_task_id)
    except LongTaskNotFoundError as exc:
        return json.dumps(
            {
                "success": False,
                "error_code": "long_task_not_found",
                "message": (
                    f"Confluence long-task id {exc.long_task_id!r} was "
                    f"not found. The id may never have been issued, or "
                    f"the task may have completed and been purged from "
                    f"the long-task registry."
                ),
                "details": {"long_task_id": exc.long_task_id},
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error(
            f"Error polling Confluence long task {long_task_id!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "long_task_id": str(long_task_id),
            "status": status,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Confluence Groups (Req 39 — toolset:confluence_groups, read-only)
# =============================================================================
#
# Requirement 39 exposes two Read_Tools — ``confluence_search_groups`` and
# ``confluence_get_user_groups`` — that let an agent reason about Confluence
# group membership without modifying it. Both tools wrap the read-only
# methods on :class:`GroupsMixin` (aliased in ``confluence.__init__`` as
# ``ConfluenceGroupsMixin`` to avoid a name collision with the Jira-side
# ``GroupsMixin`` exposed through ``JiraFetcher``):
#
#   * ``confluence_search_groups`` → ``GET /rest/api/group`` with an optional
#     ``prefix`` filter; used to discover groups by name-prefix.
#   * ``confluence_get_user_groups`` → ``GET /rest/api/user/memberof`` with
#     either the legacy ``username`` selector or the newer ``key`` (user
#     key) selector; used to enumerate a given user's group memberships.
#
# Requirement 39.2 explicitly forbids any Write_Tool in
# ``toolset:confluence_groups`` — there is no tool that creates, deletes or
# modifies groups, and no tool that adds or removes users from groups.
# Property 7 (forbidden-endpoint exclusion) enforces the same boundary at
# registration time, and :class:`GroupsMixin` itself does not expose any
# write methods, so the read-only contract is defended in depth.
#
# Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
# precheck before any outbound HTTP call. Even though both tools are
# tagged ``read`` and would not be blocked by ``READ_ONLY_MODE`` in the
# first place, running the guard keeps the precheck shape uniform across
# every tool in this server so a future change that flips the tag set
# cannot accidentally slip past the read-only gate.
#
# The space filter (``CONFLUENCE_SPACES_FILTER``) is intentionally skipped
# (Req 43): group records are instance-wide, not scoped to any single
# Confluence space, so there is no space key that could be matched against
# the operator's allow-list. Callers that need to constrain discovery by
# space should do so at a higher layer.


_CONFLUENCE_GROUPS_READ_TAGS: set[str] = {
    "confluence",
    "read",
    "toolset:confluence_groups",
}


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_groups"},
    annotations={"title": "Search Groups", "readOnlyHint": True},
)
async def search_groups(
    ctx: Context,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Optional name prefix to filter groups by. When omitted "
                "or empty, the first page of groups is returned ordered "
                "by name. Forwarded to Confluence DC as the ``prefix`` "
                "query parameter on ``GET /rest/api/group``."
            ),
            default=None,
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum number of groups to return in a single page. "
                "Forwarded as the ``limit`` query parameter."
            ),
            default=50,
            ge=1,
            le=1000,
        ),
    ] = 50,
) -> str:
    """Search Confluence groups by name prefix.

    Read_Tool for Requirement 39.1. Wraps
    :meth:`GroupsMixin.search_groups`, which issues
    ``GET /rest/api/group`` against Confluence DC with an optional
    ``prefix`` filter and returns the unwrapped ``results`` list from
    DC's paged envelope.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) because group records are
    instance-wide and carry no space key that could be matched against
    the operator's allow-list.

    Requirement 39.2 forbids any Write_Tool in this toolset — there is
    no companion create / delete / modify tool and no membership-mutation
    tool registered here.

    Args:
        ctx: The FastMCP context.
        query: Optional name prefix to filter groups by. When ``None``
            or empty, DC returns groups ordered by name.
        limit: Maximum number of groups to return in a single page
            (default 50).

    Returns:
        JSON string with ``{"success": True, "query": ..., "limit": ...,
        "count": N, "groups": [...]}`` on success, where ``groups`` is
        the list of group dicts returned by DC (each typically carrying
        at least ``name`` and ``type`` fields). On guard rejection
        returns ``{"success": False, "error_code": ..., "message": ...,
        "details": {...}}`` with no outbound HTTP issued. Other
        upstream failures return ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    if err := check_read_only(_CONFLUENCE_GROUPS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    confluence = await get_confluence_fetcher(ctx)
    try:
        groups = confluence.search_groups(query=query, limit=limit)
    except Exception as e:
        logger.error(
            f"Error searching Confluence groups query={query!r} "
            f"limit={limit}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "query": query,
            "limit": limit,
            "count": len(groups),
            "groups": groups,
        },
        indent=2,
        ensure_ascii=False,
    )


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_groups"},
    annotations={"title": "Get User Groups", "readOnlyHint": True},
)
async def get_user_groups(
    ctx: Context,
    username: Annotated[
        str | None,
        Field(
            description=(
                "Confluence DC username whose group memberships should "
                "be listed. Forwarded as the ``username`` query parameter "
                "on ``GET /rest/api/user/memberof``. Exactly one of "
                "``username`` or ``key`` must be provided."
            ),
            default=None,
        ),
    ] = None,
    key: Annotated[
        str | None,
        Field(
            description=(
                "Confluence DC user key whose group memberships should "
                "be listed. Forwarded as the ``key`` query parameter on "
                "``GET /rest/api/user/memberof``. Exactly one of "
                "``username`` or ``key`` must be provided."
            ),
            default=None,
        ),
    ] = None,
) -> str:
    """List the Confluence groups a given user is a member of.

    Read_Tool for Requirement 39.1. Wraps
    :meth:`GroupsMixin.get_user_groups_confluence`, which issues
    ``GET /rest/api/user/memberof`` against Confluence DC with either
    the legacy ``username`` selector or the ``key`` (user key)
    selector and returns the unwrapped ``results`` list from DC's
    paged envelope.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) because group memberships are
    instance-wide, not scoped to any single Confluence space.

    Requirement 39.2 forbids any Write_Tool in this toolset — there is
    no companion add-to-group or remove-from-group tool registered
    here.

    Args:
        ctx: The FastMCP context.
        username: Optional Confluence DC username selector.
        key: Optional Confluence DC user-key selector.

    Returns:
        JSON string with ``{"success": True, "username": ..., "key":
        ..., "count": N, "groups": [...]}`` on success, where
        ``groups`` is the list of group dicts returned by DC (each
        typically carrying at least ``name`` and ``type`` fields). On
        invalid input (neither ``username`` nor ``key`` provided) returns
        ``{"success": False, "error": ...}`` with no outbound HTTP
        issued. On guard rejection returns ``{"success": False,
        "error_code": ..., "message": ..., "details": {...}}`` with no
        outbound HTTP issued. Other upstream failures return
        ``{"success": False, "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    if err := check_read_only(_CONFLUENCE_GROUPS_READ_TAGS):
        return json.dumps({"success": False, **err.to_dict()})

    if username is None and key is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    "Exactly one of `username` or `key` must be provided."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )

    confluence = await get_confluence_fetcher(ctx)
    try:
        groups = confluence.get_user_groups_confluence(
            username=username, key=key
        )
    except Exception as e:
        logger.error(
            f"Error listing Confluence group memberships "
            f"username={username!r} key={key!r}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    return json.dumps(
        {
            "success": True,
            "username": username,
            "key": key,
            "count": len(groups),
            "groups": groups,
        },
        indent=2,
        ensure_ascii=False,
    )


# =============================================================================
# Page Descendants Tree (Req 40 — toolset:confluence_pages)
# =============================================================================
#
# Requirement 40 exposes a single Read_Tool — ``confluence_get_page_descendants``
# — that returns the descendants subtree of a Confluence page up to a
# caller-specified depth. The tool wraps
# :meth:`DescendantsMixin.get_page_descendants`, which issues
# ``GET /rest/api/content/{page_id}/descendant/page`` with a clamped
# ``depth`` and ``limit=25`` query parameters and returns the DC
# response dict verbatim so every field the endpoint surfaces (results,
# paging envelope, expandable links) is passed through without
# re-serialization.
#
# The tool lives in ``toolset:confluence_pages`` alongside the rest of
# the page-level capability bundle (content reads, moves, copies,
# long-task polling) so agents that have already enabled page
# operations pick up descendants-tree reads without having to turn on
# an additional toolset.
#
# Depth semantics (Req 40.2, 40.3):
#   * The default depth is 3 (Req 40.2). Omitting the argument returns
#     three levels of descendants.
#   * Depth is capped at :data:`MAX_DESCENDANTS_DEPTH` (= 10) at the
#     mixin boundary (Req 40.3). The server-tool layer compares the
#     caller-supplied value to the cap and emits a ``capped_depth: 10``
#     metadata field in the response whenever the caller asked for a
#     depth greater than 10, so the agent can detect that truncation
#     occurred without having to re-derive the cap from the response
#     shape.
#
# Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
# precheck before any outbound HTTP call. Even though this tool is
# tagged ``read`` and would not be blocked by ``READ_ONLY_MODE`` in the
# first place, running the guard keeps the precheck shape uniform
# across every tool in this server so a future change that flips the
# tag set cannot accidentally slip past the read-only gate. The space
# filter is intentionally skipped (Req 43) because the descendants
# endpoint is scoped to a single page id and the tool returns whatever
# pages DC's permission model surfaces for that subtree; any cross-space
# constraint should be enforced at a higher layer.


from mcp_atlassian.confluence.descendants import (  # noqa: E402
    MAX_DESCENDANTS_DEPTH,
)


@confluence_mcp.tool(
    tags={"confluence", "read", "toolset:confluence_pages"},
    annotations={"title": "Get Page Descendants", "readOnlyHint": True},
)
async def get_page_descendants(
    ctx: Context,
    page_id: Annotated[
        str,
        Field(
            description=(
                "Confluence content id of the root page whose descendants "
                "subtree should be fetched. Backed by "
                "``GET /rest/api/content/{page_id}/descendant/page``."
            )
        ),
        BeforeValidator(lambda x: str(x) if x is not None else None),
    ],
    depth: Annotated[
        int,
        Field(
            description=(
                "Maximum tree depth to traverse. Defaults to 3 per "
                "Requirement 40.2. Values greater than 10 are capped at "
                "10 (Requirement 40.3) and the response surfaces "
                "``capped_depth: 10`` so callers can detect truncation. "
                "Forwarded to Confluence DC as the ``depth`` query "
                "parameter."
            ),
            default=3,
            ge=1,
        ),
    ] = 3,
) -> str:
    """Fetch the descendants subtree for a Confluence page.

    Read_Tool for Requirement 40.1. Wraps
    :meth:`DescendantsMixin.get_page_descendants`, which issues
    ``GET /rest/api/content/{page_id}/descendant/page`` with a clamped
    ``depth`` and ``limit=25`` query parameters and returns the DC
    response dict verbatim.

    Depth semantics (Requirements 40.2, 40.3): the default depth is 3
    and callers asking for a depth greater than ``MAX_DESCENDANTS_DEPTH``
    (= 10) receive a response truncated at depth 10 with a
    ``capped_depth: 10`` metadata field naming the applied cap so the
    agent can detect the truncation without re-deriving it from the
    tree shape. The mixin silently clamps depth at its boundary so the
    upstream HTTP call always forwards a value in ``[1, 10]``.

    This tool is registered under ``toolset:confluence_pages`` so
    enabling page operations also enables descendants-tree reads —
    the two form one end-to-end capability for processing a page
    subtree in one pass.

    Prelude per design: ``check_read_only`` runs as a belt-and-suspenders
    precheck before any outbound HTTP call. The space filter is
    intentionally skipped (Req 43) because the descendants endpoint is
    scoped to a single page id; any cross-space constraint should be
    enforced at a higher layer.

    Args:
        ctx: The FastMCP context.
        page_id: Confluence content id of the root page.
        depth: Maximum tree depth to traverse (default 3, capped at
            10).

    Returns:
        JSON string with ``{"success": True, "page_id": ..., "depth":
        <applied>, "tree": {...}}`` on success, where ``depth`` is the
        effective (capped) depth that was sent upstream and ``tree``
        is the DC descendants payload. When the caller-supplied depth
        exceeded ``MAX_DESCENDANTS_DEPTH`` the envelope additionally
        carries ``"capped_depth": 10`` (Requirement 40.3). On guard
        rejection returns ``{"success": False, "error_code": ...,
        "message": ..., "details": {...}}`` with no outbound HTTP
        issued. Other upstream failures return ``{"success": False,
        "error": ...}``.
    """
    from mcp_atlassian.utils.dc_guards import check_read_only

    tool_tags = {"confluence", "read", "toolset:confluence_pages"}
    if err := check_read_only(tool_tags):
        return json.dumps({"success": False, **err.to_dict()})

    # Apply the same cap the mixin uses so the server-tool layer can
    # surface ``capped_depth`` metadata without relying on the response
    # shape. The lower bound is left to the pydantic validator
    # (``ge=1``) and the mixin's own clamp so this layer only needs to
    # reason about the upper bound.
    effective_depth = min(depth, MAX_DESCENDANTS_DEPTH)

    confluence = await get_confluence_fetcher(ctx)
    try:
        tree = confluence.get_page_descendants(
            str(page_id), depth=effective_depth
        )
    except Exception as e:
        logger.error(
            f"Error fetching Confluence page descendants "
            f"page_id={page_id!r} depth={depth}: {e}",
            exc_info=True,
        )
        return json.dumps({"success": False, "error": str(e)})

    payload: dict[str, Any] = {
        "success": True,
        "page_id": str(page_id),
        "depth": effective_depth,
        "tree": tree,
    }
    if depth > MAX_DESCENDANTS_DEPTH:
        payload["capped_depth"] = MAX_DESCENDANTS_DEPTH

    return json.dumps(payload, indent=2, ensure_ascii=False)
