"""Repository and project admin operations for Bitbucket Data Center.

Wraps the Bitbucket DC ``/rest/api/latest/projects`` and
``/rest/api/latest/projects/{project_key}/repos`` endpoints to create,
update and fork repositories, and to create and update projects.

Destructive endpoints are intentionally NOT exposed: repository and project
deletion are too broad to expose through an agent-facing tool (Requirements
4.4 and 5.3 of the DC tool-parity spec). Callers that need to remove a
repository or project should do so through the Bitbucket UI or an explicit
admin CLI, not through this mixin.

Project-scope filtering (``BITBUCKET_PROJECTS_FILTER``) and read-only-mode
gating are enforced in the server tool layer on top of these primitives —
the mixin itself performs no access-control checks so it remains safe to
reuse from internal automation.
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.admin")


class AdminMixin(BitbucketClient):
    """Mixin providing repository and project admin operations for Bitbucket DC.

    The mixin exposes create / update / fork operations only; repository
    deletion and project deletion are intentionally omitted per the DC
    tool-parity spec (Requirements 4.4, 5.3).
    """

    # ------------------------------------------------------------------
    # Repository admin (Requirement 4)
    # ------------------------------------------------------------------

    def create_repository(
        self,
        project_key: str,
        *,
        name: str,
        scm: str = "git",
        forkable: bool = True,
        public: bool = False,
    ) -> dict[str, Any]:
        """Create a repository under an existing project.

        Wraps ``POST /rest/api/latest/projects/{project_key}/repos``.

        Args:
            project_key: The project key that will own the new repository
            name: Display name for the repository. Bitbucket derives the
                repository slug from this name on creation.
            scm: SCM to use. Bitbucket DC only supports ``"git"`` today,
                which is the default.
            forkable: Whether other users may fork the repository.
            public: Whether the repository is publicly readable.

        Returns:
            The created repository object as returned by Bitbucket.
        """
        url = f"/rest/api/latest/projects/{project_key}/repos"
        data: dict[str, Any] = {
            "name": name,
            "scmId": scm,
            "forkable": forkable,
            "public": public,
        }

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response creating repository {project_key}/{name}: "
                f"{result}"
            )
        return result

    def update_repository(
        self,
        project_key: str,
        repo_slug: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update mutable fields on an existing repository.

        Wraps ``PUT /rest/api/latest/projects/{project_key}/repos/{repo_slug}``.
        Only the fields the caller supplies are forwarded in the PUT body,
        so this method is safe to use for partial updates.

        Typical mutable fields accepted by Bitbucket DC include ``name``,
        ``description``, ``defaultBranch``, ``public``, and ``forkable``.
        The mixin does not validate the field names; Bitbucket rejects
        unknown keys with a 400 response which the caller can propagate.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            **fields: Fields to update. Only provided keys are sent.

        Returns:
            The updated repository object.
        """
        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
        data: dict[str, Any] = dict(fields)

        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating repository {project_key}/{repo_slug}: "
                f"{result}"
            )
        return result

    def fork_repository(
        self,
        source_project: str,
        source_slug: str,
        *,
        dest_project: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Fork a repository into a different project.

        Wraps ``POST /rest/api/latest/projects/{source_project}/repos/{source_slug}``
        with the DC fork payload shape
        ``{"name": name, "project": {"key": dest_project}}``. When ``name``
        is omitted Bitbucket uses the source repository's slug for the fork.

        Args:
            source_project: Project key of the repository to fork from
            source_slug: Slug of the repository to fork from
            dest_project: Project key the fork will land in
            name: Optional name for the forked repository. Omit to reuse the
                source repository's name.

        Returns:
            The created fork repository object.
        """
        url = f"/rest/api/latest/projects/{source_project}/repos/{source_slug}"
        data: dict[str, Any] = {"project": {"key": dest_project}}
        if name is not None:
            data["name"] = name

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response forking {source_project}/{source_slug} "
                f"into {dest_project}: {result}"
            )
        return result

    # ------------------------------------------------------------------
    # Project admin (Requirement 5)
    # ------------------------------------------------------------------

    def create_project(
        self,
        *,
        key: str,
        name: str,
        description: str | None = None,
        public: bool = False,
    ) -> dict[str, Any]:
        """Create a new Bitbucket project.

        Wraps ``POST /rest/api/latest/projects``.

        Args:
            key: Project key (uppercase letters, digits, and underscores).
                Bitbucket uses this as the immutable project identifier.
            name: Display name for the project.
            description: Optional project description.
            public: Whether the project is publicly visible.

        Returns:
            The created project object.
        """
        url = "/rest/api/latest/projects"
        data: dict[str, Any] = {
            "key": key,
            "name": name,
            "public": public,
        }
        if description is not None:
            data["description"] = description

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response creating project {key}: {result}"
            )
        return result

    def update_project(
        self,
        project_key: str,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update mutable fields on an existing project.

        Wraps ``PUT /rest/api/latest/projects/{project_key}``. Only the
        fields supplied by the caller are forwarded, so this method is
        safe to use for partial updates.

        Typical mutable fields accepted by Bitbucket DC include ``name``,
        ``description``, ``avatar``, and ``public``. The mixin does not
        validate field names; Bitbucket rejects unknown keys with a 400
        response the caller can propagate.

        Args:
            project_key: The project key
            **fields: Fields to update. Only provided keys are sent.

        Returns:
            The updated project object.
        """
        url = f"/rest/api/latest/projects/{project_key}"
        data: dict[str, Any] = dict(fields)

        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating project {project_key}: {result}"
            )
        return result
