"""Commit and diff operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/commits``
and the associated ``/rest/build-status/latest`` / ``/rest/search/latest``
plugins. Cloud paths target ``/2.0/repositories/{workspace}/{slug}/...``
and ``/2.0/workspaces/{workspace}/search/code`` (Requirements 11.1,
11.2, 11.3, 11.4, 11.6, 11.7, 11.11). The agent-facing method
signatures, parameter names, and return types do not change between
modes; Cloud payloads are passed through :func:`normalize_commit` so
downstream code keeps consuming the DC shape.

The DC-only :meth:`cherry_pick_commit` mixin method intentionally lives
outside this module (see ``cherry_pick.py``); it is guarded at the
server-tool layer in task 17.1 and never receives a Cloud branch.
"""

import logging
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_commit

logger = logging.getLogger("mcp-atlassian.bitbucket.commits")


# Characters rejected by :func:`_validate_compare_ref` (Requirement 11.5).
# The Cloud ``/diff/{spec}`` endpoint embeds the ref value directly into the
# URL path as part of ``{to}..{from}``; any of ``/``, ``?``, ``#`` or
# whitespace would change how the path is parsed by the router and must be
# rejected before any HTTP call.
_INVALID_REF_CHARS: frozenset[str] = frozenset("/?#")


def _validate_compare_ref(argument: str, value: str) -> None:
    """Pre-HTTP validation for ``from``/``to`` args on Cloud compare_commits.

    Requirement 11.5: in CloudMode, when either side of the compare spec is
    empty or contains any of ``/``, ``?``, ``#`` or whitespace, the tool
    SHALL emit a structured ``invalid_target`` error before issuing any HTTP
    call. The mixin signals this by raising :class:`ValueError` with an
    ``invalid_target:`` prefix; the server-tool layer catches the prefix and
    maps it onto a :class:`StructuredError` with
    ``error_code="invalid_target"``, matching the convention already used
    for ``filtered_out``.
    """
    if not value:
        raise ValueError(
            f"invalid_target: compare_commits argument {argument!r} is empty; "
            "Cloud requires a non-empty ref to form the {to}..{from} spec."
        )
    for char in value:
        if char in _INVALID_REF_CHARS or char.isspace():
            raise ValueError(
                f"invalid_target: compare_commits argument {argument!r} "
                f"contains illegal character {char!r}; Cloud rejects any of "
                "'/', '?', '#' or whitespace in a diff spec."
            )


def _resolve_workspace(
    project_key: str | None,
    config_workspace: str | None,
) -> str:
    """Resolve the Cloud workspace for a Bitbucket tool call.

    Precedence rules from Requirements 2.4 / 2.5 / 2.6:

    1. A non-empty ``project_key`` argument wins — it is interpreted as the
       workspace slug in Cloud mode.
    2. Otherwise ``config_workspace`` (populated from ``BITBUCKET_WORKSPACE``
       or the URL path by :meth:`BitbucketConfig.from_env`) is used.
    3. When both are empty/``None``, the mixin raises ``ValueError`` with a
       ``filtered_out:`` prefix so the server layer can map it onto a
       :class:`StructuredError` with ``error_code="filtered_out"`` before
       any outbound HTTP call.
    """
    if project_key:
        return project_key
    if config_workspace:
        return config_workspace
    raise ValueError(
        "filtered_out: Bitbucket Cloud workspace is required. "
        "Pass a non-empty project_key or set BITBUCKET_WORKSPACE."
    )


