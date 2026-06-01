"""Markup preview operations for Bitbucket Data Center.

Wraps the ``/rest/api/latest/markup/preview`` endpoint, which renders
Bitbucket-flavoured markup (Markdown) to HTML. The call is idempotent —
no repository state is changed — so the server layer exposes this as a
Read_Tool under ``toolset:bitbucket_repositories`` (see Requirement 9).
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.markup")


class MarkupMixin(BitbucketClient):
    """Mixin providing markup-to-HTML preview for Bitbucket DC."""

    def render_markup(
        self,
        markup_text: str,
        project_key: str | None = None,
        repo_slug: str | None = None,
        page_type: str = "COMMENT",
    ) -> str:
        """Render Bitbucket markup to HTML.

        POSTs ``markup_text`` to ``/rest/api/latest/markup/preview`` along
        with an optional rendering context (project, repository, page type)
        so Bitbucket can resolve relative links, mentions, and emoji the
        same way it does when the markup is actually posted.

        Args:
            markup_text: The raw markup to render.
            project_key: Optional project key for the rendering context.
            repo_slug: Optional repository slug for the rendering context.
                Ignored by Bitbucket unless ``project_key`` is also set.
            page_type: Rendering surface hint (for example ``"COMMENT"``,
                ``"PULL_REQUEST"``, ``"README"``). Defaults to
                ``"COMMENT"``, matching the Bitbucket DC default.

        Returns:
            The rendered HTML as a string. Bitbucket DC returns the HTML
            body directly for this endpoint, but some deployments wrap it
            in a JSON envelope; both shapes are normalized to a plain
            string here.
        """
        url = "/rest/api/latest/markup/preview"
        data: dict[str, Any] = {"markup": markup_text}
        params: dict[str, Any] = {}
        if project_key is not None:
            params["projectKey"] = project_key
        if repo_slug is not None:
            params["repoSlug"] = repo_slug
        if page_type:
            params["pageType"] = page_type

        result = self.bitbucket.post(url, data=data, params=params or None)

        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("html", "body", "rendered", "preview"):
                value = result.get(key)
                if isinstance(value, str):
                    return value
            logger.debug(
                "Bitbucket markup preview returned dict without a known "
                "HTML field; keys=%s",
                sorted(result.keys()),
            )
            return ""
        raise ValueError(
            f"Unexpected response type from markup preview: {type(result).__name__}"
        )
