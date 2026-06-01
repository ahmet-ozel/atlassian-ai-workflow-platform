"""Pull request operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/pull-requests/...``
(plus the associated ``/dashboard/pull-requests`` dashboard endpoint).
Cloud paths target ``/2.0/repositories/{workspace}/{repo_slug}/pullrequests/...``
(Requirements 9.1 - 9.8, 9.11, 9.12). The agent-facing method signatures,
parameter names, and return types do not change between modes; Cloud payloads
are passed through :func:`normalize_pull_request` (which additionally invokes
:func:`normalize_user` on author / reviewers / participants) so downstream
server-tool code keeps consuming the DC shape, including the synthetic
``fromRef`` / ``toRef`` wrappers Cloud does not natively expose and the
epoch-millis ``createdDate`` / ``updatedDate`` DC callers expect.

State values (Requirement 9.6): the agent-visible input parameter name
(``state``) and the DC vocabulary (``OPEN``, ``MERGED``, ``DECLINED``) are
preserved on both sides. Cloud returns an additional ``SUPERSEDED`` value
that does not exist on DC; the normalizer passes it through verbatim so
callers can branch on it when needed.

DC-only capabilities that have no Cloud equivalent —
``list_pull_request_participants``, ``add_pr_comment_reaction``,
``remove_pr_comment_reaction`` — live in sibling modules
(``pr_participants.py``, ``reactions.py``) and are guarded at the
server-tool layer in task 17.1; this module never receives Cloud
branches for those capabilities.
"""

import logging
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_pull_request

logger = logging.getLogger("mcp-atlassian.bitbucket.pull_requests")


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


def _strip_heads_prefix(ref: str) -> str:
    """Normalize a DC-style ``refs/heads/<name>`` ref down to the plain name.

    The agent-facing Cloud PR body uses plain branch names under
    ``source.branch.name`` and ``destination.branch.name``; DC accepts
    both plain names and fully-qualified refs. Callers of
    :meth:`create_pull_request` supply either form, so the Cloud branch
    strips the ``refs/heads/`` prefix when present (Requirement 9.3).
    """
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return ref


def _cloud_reviewer_entry(username: str) -> dict[str, Any]:
    """Build a Cloud reviewer entry from a DC-shaped username value.

    Cloud expects reviewers in the shape ``{"uuid": "{...}"}`` or
    ``{"account_id": "..."}``. When the agent passes a value that looks
    like a Cloud UUID (``{...}``) we use ``uuid``; otherwise we treat it
    as an ``account_id`` and forward it unchanged (Requirement 9.3).
    """
    if username.startswith("{") and username.endswith("}"):
        return {"uuid": username}
    return {"account_id": username}


