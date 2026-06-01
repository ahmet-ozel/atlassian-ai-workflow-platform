"""Commit comment operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/commits/{sha}/comments``.
Cloud paths target ``/2.0/repositories/{workspace}/{slug}/commit/{sha}/comments``
(Requirements 11.8, 11.9). The agent-facing method signatures, parameter
names, and return types do not change between modes; Cloud payloads are
passed through :func:`_normalize_cloud_comment` (which itself delegates
to :func:`normalize_user` for the author) so downstream code keeps
consuming a DC-ish shape (``text``, ``author``, ``id``).

Cloud comment body shape for create / update is
``{"content": {"raw": "<markdown>"}, "inline": {...optional}}``; the DC
``path`` / ``line`` / ``line_type`` / ``file_type`` inline fields are
mapped onto Cloud's ``inline.path`` plus ``inline.to`` (new-side line)
or ``inline.from`` (old-side line) depending on the requested diff side.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import BitbucketClient
from .response_normalizer import normalize_user

logger = logging.getLogger("mcp-atlassian.bitbucket.commit_comments")


class NotCommentAuthorError(Exception):
    """Raised when Bitbucket rejects a commit-comment delete with 401/403.

    Both Bitbucket DC and Bitbucket Cloud enforce that only the original
    comment author (or an admin) may delete a commit comment. When the
    authenticated caller is neither, the endpoint returns
    ``401 Unauthorized`` or ``403 Forbidden``. The mixin translates that
    status into this typed exception so the server-tool layer can map it
    onto a structured ``not_comment_author`` error code (Requirement 8.5
    of the dc-tool-parity spec) without inspecting HTTP status codes
    from inside the tool function.

    The ownership check itself is always performed by the remote
    Bitbucket server (DC or Cloud) — DC compares by ``username`` / PAT
    identity, Cloud compares by OAuth / App-Password ``account_id``.
    This module does not duplicate that check client-side; it only
    normalises the resulting HTTP error into the mode-independent
    exception type above.
    """


def _resolve_workspace(
    project_key: str | None,
    config_workspace: str | None,
) -> str:
    """Resolve the Cloud workspace for a Bitbucket tool call.

    Precedence rules from Requirements 2.4 / 2.5 / 2.6:

    1. A non-empty ``project_key`` argument wins — it is interpreted as
       the workspace slug in Cloud mode.
    2. Otherwise ``config_workspace`` (populated from
       ``BITBUCKET_WORKSPACE`` or the URL path by
       :meth:`BitbucketConfig.from_env`) is used.
    3. When both are empty/``None``, the mixin raises ``ValueError``
       with a ``filtered_out:`` prefix so the server layer can map it
       onto a :class:`StructuredError` with ``error_code="filtered_out"``
       before any outbound HTTP call.
    """
    if project_key:
        return project_key
    if config_workspace:
        return config_workspace
    raise ValueError(
        "filtered_out: Bitbucket Cloud workspace is required. "
        "Pass a non-empty project_key or set BITBUCKET_WORKSPACE."
    )


def _build_cloud_inline(
    path: str | None,
    line: int | None,
    line_type: str | None,
    file_type: str | None,
) -> dict[str, Any] | None:
    """Translate DC ``path``/``line``/``line_type``/``file_type`` onto a
    Cloud ``inline`` object.

    Cloud's inline shape is ``{"path": "<file>", "to": <int>}`` for
    new-side (post-change) line anchors and ``{"path": "<file>",
    "from": <int>}`` for old-side (pre-change) line anchors. DC's
    ``line_type`` values ``ADDED`` / ``CONTEXT`` map onto ``to`` and
    ``REMOVED`` maps onto ``from``. ``file_type`` (``FROM`` / ``TO``)
    takes precedence when supplied, since it is the DC field whose
    semantics line up exactly with Cloud's ``from`` / ``to`` axis.

    Returns ``None`` when no inline fields were supplied — the resulting
    comment is a general (non-inline) commit comment.
    """
    if path is None and line is None and line_type is None and file_type is None:
        return None

    inline: dict[str, Any] = {}
    if path is not None:
        inline["path"] = path
    if line is not None:
        # Pick the ``from`` / ``to`` axis. ``file_type`` is the most
        # explicit signal ("which side of the diff am I on?"); fall back
        # to ``line_type`` semantics otherwise.
        axis: str
        if file_type == "FROM":
            axis = "from"
        elif file_type == "TO":
            axis = "to"
        elif line_type == "REMOVED":
            axis = "from"
        else:
            # ADDED, CONTEXT, or unspecified default to the new side.
            axis = "to"
        inline[axis] = line
    return inline


def _normalize_cloud_comment(c: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Cloud commit-comment payload to a DC-ish shape.

    Cloud comment shape::

        {"id": 42, "content": {"raw": "<md>", "markup": ..., "html": ...},
         "user": {...}, "inline": {"path": ..., "to": 7}, "created_on": ..., "updated_on": ...}

    DC commit-comment shape exposes ``text`` (the comment body) and
    ``author`` (the user object). We preserve every Cloud key
    (passthrough) and additionally synthesize ``text``/``author`` so
    downstream server-tool assembly keeps reading a familiar shape.
    The author is passed through :func:`normalize_user` so it exposes
    both Cloud (``account_id``, ``display_name``) and DC (``name``,
    ``slug``, ``displayName``) identifier keys.
    """
    if not isinstance(c, dict):
        return c  # type: ignore[unreachable]

    out: dict[str, Any] = dict(c)

    content = c.get("content")
    if isinstance(content, dict):
        raw = content.get("raw")
        if isinstance(raw, str):
            out.setdefault("text", raw)

    user = c.get("user")
    if isinstance(user, dict):
        out.setdefault("author", normalize_user(user))

    return out


