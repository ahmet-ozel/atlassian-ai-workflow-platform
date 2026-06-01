"""Default-reviewer rule operations for Bitbucket Data Center.

Wraps the ``/rest/default-reviewers/1.0/projects/{project_key}/repos/{repo_slug}/conditions``
endpoints exposed by the Default Reviewers plugin that ships with
Bitbucket DC. A "condition" (a.k.a. default-reviewer rule) pairs a
source/target ref matcher with a list of reviewers and a required-approvals
count, so newly opened pull requests that match the matcher automatically
gain the specified reviewers.

Unlike most Bitbucket DC list endpoints, the conditions list endpoint
returns a flat JSON array rather than the usual paged envelope, so this
mixin uses ``self.bitbucket.get`` directly instead of
``self._get_paged_results``.
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.default_reviewers")


class DefaultReviewersMixin(BitbucketClient):
    """Mixin providing default-reviewer rule CRUD for Bitbucket DC."""

    def list_default_reviewers(
        self,
        project_key: str,
        repo_slug: str,
    ) -> list[dict[str, Any]]:
        """List default-reviewer rules (conditions) on a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug

        Returns:
            List of condition objects. Each object includes ``id``,
            ``sourceRefMatcher``, ``targetRefMatcher``, ``reviewers`` and
            ``requiredApprovals``.
        """
        url = (
            f"/rest/default-reviewers/1.0/projects/{project_key}"
            f"/repos/{repo_slug}/conditions"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, list):
            raise ValueError(
                f"Unexpected response listing default reviewers in "
                f"{project_key}/{repo_slug}: {result}"
            )
        return result

    def get_default_reviewer_rule(
        self,
        project_key: str,
        repo_slug: str,
        rule_id: int,
    ) -> dict[str, Any]:
        """Fetch a single default-reviewer rule by id.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            rule_id: The numeric condition id

        Returns:
            Condition object.
        """
        url = (
            f"/rest/default-reviewers/1.0/projects/{project_key}"
            f"/repos/{repo_slug}/conditions/{rule_id}"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response for default-reviewer rule "
                f"{rule_id} in {project_key}/{repo_slug}: {result}"
            )
        return result

    def create_default_reviewer_rule(
        self,
        project_key: str,
        repo_slug: str,
        *,
        source_matcher: dict[str, Any],
        target_matcher: dict[str, Any],
        reviewers: list[dict[str, Any]],
        required_approvals: int,
    ) -> dict[str, Any]:
        """Create a new default-reviewer rule.

        The matcher objects follow Bitbucket DC's ref-matcher schema, e.g.
        ``{"id": "refs/heads/main", "type": {"id": "BRANCH"}}`` or
        ``{"id": "ANY_REF_MATCHER_ID", "type": {"id": "ANY_REF"}}``.
        Reviewers are user objects, typically shaped as ``{"id": 42}`` or
        ``{"name": "jdoe"}``.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            source_matcher: Source ref matcher object
            target_matcher: Target ref matcher object
            reviewers: List of reviewer user objects
            required_approvals: Number of approvals required from this rule

        Returns:
            Created condition object.
        """
        url = (
            f"/rest/default-reviewers/1.0/projects/{project_key}"
            f"/repos/{repo_slug}/conditions"
        )
        data: dict[str, Any] = {
            "sourceMatcher": source_matcher,
            "targetMatcher": target_matcher,
            "reviewers": list(reviewers),
            "requiredApprovals": required_approvals,
        }

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response creating default-reviewer rule in "
                f"{project_key}/{repo_slug}: {result}"
            )
        return result

    def update_default_reviewer_rule(
        self,
        project_key: str,
        repo_slug: str,
        rule_id: int,
        **fields: Any,
    ) -> dict[str, Any]:
        """Update an existing default-reviewer rule.

        Accepts any mutable condition fields (``sourceMatcher``,
        ``targetMatcher``, ``reviewers``, ``requiredApprovals``) as keyword
        arguments and forwards them as the PUT body. Callers are
        responsible for shaping the matcher and reviewer objects to match
        Bitbucket DC's schema.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            rule_id: The numeric condition id
            **fields: Fields to update on the rule.

        Returns:
            Updated condition object.
        """
        url = (
            f"/rest/default-reviewers/1.0/projects/{project_key}"
            f"/repos/{repo_slug}/conditions/{rule_id}"
        )
        result = self.bitbucket.put(url, data=dict(fields))
        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response updating default-reviewer rule "
                f"{rule_id} in {project_key}/{repo_slug}: {result}"
            )
        return result

    def delete_default_reviewer_rule(
        self,
        project_key: str,
        repo_slug: str,
        rule_id: int,
    ) -> None:
        """Delete a default-reviewer rule.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            rule_id: The numeric condition id
        """
        url = (
            f"/rest/default-reviewers/1.0/projects/{project_key}"
            f"/repos/{repo_slug}/conditions/{rule_id}"
        )
        self.bitbucket.delete(url)
