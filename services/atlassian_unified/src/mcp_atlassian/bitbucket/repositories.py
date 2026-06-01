"""Repository and project operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/...`` and the
associated ``/browse``, ``/raw``, ``/files`` sub-resources. Cloud paths
target ``/2.0/repositories/{workspace}/{slug}/...`` and ``/2.0/workspaces``
for the project-list tool (Requirements 8.1 - 8.7). The agent-facing method
signatures, parameter names, and return types do not change between modes;
Cloud payloads are passed through :func:`normalize_repository` so downstream
code keeps consuming the DC shape, including the synthetic ``project``
wrapper Cloud does not natively expose (Requirement 8.7).
"""

import logging
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_repository

logger = logging.getLogger("mcp-atlassian.bitbucket.repositories")


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


def _workspace_to_project(workspace: dict[str, Any]) -> dict[str, Any]:
    """Project the DC ``{key, name, description, public}`` shape onto a
    Cloud workspace payload.

    This is the workspace-list analogue of :func:`normalize_repository`'s
    synthetic project wrapper (Requirement 8.7): every Cloud workspace
    surfaces with ``key`` set to its ``slug`` so downstream server-tool
    code that reads ``p.get("key")`` keeps working unchanged. Unknown
    Cloud fields (``uuid``, ``links``, ``type``, ...) are passed through.
    """
    slug = workspace.get("slug")
    name = workspace.get("name", slug)
    out: dict[str, Any] = dict(workspace)
    out.setdefault("key", slug)
    out.setdefault("name", name)
    # Cloud workspaces are private by default; the DC ``public`` field has
    # no direct Cloud equivalent. Preserve any existing value and default
    # to ``False`` to match the DC-shape consumers expect.
    out.setdefault("public", False)
    out.setdefault("description", "")
    return out