class CommitsMixin(BitbucketClient):
    """Mixin providing commit and diff operations for Bitbucket DC and Cloud."""

    def get_commits(
        self,
        project_key: str,
        repo_slug: str,
        until: str | None = None,
        since: str | None = None,
        path: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List commits in a repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            until: Optional commit hash or branch to list commits up to (inclusive)
            since: Optional commit hash or branch to list commits from (exclusive)
            path: Optional file path to filter commits affecting this path
            limit: Maximum number of results per page

        Returns:
            List of commit objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/commits"
            cloud_params: dict[str, Any] = {}
            # Cloud's ``commits`` endpoint accepts ``include``/``exclude``
            # query args for ref-range filtering (``until`` maps to
            # ``include``; ``since`` maps to ``exclude``). ``path`` is
            # filtered client-side on Cloud, so we forward it as-is only
            # when the API accepts it; otherwise Cloud ignores the param.
            if until:
                cloud_params["include"] = until
            if since:
                cloud_params["exclude"] = since
            if path:
                cloud_params["path"] = path
            return self._get_paged_results(
                url, params=cloud_params, limit=limit, normalizer=normalize_commit
            )

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/commits"
        params: dict[str, Any] = {}
        if until:
            params["until"] = until
        if since:
            params["since"] = since
        if path:
            params["path"] = path

        return self._get_paged_results(url, params=params, limit=limit)

    def get_commit(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
    ) -> dict[str, Any]:
        """Get a single commit by hash.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: The commit hash

        Returns:
            Commit object (normalized to the DC shape on Cloud).

        Raises:
            ValueError: If the commit is not found
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/commit/{commit_id}"
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response for commit {commit_id}: {raw}")
            normalized = normalize_commit(raw)
            # ``normalize_commit`` only returns ``None`` for a ``None``
            # input, which cannot occur here because ``raw`` is a dict.
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/commits/{commit_id}"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response for commit {commit_id}: {result}")
        return result

    def get_commit_changes(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get the list of changed files in a commit.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            commit_id: The commit hash
            limit: Maximum number of results per page

        Returns:
            List of change objects with file paths and change types
        """
        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/commits/{commit_id}/changes"
        return self._get_paged_results(url, limit=limit)

    def get_diff(
        self,
        project_key: str,
        repo_slug: str,
        commit_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        path: str | None = None,
        context_lines: int = 3,
    ) -> str:
        """Get a diff for a commit or between two refs.

        When ``commit_id`` is supplied, this returns the commit diff
        (``/diff/{sha}`` on Cloud, ``/commits/{sha}.diff`` on DC). When
        ``commit_id`` is omitted and ``since``/``until`` are supplied,
        this returns a ref-range diff; Cloud renders this as
        ``/diff/{until}..{since}`` while DC uses its ``/diff`` endpoint
        with ``since``/``until`` query params.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            commit_id: Optional specific commit hash to get diff for
            since: Optional start ref for comparison
            until: Optional end ref for comparison
            path: Optional file path to limit diff to
            context_lines: Number of context lines around changes

        Returns:
            Diff content as string
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            # Cloud's commit-diff endpoint is ``/diff/{sha}``; the
            # ref-range form uses ``/diff/{to}..{from}``. Both return
            # unified-diff text.
            if commit_id:
                spec = commit_id
            else:
                # Map DC ``since`` / ``until`` semantics onto Cloud's
                # ``{to}..{from}`` spec. DC treats ``since`` as the
                # exclusive start (from) and ``until`` as the inclusive
                # end (to), which matches Cloud's ``diff/{to}..{from}``
                # ordering.
                if not (since and until):
                    raise ValueError(
                        "get_diff on Cloud requires either commit_id, or "
                        "both since and until."
                    )
                spec = f"{until}..{since}"
            url = f"/2.0/repositories/{workspace}/{repo_slug}/diff/{spec}"
            params: dict[str, Any] = {"context": context_lines}
            if path:
                params["path"] = path

            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                params=params,
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            return response.text

        if commit_id:
            url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/commits/{commit_id}.diff"
        else:
            url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/diff"

        params: dict[str, Any] = {"contextLines": context_lines}
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if path:
            params["srcPath"] = path

        response = self.bitbucket._session.get(
            f"{self.config.url}{url}",
            params=params,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        return response.text

    def get_commit_build_status(
        self,
        commit_id: str,
        limit: int = 25,
        project_key: str | None = None,
        repo_slug: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch CI build statuses reported against a commit.

        On DC this uses the global ``/rest/build-status`` API, which is
        addressed purely by commit SHA. On Cloud, build statuses live
        under the per-repository commit resource, so ``project_key``
        (workspace) and ``repo_slug`` are resolved from config / args
        (Requirement 11.6).

        Args:
            commit_id: Full commit SHA-1
            limit: Maximum number of results per page
            project_key: The project key (DC — ignored) or workspace slug
                (Cloud). Optional; on Cloud, falls back to
                ``config.workspace`` via :func:`_resolve_workspace`.
            repo_slug: The repository slug. Required on Cloud; ignored on
                DC which looks up by commit SHA alone.

        Returns:
            List of build status entries (state, key, name, url, ...).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            if not repo_slug:
                raise ValueError(
                    "get_commit_build_status on Cloud requires repo_slug."
                )
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/statuses"
            )
            return self._get_paged_results(url, limit=limit)

        url = f"/rest/build-status/latest/commits/{commit_id}"
        return self._get_paged_results(url, limit=limit)

    def post_commit_build_status(
        self,
        commit_id: str,
        state: str,
        key: str,
        name: str | None = None,
        url: str | None = None,
        description: str | None = None,
        project_key: str | None = None,
        repo_slug: str | None = None,
    ) -> bool:
        """Publish a CI build status against a commit.

        The DC fields ``key``/``url``/``state``/``name``/``description``
        map 1:1 to the Cloud body shape; ``state`` values
        ``SUCCESSFUL``/``FAILED``/``INPROGRESS`` are identical on both
        sides (Requirement 11.7).

        Args:
            commit_id: Full commit SHA-1
            state: One of ``SUCCESSFUL``, ``INPROGRESS``, ``FAILED``
            key: Stable identifier for the build (e.g. CI job key)
            name: Optional human-readable name
            url: Optional URL pointing back to the CI run
            description: Optional short description
            project_key: The project key (DC — ignored) or workspace slug
                (Cloud). Optional; on Cloud, falls back to
                ``config.workspace`` via :func:`_resolve_workspace`.
            repo_slug: The repository slug. Required on Cloud; ignored on
                DC which posts by commit SHA alone.

        Returns:
            True on success (the API returns 204 No Content on DC; Cloud
            returns the created status object, which is discarded here in
            favour of a boolean for shape-parity with DC).
        """
        data: dict[str, Any] = {"state": state, "key": key}
        if name:
            data["name"] = name
        if url:
            data["url"] = url
        if description:
            data["description"] = description

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            if not repo_slug:
                raise ValueError(
                    "post_commit_build_status on Cloud requires repo_slug."
                )
            endpoint = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/commit/{commit_id}/statuses/build"
            )
            self.bitbucket.post(endpoint, data=data)
            return True

        endpoint = f"/rest/build-status/latest/commits/{commit_id}"
        self.bitbucket.post(endpoint, data=data)
        return True

    def search_code(
        self,
        query: str,
        project_key: str | None = None,
        repo_slug: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search for code across repositories.

        On DC this targets the Bitbucket code-search plugin at
        ``/rest/search/latest/search`` (requires Elasticsearch). On
        Cloud it targets the workspace-scoped code search at
        ``GET /2.0/workspaces/{workspace}/search/code`` (Requirement
        11.11). The workspace is resolved from ``project_key`` (if set)
        or ``config.workspace``.

        Args:
            query: Search query string
            project_key: Optional project key (DC) or workspace slug
                (Cloud) to limit search scope
            repo_slug: Optional repository slug to limit search scope
            limit: Maximum number of results

        Returns:
            List of search result objects
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/workspaces/{workspace}/search/code"
            # Cloud's search DSL accepts free-text under ``search_query``;
            # repo-scoping is expressed inline via ``repo:{slug}`` so we
            # keep the agent-visible ``repo_slug`` parameter intact.
            search_query = query
            if repo_slug:
                search_query = f"{query} repo:{repo_slug}"
            params: dict[str, Any] = {
                "search_query": search_query,
                "pagelen": limit,
            }
            result = self.bitbucket.get(url, params=params)
            if isinstance(result, dict):
                return result.get("values", [])
            return []

        url = "/rest/search/latest/search"
        params = {
            "query": query,
            "type": "code",
            "limit": limit,
        }

        if project_key:
            params["projectKey"] = project_key
        if repo_slug and project_key:
            params["repoSlug"] = repo_slug

        result = self.bitbucket.get(url, params=params)
        if isinstance(result, dict):
            return result.get("values", [])
        return []

    # ------------------------------------------------------------------
    # Compare refs (branches, tags, commits)
    # ------------------------------------------------------------------

    def compare_commits(
        self,
        project_key: str,
        repo_slug: str,
        from_ref: str,
        to_ref: str,
        from_repo: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List commits reachable from ``from_ref`` but not ``to_ref``.

        Wraps the dedicated ``/compare/commits`` endpoint on DC, which is
        purpose-built for "what's in X that isn't in Y?" questions —
        release diffs, cherry-pick planning and cross-fork comparison.

        On Cloud there is no ``/compare/commits`` resource; the closest
        equivalent is the diff-by-spec endpoint
        ``GET /2.0/repositories/{workspace}/{slug}/diff/{to}..{from}``
        (Requirement 11.4). The Cloud response is unified-diff text, not a
        commit list, so on Cloud this method returns an empty list when
        the refs have no divergence and a single-element list carrying
        the raw diff under a ``diff`` key otherwise. Downstream tools
        that expect a commit-list shape should call
        ``get_commits(..., until=from_ref, since=to_ref)`` instead — that
        path returns a true commit list on both modes.

        The :mod:`invalid_target` pre-check that validates ``from_ref`` /
        ``to_ref`` shape runs before any outbound HTTP call on Cloud
        (Requirement 11.5). Empty values and values containing ``/``,
        ``?``, ``#`` or whitespace raise :class:`ValueError` with an
        ``invalid_target:`` prefix, which the server-tool layer maps
        onto a structured ``invalid_target`` error envelope. DC calls
        are unchanged — the DC ``/compare/commits`` endpoint carries the
        refs as query parameters, which makes the pre-check unnecessary
        there.

        Args:
            project_key: Target project key (DC) or workspace slug (Cloud)
            repo_slug: Target repository slug
            from_ref: Source ref (branch, tag or commit)
            to_ref: Target ref (branch, tag or commit)
            from_repo: Optional ``(project_key, repo_slug)`` tuple when
                ``from_ref`` lives in a fork (DC only)
            limit: Page size

        Returns:
            List of commit objects in compare order (DC), or a
            single-element list containing ``{"diff": <unified-diff>}``
            when running against Cloud and the refs have divergence.
        """
        if self.is_cloud:
            _validate_compare_ref("from", from_ref)
            _validate_compare_ref("to", to_ref)
            workspace = _resolve_workspace(project_key, self.config.workspace)
            spec = f"{to_ref}..{from_ref}"
            url = f"/2.0/repositories/{workspace}/{repo_slug}/diff/{spec}"
            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            diff_text = response.text
            if not diff_text:
                return []
            return [{"diff": diff_text}]

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/compare/commits"
        )
        params: dict[str, Any] = {"from": from_ref, "to": to_ref}
        if from_repo:
            params["fromRepo"] = f"{from_repo[0]}/{from_repo[1]}"
        return self._get_paged_results(url, params=params, limit=limit)

    def compare_changes(
        self,
        project_key: str,
        repo_slug: str,
        from_ref: str,
        to_ref: str,
        from_repo: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List changed files between two refs.

        Args:
            project_key: Target project key
            repo_slug: Target repository slug
            from_ref: Source ref
            to_ref: Target ref
            from_repo: Optional ``(project_key, repo_slug)`` tuple for forks
            limit: Page size

        Returns:
            List of change objects (``path``, ``type``, ...).
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/compare/changes"
        )
        params: dict[str, Any] = {"from": from_ref, "to": to_ref}
        if from_repo:
            params["fromRepo"] = f"{from_repo[0]}/{from_repo[1]}"
        return self._get_paged_results(url, params=params, limit=limit)

    def compare_diff(
        self,
        project_key: str,
        repo_slug: str,
        from_ref: str,
        to_ref: str,
        path: str | None = None,
        from_repo: tuple[str, str] | None = None,
        context_lines: int = 3,
    ) -> str:
        """Return a unified diff between two refs.

        Args:
            project_key: Target project key
            repo_slug: Target repository slug
            from_ref: Source ref
            to_ref: Target ref
            path: Optional path filter
            from_repo: Optional ``(project_key, repo_slug)`` tuple for forks
            context_lines: Diff context lines

        Returns:
            Unified diff content.
        """
        suffix = f"/{path.lstrip('/')}" if path else ""
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/compare/diff{suffix}"
        )
        params: dict[str, Any] = {
            "from": from_ref,
            "to": to_ref,
            "contextLines": context_lines,
        }
        if from_repo:
            params["fromRepo"] = f"{from_repo[0]}/{from_repo[1]}"

        response = self.bitbucket._session.get(
            f"{self.config.url}{url}",
            params=params,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        return response.text