class CommitCommentsMixin(BitbucketClient):
    """Mixin providing commit comment operations for Bitbucket DC and Cloud.

    Wraps the ``/commits/{commit_id}/comments`` endpoint family on DC and
    the ``/commit/{sha}/comments`` endpoint family on Cloud. Commit
    comments live alongside the commit itself and are distinct from
    pull-request comments; a commit-level thread survives even if the PR
    containing the commit is later deleted.

    Inline (per-line) comments are supported by supplying
    ``path``/``line``/``line_type``/``file_type`` on
    :meth:`add_commit_comment`; on DC those fields are bundled into the
    ``anchor`` object Bitbucket expects, on Cloud they are mapped onto
    the ``inline`` object (see :func:`_build_cloud_inline`). Omitting
    all four creates a general (non-inline) commit comment on both
    modes.

    Authorization rules (for example, only the comment author or an
    admin may delete a comment) are enforced upstream by Bitbucket; on
    delete, this mixin translates the resulting 401/403 responses into
    :class:`NotCommentAuthorError` so the server-tool layer can surface
    a structured ``not_comment_author`` error on both DC and Cloud.
    """

    def list_commit_comments(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        *,
        path: str | None = None,
    ) -> list[dict[str, Any]]:
        """List comments attached to a commit.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: The commit hash
            path: Optional file path to scope results to inline comments
                on that path; omit for all comments (general + inline)

        Returns:
            List of comment objects, paginated across all pages. On
            Cloud each comment is normalized via
            :func:`_normalize_cloud_comment` so it carries ``text`` and
            ``author`` alongside the original Cloud keys.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/comments"
            )
            params: dict[str, Any] = {}
            if path:
                # Cloud filter DSL: match inline comments on a given file.
                params["q"] = f'inline.path="{path}"'
            return self._get_paged_results(
                url,
                params=params,
                normalizer=_normalize_cloud_comment,
            )

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/comments"
        )
        params = {}
        if path:
            params["path"] = path

        return self._get_paged_results(url, params=params)

    def add_commit_comment(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        *,
        text: str,
        path: str | None = None,
        line: int | None = None,
        line_type: str | None = None,
        file_type: str | None = None,
    ) -> dict[str, Any]:
        """Create a general or inline comment on a commit.

        When any of ``path``, ``line``, ``line_type`` or ``file_type``
        are supplied, they are placed inside an ``anchor`` object on DC
        (so Bitbucket DC treats the comment as inline) or an ``inline``
        object on Cloud (see :func:`_build_cloud_inline` for the DC→Cloud
        mapping). If all four are ``None`` the comment is created as a
        general commit comment on both modes.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: The commit hash to attach the comment to
            text: Comment body
            path: Optional file path for inline anchoring
            line: Optional line number inside ``path``
            line_type: Optional line diff type (``ADDED``, ``REMOVED``,
                ``CONTEXT``)
            file_type: Optional file side (``FROM`` or ``TO``)

        Returns:
            Created comment object. On Cloud the payload is normalized
            via :func:`_normalize_cloud_comment`.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/comments"
            )
            data: dict[str, Any] = {"content": {"raw": text}}
            inline = _build_cloud_inline(path, line, line_type, file_type)
            if inline is not None:
                data["inline"] = inline

            result = self.bitbucket.post(url, data=data)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response adding comment to commit {commit_id}: {result}"
                )
            return _normalize_cloud_comment(result)

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/comments"
        )
        data = {"text": text}

        anchor: dict[str, Any] = {}
        if path is not None:
            anchor["path"] = path
        if line is not None:
            anchor["line"] = line
        if line_type is not None:
            anchor["lineType"] = line_type
        if file_type is not None:
            anchor["fileType"] = file_type
        if anchor:
            data["anchor"] = anchor

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response adding comment to commit {commit_id}: {result}"
            )
        return result

    def update_commit_comment(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        comment_id: int,
        *,
        text: str,
        version: int,
    ) -> dict[str, Any]:
        """Update the text of an existing commit comment.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: The commit hash the comment is attached to
            comment_id: The comment ID to update
            text: Replacement comment text
            version: Current comment version (DC optimistic locking;
                ignored on Cloud, which has no optimistic-concurrency
                field on commit comments)

        Returns:
            Updated comment object. On Cloud the payload is normalized
            via :func:`_normalize_cloud_comment`.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/comments/{comment_id}"
            )
            data = {"content": {"raw": text}}

            result = self.bitbucket.put(url, data=data)
            if not isinstance(result, dict):
                raise ValueError(
                    f"Unexpected response updating commit comment {comment_id}: {result}"
                )
            return _normalize_cloud_comment(result)

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/comments/{comment_id}"
        )
        data = {"text": text, "version": version}

        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating commit comment {comment_id}: {result}"
            )
        return result

    def delete_commit_comment(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        comment_id: int,
        *,
        version: int,
    ) -> None:
        """Delete a commit comment.

        Bitbucket enforces author/admin authorization on both DC and
        Cloud. DC compares by ``username`` / PAT identity; Cloud
        compares by the authenticated user's ``account_id``. In either
        case, when the caller is not the comment author (or an admin)
        the server returns 401/403. This mixin translates either status
        into :class:`NotCommentAuthorError` so the server-tool layer can
        surface it as a structured ``not_comment_author`` error without
        inspecting HTTP status codes from inside the tool function.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: The commit hash the comment is attached to
            comment_id: The comment ID to delete
            version: Current comment version (DC optimistic locking;
                ignored on Cloud)

        Raises:
            NotCommentAuthorError: When Bitbucket responds with HTTP
                401 or 403, meaning the authenticated user is neither
                the comment author nor an admin.
            requests.exceptions.HTTPError: For any other non-success
                response; callers (the server-tool layer) map these
                onto the generic error envelope.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/comments/{comment_id}"
            )
            try:
                self.bitbucket.delete(url)
            except HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    logger.debug(
                        "delete_commit_comment (Cloud): Bitbucket returned %s "
                        "for %s/%s commit %s comment %s; treating as "
                        "not_comment_author",
                        status,
                        workspace,
                        repo_slug,
                        commit_id,
                        comment_id,
                    )
                    raise NotCommentAuthorError(
                        f"Authenticated user is not permitted to delete "
                        f"commit comment {comment_id} on {commit_id} "
                        f"(Bitbucket returned HTTP {status})."
                    ) from exc
                raise
            return

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/commits/{commit_id}/comments/{comment_id}"
        )
        try:
            self.bitbucket.delete(url, params={"version": version})
        except HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                logger.debug(
                    "delete_commit_comment: Bitbucket returned %s for "
                    "%s/%s commit %s comment %s; treating as "
                    "not_comment_author",
                    status,
                    project_key,
                    repo_slug,
                    commit_id,
                    comment_id,
                )
                raise NotCommentAuthorError(
                    f"Authenticated user is not permitted to delete "
                    f"commit comment {comment_id} on {commit_id} "
                    f"(Bitbucket returned HTTP {status})."
                ) from exc
            raise