class RepositoriesMixin(BitbucketClient):
    """Mixin providing repository and project operations for Bitbucket DC and Cloud."""

    def get_projects(self, limit: int = 25) -> list[dict[str, Any]]:
        """List all projects accessible to the authenticated user.

        On DC this calls ``GET /rest/api/latest/projects`` and returns the
        DC project envelope unchanged.

        On Cloud, there is no "project" primitive — the closest equivalent
        was the list of workspaces the authenticated user could access via
        ``GET /2.0/workspaces``. Atlassian removed that endpoint in
        CHANGE-2770 (September 2025) and it now returns HTTP 410 Gone.
        There is no replacement API for "list every workspace I can see";
        clients must be given a workspace identifier explicitly. The
        Cloud branch therefore raises ``ValueError("not_supported_on_cloud: ...")``
        before any outbound HTTP so callers see an actionable error
        instead of a 410. The tool-layer at ``servers/bitbucket.py``
        additionally emits a structured ``not_supported_on_cloud`` error
        before reaching the mixin.

        Args:
            limit: Maximum number of results per page (DC only).

        Returns:
            List of project objects (DC).

        Raises:
            ValueError: On Cloud — the underlying endpoint was removed
                by Atlassian (CHANGE-2770).
        """
        if self.is_cloud:
            raise ValueError(
                "not_supported_on_cloud: bitbucket_list_projects has no "
                "Cloud equivalent. Atlassian removed GET /2.0/workspaces "
                "in CHANGE-2770 (September 2025). On Cloud, pass a "
                "workspace slug explicitly to workspace-scoped tools "
                "instead (for example bitbucket_list_repositories with "
                "project_key=<workspace-slug>)."
            )

        url = "/rest/api/latest/projects"
        projects = self._get_paged_results(url, limit=limit)

        # Apply project filter if configured
        if self.config.projects_filter:
            filter_keys = [
                k.strip().upper()
                for k in self.config.projects_filter.split(",")
            ]
            projects = [
                p for p in projects
                if p.get("key", "").upper() in filter_keys
            ]

        return projects

    def get_project(self, project_key: str) -> dict[str, Any]:
        """Get a single project by key.

        On Cloud, ``project_key`` is interpreted as a workspace slug and
        the method fetches ``GET /2.0/workspaces/{workspace}``, projecting
        the response into the DC-shaped project envelope so callers see
        the same fields in either mode (Requirement 8.7).

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)

        Returns:
            Project object

        Raises:
            ValueError: If the project is not found
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/workspaces/{workspace}"
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response for workspace {workspace}: {raw}"
                )
            return _workspace_to_project(raw)

        url = f"/rest/api/latest/projects/{project_key}"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response for project {project_key}: {result}")
        return result

    def get_repositories(
        self,
        project_key: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List repositories in a project.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            limit: Maximum number of results per page

        Returns:
            List of repository objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}"
            return self._get_paged_results(
                url,
                limit=limit,
                normalizer=lambda r: normalize_repository(r, workspace=workspace),
            )

        url = f"/rest/api/latest/projects/{project_key}/repos"
        return self._get_paged_results(url, limit=limit)

    def get_repository(
        self,
        project_key: str,
        repo_slug: str,
    ) -> dict[str, Any]:
        """Get a single repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug

        Returns:
            Repository object (normalized to the DC shape on Cloud).

        Raises:
            ValueError: If the repository is not found
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}"
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response for repo {workspace}/{repo_slug}: {raw}"
                )
            normalized = normalize_repository(raw, workspace=workspace)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response for repo {project_key}/{repo_slug}: {result}"
            )
        return result

    def search_repositories(
        self,
        query: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search for repositories by name.

        On Cloud, repository search is workspace-scoped: the method calls
        ``GET /2.0/repositories/{workspace}?q=name~"{query}"`` using the
        default workspace resolved from ``BitbucketConfig.workspace``. DC's
        cross-project ``GET /rest/api/latest/repos?name=...`` endpoint has
        no Cloud equivalent, so operators using the Cloud branch must
        configure a default workspace (or accept a pre-HTTP ``filtered_out``
        when neither ``project_key`` nor ``BITBUCKET_WORKSPACE`` is set).

        Args:
            query: Search query string
            limit: Maximum number of results per page

        Returns:
            List of matching repository objects (normalized to the DC
            shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(None, self.config.workspace)
            url = f"/2.0/repositories/{workspace}"
            # Cloud uses the BBQL-like query DSL: ``q=name~"<text>"`` for a
            # case-insensitive substring match on the repository name.
            params = {"q": f'name~"{query}"'}
            return self._get_paged_results(
                url,
                params=params,
                limit=limit,
                normalizer=lambda r: normalize_repository(r, workspace=workspace),
            )

        url = "/rest/api/latest/repos"
        params = {"name": query}
        return self._get_paged_results(url, params=params, limit=limit)

    def get_file_content(
        self,
        project_key: str,
        repo_slug: str,
        file_path: str,
        at: str | None = None,
        max_lines: int | None = None,
    ) -> str:
        """Get the content of a file in a repository.

        On DC the ``/browse/{path}`` endpoint paginates lines (default
        page size is ~1000); fetching only the first page silently
        truncates large files, so this method walks every page until
        ``isLastPage`` is true.

        On Cloud the file-content endpoint is
        ``GET /2.0/repositories/{workspace}/{slug}/src/{commit_or_branch}/{path}``
        which streams the file body unmodified. Cloud requires a commit
        or branch in the path segment; when ``at`` is omitted the method
        defaults to ``HEAD`` which Cloud accepts as an alias for the
        default branch's tip (Requirement 8.5).

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            file_path: Path to the file within the repository
            at: Optional commit hash or branch ref
            max_lines: Optional cap on the number of lines to read. ``None``
                (default) means read the whole file.

        Returns:
            File content as a single string with newline separators.
        """
        clean_path = file_path.lstrip("/")

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            ref = at or "HEAD"
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/src/{ref}/{clean_path}"
            )
            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            text = response.content.decode("utf-8", errors="replace")
            if max_lines is not None:
                return "\n".join(text.splitlines()[:max_lines])
            return text

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/browse/{clean_path}"
        )

        all_lines: list[str] = []
        start = 0
        page_size = 1000
        while True:
            params: dict[str, Any] = {"start": start, "limit": page_size}
            if at:
                params["at"] = at

            result = self.bitbucket.get(url, params=params)
            if not isinstance(result, dict):
                # Endpoint may return a string for very small/edge cases
                return str(result) if result else ""

            lines = result.get("lines", [])
            all_lines.extend(line.get("text", "") for line in lines)

            if max_lines is not None and len(all_lines) >= max_lines:
                all_lines = all_lines[:max_lines]
                break

            if result.get("isLastPage", True):
                break

            start = result.get("nextPageStart", start + page_size)

        return "\n".join(all_lines)

    def get_raw_file_content(
        self,
        project_key: str,
        repo_slug: str,
        file_path: str,
        at: str | None = None,
    ) -> str:
        """Fetch raw file content via Bitbucket's ``/raw`` / ``/src`` endpoint.

        On DC this uses ``/raw/{path}`` which streams the file body
        unmodified. On Cloud the equivalent endpoint is
        ``GET /2.0/repositories/{workspace}/{slug}/src/{commit_or_branch}/{path}``
        with ``format=raw`` (Requirement 8.6). Both return bytes that are
        decoded as UTF-8 with replacement for invalid sequences.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            file_path: Path to the file within the repository
            at: Optional commit hash or branch ref

        Returns:
            Decoded file content
        """
        clean_path = file_path.lstrip("/")

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            ref = at or "HEAD"
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/src/{ref}/{clean_path}"
            )
            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                params={"format": "raw"},
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            return response.content.decode("utf-8", errors="replace")

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/raw/{clean_path}"
        )
        params: dict[str, Any] = {}
        if at:
            params["at"] = at

        response = self.bitbucket._session.get(
            f"{self.config.url}{url}",
            params=params,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        # Decode defensively — caller knows whether the file is text.
        return response.content.decode("utf-8", errors="replace")

    def browse_directory(
        self,
        project_key: str,
        repo_slug: str,
        path: str = "",
        at: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """List the children of a directory in a repository.

        On DC this uses ``/files/{path}`` which returns a paginated list of
        names. Pass an empty ``path`` to list the repository root.

        On Cloud the equivalent endpoint is
        ``GET /2.0/repositories/{workspace}/{slug}/src/{commit_or_branch}/{path}``
        (Requirement 8.5). When the target path is a directory, Cloud
        returns the Cloud_Pagination_Shape envelope with each entry
        carrying ``path``, ``type`` (``"commit_directory"`` or
        ``"commit_file"``), and a ``commit`` sub-object. The Cloud branch
        walks pages until exhaustion and converts each entry into the
        ``{"path": ..., "type": "FILE|DIR"}`` dict the DC branch exposes
        so downstream tools see a consistent shape in either mode.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            path: Repository-relative directory path (empty = root)
            at: Optional commit hash or branch ref
            limit: Maximum number of entries per page

        Returns:
            List of dicts of the form ``{"path": "<name>", "type": "FILE|DIR"}``
        """
        clean_path = path.lstrip("/").rstrip("/")
        # Treat "." and ".." as "repository root" on both modes so the
        # tool is usable via agents that invoke it with the conventional
        # "current directory" idiom. Cloud's /src/ endpoint returns HTTP
        # 500 for a literal "." path segment, so normalising it here keeps
        # the behavior predictable. (No DC semantic change: DC already
        # stripped the leading slash above, so "." would have produced a
        # "/files/." URL which is harmless but also not useful.)
        if clean_path in (".", "./"):
            clean_path = ""

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            ref = at or "HEAD"
            # Cloud's src-listing endpoint requires a trailing slash on
            # the root-path URL: `/src/{ref}/` returns the root tree,
            # while `/src/{ref}` without the slash responds with HTTP
            # 404. Non-empty directory paths follow the same rule and
            # should NOT carry a trailing slash.
            #
            # The underlying ``atlassian-python-api`` client normalises
            # URLs by stripping trailing slashes, which breaks the
            # root-listing case — so for Cloud directory browsing we
            # bypass it and talk directly to the session. This preserves
            # the DC branch (which is unchanged below) and keeps the
            # public method signature + return shape identical.
            if clean_path:
                url_path = (
                    f"/2.0/repositories/{workspace}/{repo_slug}"
                    f"/src/{ref}/{clean_path}"
                )
            else:
                url_path = (
                    f"/2.0/repositories/{workspace}/{repo_slug}"
                    f"/src/{ref}/"
                )
            raw_entries = self._cloud_browse_directory(
                url_path, limit=limit
            )
            normalized: list[dict[str, Any]] = []
            for entry in raw_entries:
                if not isinstance(entry, dict):
                    continue
                entry_type = entry.get("type")
                # Cloud uses ``commit_directory`` / ``commit_file``; map
                # onto the DC-style uppercase ``FILE`` / ``DIR`` marker so
                # downstream consumers do not need to branch on mode.
                if entry_type == "commit_directory":
                    dc_type = "DIR"
                elif entry_type == "commit_file":
                    dc_type = "FILE"
                else:
                    dc_type = entry_type
                normalized.append(
                    {
                        "path": entry.get("path"),
                        "type": dc_type,
                        # Preserve Cloud-native fields so callers that
                        # already know the Cloud shape can still read them.
                        **{
                            k: v
                            for k, v in entry.items()
                            if k not in {"path", "type"}
                        },
                    }
                )
            return normalized

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/files{('/' + clean_path) if clean_path else ''}"
        )

        all_entries: list[dict[str, Any]] = []
        start = 0
        while True:
            params: dict[str, Any] = {"start": start, "limit": limit}
            if at:
                params["at"] = at

            result = self.bitbucket.get(url, params=params)
            if not isinstance(result, dict):
                break

            for raw in result.get("values", []):
                # The endpoint returns plain strings; normalise into dicts so
                # downstream tools always see a structured shape.
                if isinstance(raw, str):
                    all_entries.append({"path": raw})
                elif isinstance(raw, dict):
                    all_entries.append(raw)

            if result.get("isLastPage", True):
                break
            start = result.get("nextPageStart", start + limit)

        return all_entries

    def _cloud_browse_directory(
        self,
        url_path: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Fetch a Cloud directory listing, preserving trailing slashes.

        The underlying ``atlassian-python-api`` client normalises URLs
        by stripping trailing slashes. Bitbucket Cloud's
        ``/2.0/repositories/{workspace}/{slug}/src/{ref}/`` endpoint
        relies on that trailing slash to distinguish root-directory
        listings from commit-metadata lookups: without it, the API
        responds with HTTP 404. To keep the DC code path byte-for-byte
        identical, we bypass the library's ``get`` method here and issue
        the request through the underlying ``requests.Session`` directly.

        The helper walks the Cloud ``values`` / ``next`` pagination
        envelope and returns the concatenated list of raw entry dicts;
        the caller normalises ``commit_directory`` / ``commit_file``
        into the DC-shaped ``FILE`` / ``DIR`` markers.

        Args:
            url_path: Absolute path on ``api.bitbucket.org`` starting
                with ``/2.0/...``. Trailing slashes are preserved as-is.
            limit: Upper bound on the cumulative number of entries
                returned. A value ``<= 0`` disables the cap (all pages
                are consumed).

        Returns:
            List of raw Cloud ``src``-entry dicts.
        """
        base = self.config.url.rstrip("/")
        url = f"{base}{url_path}"
        # Cloud's max pagelen is 100; cap it to avoid HTTP 500 from
        # Atlassian when callers pass large limits (e.g. 1000).
        cloud_pagelen = min(limit, 100) if limit and limit > 0 else 100

        params: dict[str, Any] = {}
        if limit and limit > 0:
            params["pagelen"] = cloud_pagelen

        all_entries: list[dict[str, Any]] = []
        next_url: str | None = url
        first = True
        session = self.bitbucket._session
        while next_url:
            if first:
                response = session.get(next_url, params=params, timeout=self.config.timeout)
                first = False
            else:
                # Cloud's ``next`` is a fully-qualified URL that already
                # carries ``pagelen`` / ``page``.
                response = session.get(next_url, timeout=self.config.timeout)

            response.raise_for_status()
            try:
                body = response.json()
            except ValueError:
                break
            if not isinstance(body, dict):
                break
            values = body.get("values") or []
            for entry in values:
                if isinstance(entry, dict):
                    all_entries.append(entry)
                    if limit and limit > 0 and len(all_entries) >= limit:
                        return all_entries[:limit]
            next_url = body.get("next")

        return all_entries

    def get_default_branch(
        self,
        project_key: str,
        repo_slug: str,
    ) -> dict[str, Any]:
        """Get the default branch of a repository.

        On DC this hits the dedicated ``/default-branch`` resource. Cloud
        exposes the default branch as ``mainbranch`` on the repository
        object itself, so this method fetches the repo and projects its
        ``mainbranch`` payload into the DC-shaped branch envelope
        (``id``/``displayId``/``type``). The result is normalized through
        :func:`normalize_branch` via the repository shape.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug

        Returns:
            Branch object for the default branch
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}"
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response for repo {workspace}/{repo_slug}: {raw}"
                )
            main = raw.get("mainbranch")
            if not isinstance(main, dict):
                raise ValueError(
                    f"Cloud repo {workspace}/{repo_slug} did not expose a "
                    f"mainbranch field: {raw}"
                )
            name = main.get("name")
            return {
                "id": f"refs/heads/{name}" if name else None,
                "displayId": name,
                "type": main.get("type", "BRANCH"),
            }

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/default-branch"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response for default branch of {project_key}/{repo_slug}: {result}"
            )
        return result

    # ------------------------------------------------------------------
    # File write (commit) operations (DC 7.2+)
    # ------------------------------------------------------------------

    def put_file_content(
        self,
        project_key: str,
        repo_slug: str,
        file_path: str,
        content: str,
        message: str,
        branch: str,
        source_commit_id: str | None = None,
        source_branch: str | None = None,
    ) -> dict[str, Any]:
        """Create or update a file with a single commit.

        Uses Bitbucket DC's multipart ``PUT /browse/{path}`` endpoint. The
        same call both creates new files and updates existing ones; when
        updating, pass ``source_commit_id`` for optimistic concurrency.

        On Cloud the equivalent is ``POST /2.0/repositories/{ws}/{slug}/src``
        with multipart form data where the file path is itself a form
        field name carrying the content (that is, ``files={file_path: content}``
        with ``branch`` and ``message`` as sibling fields). Cloud has no
        optimistic-concurrency equivalent, so ``source_commit_id`` is
        ignored when ``is_cloud`` is ``True``. ``source_branch`` is
        honored when supplied — Cloud's API will branch from it when the
        target branch does not exist.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            file_path: Repository-relative path of the file
            content: New file content (text)
            message: Commit message
            branch: Branch name to commit to (plain name, no ``refs/heads/``)
            source_commit_id: Optional current commit ID of the file when
                updating (conflict detection, DC only). Ignored on Cloud.
            source_branch: Optional source branch to create ``branch`` from
                when ``branch`` does not yet exist

        Returns:
            The resulting commit object.
        """
        clean_path = file_path.lstrip("/")

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/src"
            # Cloud's POST /src treats the file path itself as the form
            # field name — this is what distinguishes a write from a
            # query. `message` and `branch` are sibling form fields.
            files = {
                clean_path: (None, content),
                "message": (None, message),
                "branch": (None, branch),
            }
            if source_branch:
                files["parents"] = (None, source_branch)
            response = self.bitbucket._session.post(
                f"{self.config.url}{url}",
                files=files,
                verify=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            # Cloud returns 201 Created with an empty body; synthesize a
            # DC-shaped receipt so callers see a consistent return type.
            return {
                "path": file_path,
                "branch": branch,
                "message": message,
                "status": "created",
            }

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/browse/{clean_path}"
        )

        # Bitbucket DC requires multipart/form-data for this endpoint.
        # We construct the multipart payload manually via requests' files API
        # so we don't need atlassian-python-api's limited wrapper.
        files = {
            "content": (None, content),
            "message": (None, message),
            "branch": (None, branch),
        }
        if source_commit_id:
            files["sourceCommitId"] = (None, source_commit_id)
        if source_branch:
            files["sourceBranch"] = (None, source_branch)

        response = self.bitbucket._session.put(
            f"{self.config.url}{url}",
            files=files,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response writing {file_path}: {result}")
        return result

    def delete_file(
        self,
        project_key: str,
        repo_slug: str,
        file_path: str,
        message: str,
        branch: str,
        source_commit_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete a file from a branch with a single commit.

        DC exposes file removal via ``DELETE /browse/{path}``.

        Cloud has no DELETE verb on its ``src`` endpoint; file removal is
        expressed by a ``POST /2.0/repositories/{ws}/{slug}/src`` with the
        path listed in the ``files`` form field (comma-separated). We
        issue that POST and synthesize a DC-shaped receipt so downstream
        callers see a consistent return type.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            file_path: Repository-relative path to remove
            message: Commit message
            branch: Branch name
            source_commit_id: Optional current file commit ID for
                optimistic concurrency (DC only). Ignored on Cloud.

        Returns:
            The resulting commit object, or a synthesized
            ``{"deleted": True, ...}`` receipt when the API returns an
            empty body.
        """
        clean_path = file_path.lstrip("/")

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/src"
            files = {
                "files": (None, clean_path),
                "message": (None, message),
                "branch": (None, branch),
            }
            response = self.bitbucket._session.post(
                f"{self.config.url}{url}",
                files=files,
                verify=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            return {
                "deleted": True,
                "path": file_path,
                "branch": branch,
                "message": message,
            }

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/browse/{clean_path}"
        )
        data: dict[str, Any] = {"message": message, "branch": branch}
        if source_commit_id:
            data["sourceCommitId"] = source_commit_id

        response = self.bitbucket._session.delete(
            f"{self.config.url}{url}",
            data=data,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        if response.content:
            try:
                result = response.json()
                if isinstance(result, dict):
                    return result
            except ValueError:
                pass
        return {"deleted": True, "path": file_path, "branch": branch}
