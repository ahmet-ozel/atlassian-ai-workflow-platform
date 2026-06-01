"""Branching model read operations for Bitbucket Data Center.

Wraps the Branch-Utils plugin endpoint
``/rest/branch-utils/latest/projects/{k}/repos/{r}/branchmodel`` which
returns the repository's configured branching-model: the development
and production branch references plus the prefix matchers for
feature / release / hotfix / bugfix branch types.

Write access to the branching model is deliberately NOT exposed —
modifying branch prefixes or the development/production pointers is an
administrative action that can invalidate in-flight pull requests and
automation, so it stays in the Bitbucket UI (see Requirement 14.2).
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.branching_model")


class BranchingModelMixin(BitbucketClient):
    """Mixin providing read-only access to the branching-model config."""

    def get_branching_model(
        self,
        project_key: str,
        repo_slug: str,
    ) -> dict[str, Any]:
        """Return the repository's branching-model configuration.

        Calls ``GET /rest/branch-utils/latest/projects/{project_key}/
        repos/{repo_slug}/branchmodel`` and returns the decoded JSON
        payload. The response typically looks like::

            {
                "development": {"refId": "refs/heads/main", ...},
                "production": {"refId": "refs/heads/release", ...},
                "types": [
                    {"id": "FEATURE",  "displayName": "Feature",
                     "prefix": "feature/",  "enabled": true},
                    {"id": "RELEASE",  "displayName": "Release",
                     "prefix": "release/",  "enabled": true},
                    {"id": "HOTFIX",   "displayName": "Hotfix",
                     "prefix": "hotfix/",   "enabled": true},
                    {"id": "BUGFIX",   "displayName": "Bugfix",
                     "prefix": "bugfix/",   "enabled": false}
                ]
            }

        Args:
            project_key: The project key (for example ``"PROJ"``).
            repo_slug: The repository slug (for example ``"repo"``).

        Returns:
            The branching-model configuration dict. Returns an empty
            dict if the endpoint responds with a non-dict payload
            (for example when the Branch-Utils plugin is disabled and
            the server returns an empty body).
        """
        url = (
            f"/rest/branch-utils/latest/projects/{project_key}"
            f"/repos/{repo_slug}/branchmodel"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            logger.debug(
                "Bitbucket branching-model endpoint returned unexpected "
                "payload shape for %s/%s: %s",
                project_key,
                repo_slug,
                type(result).__name__,
            )
            return {}
        return result