class PullRequestsMixin(BitbucketClient):
    """Mixin providing pull request operations for Bitbucket DC and Cloud."""

    def get_pull_requests(
        self,
        project_key: str,
        repo_slug: str,
        state: str = "OPEN",
        limit: int = 25,
        order: str | None = None,
        at: str | None = None,
    ) -> list[dict[str, Any]]:
        """List pull requests for a repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            state: PR state filter (OPEN, MERGED, DECLINED, ALL)
            limit: Maximum number of results per page
            order: Sort order (NEWEST, OLDEST)
            at: Branch ref to filter PRs targeting this branch

        Returns:
            List of pull request objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/pullrequests"
            cloud_params: dict[str, Any] = {}
            # State translation is a no-op — Cloud accepts the same DC
            # vocabulary (OPEN / MERGED / DECLINED) plus Cloud-extra
            # SUPERSEDED (Requirement 9.6). Treat ALL as "omit filter".
            if state and state.upper() != "ALL":
                cloud_params["state"] = state
            if order:
                cloud_params["sort"] = {
                    "NEWEST": "-updated_on",
                    "OLDEST": "updated_on",
                }.get(order, order)
            if at:
                # Cloud PR search uses BBQL; scope to the target branch.
                plain = _strip_heads_prefix(at)
                cloud_params["q"] = f'destination.branch.name="{plain}"'
            return self._get_paged_results(
                url,
                params=cloud_params,
                limit=limit,
                normalizer=normalize_pull_request,
            )

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests"
        params: dict[str, Any] = {"state": state}
        if order:
            params["order"] = order
        if at:
            params["at"] = at

        return self._get_paged_results(url, params=params, limit=limit)

    def get_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Get a single pull request by ID.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            Pull request object (normalized to the DC shape on Cloud).

        Raises:
            ValueError: If the PR is not found or API call fails
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response for PR #{pr_id}: {raw}")
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response for PR #{pr_id}: {result}")
        return result

    def create_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        title: str,
        from_branch: str,
        to_branch: str,
        description: str | None = None,
        reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            title: PR title
            from_branch: Source branch ref (e.g., 'refs/heads/feature-branch'
                or plain 'feature-branch')
            to_branch: Target branch ref (e.g., 'refs/heads/main' or 'main')
            description: Optional PR description
            reviewers: Optional list of reviewer usernames (DC slugs) or
                Cloud account_ids / UUIDs

        Returns:
            Created pull request object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/pullrequests"
            cloud_data: dict[str, Any] = {
                "title": title,
                "source": {"branch": {"name": _strip_heads_prefix(from_branch)}},
                "destination": {"branch": {"name": _strip_heads_prefix(to_branch)}},
            }
            if description:
                cloud_data["description"] = description
            if reviewers:
                cloud_data["reviewers"] = [
                    _cloud_reviewer_entry(r) for r in reviewers
                ]
            raw = self.bitbucket.post(url, data=cloud_data)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response creating PR: {raw}")
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests"

        # Ensure branch refs have proper prefix
        if not from_branch.startswith("refs/heads/"):
            from_branch = f"refs/heads/{from_branch}"
        if not to_branch.startswith("refs/heads/"):
            to_branch = f"refs/heads/{to_branch}"

        data: dict[str, Any] = {
            "title": title,
            "fromRef": {
                "id": from_branch,
                "repository": {
                    "slug": repo_slug,
                    "project": {"key": project_key},
                },
            },
            "toRef": {
                "id": to_branch,
                "repository": {
                    "slug": repo_slug,
                    "project": {"key": project_key},
                },
            },
        }

        if description:
            data["description"] = description

        if reviewers:
            data["reviewers"] = [{"user": {"name": r}} for r in reviewers]

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response creating PR: {result}")
        return result

    def merge_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        version: int | None = None,
        message: str | None = None,
        delete_source_branch: bool = False,
    ) -> dict[str, Any]:
        """Merge a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            version: The current version of the PR (DC optimistic locking;
                ignored on Cloud which does not use version tokens)
            message: Optional merge commit message
            delete_source_branch: Whether to delete the source branch after merge

        Returns:
            Merged pull request object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/merge"
            )
            cloud_body: dict[str, Any] = {
                "merge_strategy": "merge_commit",
                "close_source_branch": bool(delete_source_branch),
            }
            if message:
                cloud_body["message"] = message
            raw = self.bitbucket.post(url, data=cloud_body)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response merging PR #{pr_id}: {raw}")
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/merge"
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version

        data: dict[str, Any] = {}
        if message:
            data["message"] = message
        if delete_source_branch:
            data["deleteSourceRef"] = True

        result = self.bitbucket.post(url, params=params, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response merging PR #{pr_id}: {result}")
        return result

    def approve_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Approve a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            Participant object with approval status
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/approve"
            )
            raw = self.bitbucket.post(url)
            if not isinstance(raw, dict):
                return {"status": "APPROVED"}
            return raw

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/approve"
        result = self.bitbucket.post(url)
        if not isinstance(result, dict):
            # Approval may return empty response on success
            return {"status": "APPROVED"}
        return result

    def unapprove_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Remove approval from a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            Participant object
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/approve"
            )
            raw = self.bitbucket.delete(url)
            if not isinstance(raw, dict):
                return {"status": "UNAPPROVED"}
            return raw

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/approve"
        result = self.bitbucket.delete(url)
        if not isinstance(result, dict):
            return {"status": "UNAPPROVED"}
        return result

    def decline_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Decline a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            version: The current version of the PR (DC optimistic locking;
                ignored on Cloud)

        Returns:
            Declined pull request object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/decline"
            )
            raw = self.bitbucket.post(url)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response declining PR #{pr_id}: {raw}")
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/decline"
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version

        result = self.bitbucket.post(url, params=params)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response declining PR #{pr_id}: {result}")
        return result

    def get_pull_request_activities(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Get activities (comments, approvals, etc.) for a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            limit: Maximum number of results per page

        Returns:
            List of activity objects
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            # Cloud exposes the activities stream at ``/activity`` (singular)
            # rather than DC's ``/activities`` (Requirement 9.7).
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/activity"
            )
            return self._get_paged_results(url, limit=limit)

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/activities"
        return self._get_paged_results(url, limit=limit)

    def add_pull_request_comment(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        text: str,
        parent_id: int | None = None,
        anchor: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a comment to a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            text: Comment text
            parent_id: Optional parent comment ID for replies
            anchor: Optional anchor for inline comments. DC shape is
                ``{"path": ..., "line": ..., "lineType": ADDED|REMOVED|CONTEXT,
                "fileType": FROM|TO}``. On Cloud this is translated onto
                Cloud's ``inline`` object (``{"path": ..., "to"|"from": <line>}``).

        Returns:
            Created comment object
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/comments"
            )
            cloud_body: dict[str, Any] = {"content": {"raw": text}}
            if parent_id is not None:
                cloud_body["parent"] = {"id": parent_id}
            if anchor:
                cloud_inline = self._dc_anchor_to_cloud_inline(anchor)
                if cloud_inline is not None:
                    cloud_body["inline"] = cloud_inline
            raw = self.bitbucket.post(url, data=cloud_body)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response adding comment to PR #{pr_id}: {raw}"
                )
            return self._normalize_cloud_pr_comment(raw)

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/comments"
        data: dict[str, Any] = {"text": text}

        if parent_id is not None:
            data["parent"] = {"id": parent_id}

        if anchor:
            data["anchor"] = anchor

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response adding comment to PR #{pr_id}: {result}")
        return result

    @staticmethod
    def _dc_anchor_to_cloud_inline(
        anchor: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Translate a DC PR comment ``anchor`` onto a Cloud ``inline`` object.

        Cloud's inline shape is ``{"path": "<file>", "to": <int>}`` for
        destination-side lines and ``{"path": "<file>", "from": <int>}``
        for source-side lines. DC's ``fileType`` (``FROM`` / ``TO``) is
        the authoritative axis; ``lineType`` (``ADDED`` / ``REMOVED`` /
        ``CONTEXT``) is used as fallback when ``fileType`` is absent.
        """
        path = anchor.get("path")
        line = anchor.get("line")
        if path is None and line is None:
            return None
        inline: dict[str, Any] = {}
        if path is not None:
            inline["path"] = path
        if line is not None:
            file_type = anchor.get("fileType")
            line_type = anchor.get("lineType")
            if file_type == "FROM":
                axis = "from"
            elif file_type == "TO":
                axis = "to"
            elif line_type == "REMOVED":
                axis = "from"
            else:
                axis = "to"
            inline[axis] = line
        return inline

    @staticmethod
    def _normalize_cloud_pr_comment(c: dict[str, Any]) -> dict[str, Any]:
        """Normalize a Cloud PR comment payload to a DC-ish shape.

        Cloud comment shape::

            {"id": 42, "content": {"raw": "<md>", ...},
             "user": {...}, "inline": {"path": ..., "to": 7},
             "created_on": ..., "updated_on": ...}

        DC PR comments expose ``text`` (the body) and ``author`` (the
        user). We preserve every Cloud key (passthrough) and additionally
        synthesize ``text`` / ``author`` so downstream server-tool
        assembly keeps reading a familiar shape. The author is passed
        through :func:`normalize_user` so it exposes both Cloud
        (``account_id``, ``display_name``) and DC (``name``, ``slug``,
        ``displayName``) identifier keys.
        """
        # Local import avoids circular dependency at module load; the
        # normalizer module imports nothing from this one so this is only
        # a defensive re-import at call time.
        from .response_normalizer import normalize_user

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

    def get_pull_request_diff(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        context_lines: int = 3,
    ) -> str:
        """Get the diff for a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            context_lines: Number of context lines around changes

        Returns:
            Diff content as string
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/diff"
            )
            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                params={"context": context_lines},
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            return response.text

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}.diff"
        params = {"contextLines": context_lines}

        # Use raw get to get text content
        response = self.bitbucket._session.get(
            f"{self.config.url}{url}",
            params=params,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        return response.text

    def get_pull_request_changes(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get the list of changed files in a pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            limit: Maximum number of results per page

        Returns:
            List of change objects with file paths and change types
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            # Cloud exposes per-file change summaries at
            # ``/pullrequests/{id}/diffstat`` (paginated). We forward the
            # payload unchanged; the server-tool layer already reads the
            # entries defensively (``path.toString`` / ``path.name``).
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/diffstat"
            )
            return self._get_paged_results(url, limit=limit)

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}/changes"
        return self._get_paged_results(url, limit=limit)

    def update_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        version: int,
        title: str | None = None,
        description: str | None = None,
        reviewers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update a pull request's title, description, or reviewers.

        Bitbucket DC requires the current ``version`` for optimistic locking;
        callers should fetch the PR first to obtain it. Cloud does not use
        version tokens on PR update; the argument is accepted for signature
        parity and ignored on Cloud.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            version: Current PR version (DC only; ignored on Cloud)
            title: New title (omit to keep)
            description: New description (omit to keep)
            reviewers: New reviewer username list (replaces the existing set
                when provided; omit to keep the current reviewers)

        Returns:
            Updated pull request object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            cloud_body: dict[str, Any] = {}
            if title is not None:
                cloud_body["title"] = title
            if description is not None:
                cloud_body["description"] = description
            if reviewers is not None:
                cloud_body["reviewers"] = [
                    _cloud_reviewer_entry(r) for r in reviewers
                ]
            raw = self.bitbucket.put(url, data=cloud_body)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response updating PR #{pr_id}: {raw}")
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}"
        )
        data: dict[str, Any] = {"version": version}
        if title is not None:
            data["title"] = title
        if description is not None:
            data["description"] = description
        if reviewers is not None:
            data["reviewers"] = [{"user": {"name": r}} for r in reviewers]

        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response updating PR #{pr_id}: {result}")
        return result

    def add_pr_reviewer(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        username: str,
    ) -> dict[str, Any]:
        """Add a reviewer to a pull request.

        DC exposes a dedicated ``/participants`` endpoint. Cloud has no
        direct "add single reviewer" endpoint — the canonical approach is
        ``PUT /pullrequests/{id}`` with the updated ``reviewers`` list
        (Requirement 9.12 calls for the same tool name in both modes).
        This method fetches the current PR on Cloud, appends the new
        reviewer entry, and writes the result back via
        :meth:`update_pull_request`.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            username: The reviewer's username (DC slug) or Cloud
                ``account_id`` / UUID

        Returns:
            Created participant object (DC) or updated PR (Cloud, shape-
            normalized).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            # Fetch the PR to read the current reviewer list (raw Cloud
            # shape — reviewers are ``[{"uuid": ..., "account_id": ...}]``).
            pr_url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            raw_pr = self.bitbucket.get(pr_url)
            if not isinstance(raw_pr, dict):
                raise ValueError(
                    f"Unexpected response fetching PR #{pr_id} for reviewer update: {raw_pr}"
                )
            current = raw_pr.get("reviewers") or []
            # Build the refreshed reviewer list by preserving every
            # existing entry (with whatever identifier Cloud already
            # returned) and appending a new entry for ``username``.
            new_entry = _cloud_reviewer_entry(username)
            updated_reviewers: list[dict[str, Any]] = []
            seen = False
            for r in current:
                if not isinstance(r, dict):
                    continue
                entry = {
                    k: v
                    for k, v in r.items()
                    if k in {"uuid", "account_id", "username"}
                }
                if entry and (
                    entry.get("uuid") == new_entry.get("uuid")
                    or entry.get("account_id") == new_entry.get("account_id")
                ):
                    seen = True
                if entry:
                    updated_reviewers.append(entry)
            if not seen:
                updated_reviewers.append(new_entry)

            cloud_body = {"reviewers": updated_reviewers}
            raw = self.bitbucket.put(pr_url, data=cloud_body)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response adding reviewer to PR #{pr_id}: {raw}"
                )
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/participants"
        )
        data = {"user": {"name": username}, "role": "REVIEWER"}
        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response adding reviewer to PR #{pr_id}: {result}"
            )
        return result

    def remove_pr_reviewer(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        username: str,
    ) -> bool:
        """Remove a reviewer/participant from a pull request.

        DC uses the dedicated ``DELETE /participants/{username}`` endpoint.
        Cloud has no direct removal endpoint; we ``PUT /pullrequests/{id}``
        with the reviewer filtered out of the current list.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            username: The reviewer's username (slug) or Cloud
                ``account_id`` / UUID to remove

        Returns:
            True on successful deletion
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            pr_url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            raw_pr = self.bitbucket.get(pr_url)
            if not isinstance(raw_pr, dict):
                raise ValueError(
                    f"Unexpected response fetching PR #{pr_id} for reviewer removal: {raw_pr}"
                )
            target = _cloud_reviewer_entry(username)
            target_uuid = target.get("uuid")
            target_account_id = target.get("account_id")
            current = raw_pr.get("reviewers") or []
            updated_reviewers: list[dict[str, Any]] = []
            for r in current:
                if not isinstance(r, dict):
                    continue
                entry = {
                    k: v
                    for k, v in r.items()
                    if k in {"uuid", "account_id", "username"}
                }
                if not entry:
                    continue
                if target_uuid is not None and entry.get("uuid") == target_uuid:
                    continue
                if (
                    target_account_id is not None
                    and entry.get("account_id") == target_account_id
                ):
                    continue
                # Cloud's ``nickname``/``username`` match is best-effort.
                if entry.get("username") == username:
                    continue
                updated_reviewers.append(entry)
            self.bitbucket.put(pr_url, data={"reviewers": updated_reviewers})
            return True

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/participants/{username}"
        )
        self.bitbucket.delete(url)
        return True

    def set_pr_participant_status(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        username: str,
        status: str,
        approved: bool = False,
    ) -> dict[str, Any]:
        """Update a participant's review status on a PR.

        Bitbucket DC has three reviewer states:

        * ``UNAPPROVED`` (default — no opinion yet)
        * ``APPROVED``   (approval given)
        * ``NEEDS_WORK`` (Bitbucket DC's equivalent of GitHub's
          "Request changes" — blocks merge by default)

        This is the underlying primitive that ``request_changes`` and
        ``unrequest_changes`` are built on. On Cloud, the method routes
        the supported transitions onto the dedicated ``/approve`` and
        ``/request-changes`` endpoints: ``APPROVED`` → ``POST /approve``,
        ``NEEDS_WORK`` → ``POST /request-changes``, ``UNAPPROVED`` →
        ``DELETE /request-changes`` followed by ``DELETE /approve``.
        Note that Cloud does not let one user set another user's
        participation status; the authenticated user is always the
        subject on Cloud, so the ``username`` argument is validated but
        otherwise unused.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            username: The reviewer username (slug); on Cloud the call
                acts on the authenticated user.
            status: ``APPROVED``, ``UNAPPROVED``, or ``NEEDS_WORK``
            approved: ``True`` only when ``status == "APPROVED"``

        Returns:
            Updated participant object
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            base = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            upper = (status or "").upper()
            if upper == "APPROVED":
                raw = self.bitbucket.post(f"{base}/approve")
                if isinstance(raw, dict):
                    return raw
                return {"status": "APPROVED", "approved": True}
            if upper == "NEEDS_WORK":
                raw = self.bitbucket.post(f"{base}/request-changes")
                if isinstance(raw, dict):
                    return raw
                return {"status": "NEEDS_WORK", "approved": False}
            if upper == "UNAPPROVED":
                # Clear both "request changes" and "approved" on Cloud;
                # a reviewer can be in at most one of those states.
                # Ignore 404/409 because the user may only be in one.
                try:
                    self.bitbucket.delete(f"{base}/request-changes")
                except Exception as e:  # noqa: BLE001 — idempotent clear
                    logger.debug(
                        "set_pr_participant_status (Cloud): clearing "
                        "request-changes on PR #%s raised %s",
                        pr_id,
                        e,
                    )
                try:
                    self.bitbucket.delete(f"{base}/approve")
                except Exception as e:  # noqa: BLE001 — idempotent clear
                    logger.debug(
                        "set_pr_participant_status (Cloud): clearing "
                        "approve on PR #%s raised %s",
                        pr_id,
                        e,
                    )
                return {"status": "UNAPPROVED", "approved": False}
            raise ValueError(
                f"set_pr_participant_status on Cloud does not support status={status!r}"
            )

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/participants/{username}"
        )
        data = {
            "user": {"name": username},
            "role": "REVIEWER",
            "approved": approved,
            "status": status,
        }
        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response setting participant status on PR #{pr_id}: {result}"
            )
        return result

    def request_changes_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        username: str,
    ) -> dict[str, Any]:
        """Mark a PR as needing changes for a given reviewer (NEEDS_WORK).

        This is Bitbucket DC's equivalent of GitHub's "Request changes":
        the PR is blocked from merging until the reviewer's status is
        cleared (e.g. via ``unrequest_changes_pull_request`` or
        ``approve_pull_request``). On Cloud the method targets the
        dedicated ``/pullrequests/{id}/request-changes`` endpoint
        (Requirement 9.11); the authenticated user is always the
        subject on Cloud regardless of the ``username`` argument.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/request-changes"
            )
            raw = self.bitbucket.post(url)
            if isinstance(raw, dict):
                return raw
            return {"status": "NEEDS_WORK", "approved": False}

        return self.set_pr_participant_status(
            project_key, repo_slug, pr_id, username, status="NEEDS_WORK", approved=False
        )

    def unrequest_changes_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        username: str,
    ) -> dict[str, Any]:
        """Clear a previously-set NEEDS_WORK status, returning to UNAPPROVED.

        On Cloud the method targets the dedicated
        ``/pullrequests/{id}/request-changes`` endpoint with ``DELETE``
        (Requirement 9.11).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/request-changes"
            )
            raw = self.bitbucket.delete(url)
            if isinstance(raw, dict):
                return raw
            return {"status": "UNAPPROVED", "approved": False}

        return self.set_pr_participant_status(
            project_key, repo_slug, pr_id, username, status="UNAPPROVED", approved=False
        )

    def reopen_pull_request(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        version: int | None = None,
    ) -> dict[str, Any]:
        """Reopen a previously declined pull request.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            version: Current PR version (DC optimistic locking; ignored
                on Cloud)

        Returns:
            Reopened pull request object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/reopen"
            )
            raw = self.bitbucket.post(url)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response reopening PR #{pr_id}: {raw}"
                )
            normalized = normalize_pull_request(raw)
            assert normalized is not None
            return normalized

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/reopen"
        )
        params: dict[str, Any] = {}
        if version is not None:
            params["version"] = version

        result = self.bitbucket.post(url, params=params)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response reopening PR #{pr_id}: {result}"
            )
        return result

    def get_pr_merge_status(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Check whether a PR is mergeable (and surface conflicts/vetoes).

        Returns the Bitbucket DC ``MergeStatus`` object with fields like
        ``canMerge``, ``conflicted`` and ``vetoes``. Cloud has no
        dedicated merge-status endpoint, so this method fetches the PR
        and synthesizes a DC-shaped status dict from its ``state`` /
        ``close_source_branch`` fields: ``canMerge`` is ``True`` iff
        the PR is ``OPEN``; ``conflicted`` defaults to ``False``
        (Cloud returns 409 at merge time when a conflict exists); and
        ``vetoes`` is an empty list.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            Merge status object
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}"
            )
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected merge-status response for PR #{pr_id}: {raw}"
                )
            state = (raw.get("state") or "").upper()
            return {
                "canMerge": state == "OPEN",
                "conflicted": False,
                "vetoes": [],
                "state": state,
            }

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/merge"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected merge-status response for PR #{pr_id}: {result}"
            )
        return result

    def get_pr_file_diff(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        path: str,
        context_lines: int = 3,
    ) -> str:
        """Return the unified diff for a single file in a PR.

        Useful for surgically reviewing one file in a large PR without
        pulling the whole diff into context. On Cloud the endpoint is
        ``GET /pullrequests/{id}/diff`` with the ``path`` query param
        scoping the response to a single file.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            path: Repository-relative path of the file (no leading slash)
            context_lines: Diff context lines around hunks

        Returns:
            Unified diff text for the requested path
        """
        clean_path = path.lstrip("/")

        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/diff"
            )
            response = self.bitbucket._session.get(
                f"{self.config.url}{url}",
                params={"context": context_lines, "path": clean_path},
                verify=self.config.ssl_verify,
            )
            response.raise_for_status()
            return response.text

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/diff/{clean_path}"
        )
        params = {"contextLines": context_lines}
        response = self.bitbucket._session.get(
            f"{self.config.url}{url}",
            params=params,
            verify=self.config.ssl_verify,
        )
        response.raise_for_status()
        return response.text

    def list_my_pull_requests(
        self,
        role: str = "REVIEWER",
        state: str = "OPEN",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List PRs visible on the authenticated user's dashboard.

        On DC this uses the ``/dashboard/pull-requests`` endpoint which
        scopes results to the authenticated user — perfect for "what
        should I review?" and "what have I authored?" queries.

        On Cloud the equivalent endpoint is
        ``GET /2.0/pullrequests/{selected_user}`` which requires the
        authenticated user's UUID. We resolve that via ``GET /2.0/user``
        first (one extra round-trip per invocation — the response is not
        cached because the authenticated principal may change between
        requests in multi-user mode).

        Args:
            role: ``REVIEWER`` (default) or ``AUTHOR``
            state: PR state filter (OPEN, MERGED, DECLINED, ALL)
            limit: Page size

        Returns:
            List of PR objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            me = self.bitbucket.get("/2.0/user")
            if not isinstance(me, dict):
                raise ValueError(
                    "list_my_pull_requests on Cloud failed to resolve "
                    "authenticated user from GET /2.0/user."
                )
            # Prefer ``uuid`` (always unique) and fall back to
            # ``account_id`` (OAuth / app-password) for the selector.
            selector = me.get("uuid") or me.get("account_id")
            if not selector:
                raise ValueError(
                    "list_my_pull_requests on Cloud: GET /2.0/user did not "
                    "return a uuid or account_id."
                )
            url = f"/2.0/pullrequests/{selector}"
            cloud_params: dict[str, Any] = {}
            if state and state.upper() != "ALL":
                cloud_params["state"] = state
            # Cloud's dashboard endpoint does not take a ``role`` filter;
            # it returns PRs where the user is a participant (either
            # author or reviewer). DC callers pass ``role=AUTHOR`` to
            # narrow the list, so we post-filter Cloud results by
            # comparing the PR's author selector when ``role == AUTHOR``.
            raw = self._get_paged_results(
                url,
                params=cloud_params,
                limit=limit,
                normalizer=normalize_pull_request,
            )
            if (role or "").upper() == "AUTHOR":
                raw = [
                    pr
                    for pr in raw
                    if isinstance(pr, dict)
                    and isinstance(pr.get("author"), dict)
                    and (
                        pr["author"].get("uuid") == selector
                        or pr["author"].get("account_id") == selector
                    )
                ]
            return raw

        url = "/rest/api/latest/dashboard/pull-requests"
        params: dict[str, Any] = {"role": role, "state": state}
        return self._get_paged_results(url, params=params, limit=limit)

    def update_pr_comment(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        comment_id: int,
        version: int,
        text: str,
    ) -> dict[str, Any]:
        """Update an existing PR comment.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            comment_id: The comment ID
            version: Current comment version (DC optimistic locking;
                ignored on Cloud)
            text: New comment text

        Returns:
            Updated comment object
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/comments/{comment_id}"
            )
            raw = self.bitbucket.put(url, data={"content": {"raw": text}})
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Unexpected response updating PR comment {comment_id}: {raw}"
                )
            return self._normalize_cloud_pr_comment(raw)

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/comments/{comment_id}"
        )
        data = {"version": version, "text": text}
        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating PR comment {comment_id}: {result}"
            )
        return result

    def delete_pr_comment(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        comment_id: int,
        version: int,
    ) -> bool:
        """Delete a PR comment.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID
            comment_id: The comment ID
            version: Current comment version (DC optimistic locking;
                ignored on Cloud which does not use version tokens)

        Returns:
            True on successful deletion
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/comments/{comment_id}"
            )
            self.bitbucket.delete(url)
            return True

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/comments/{comment_id}"
        )
        self.bitbucket.delete(url, params={"version": version})
        return True
