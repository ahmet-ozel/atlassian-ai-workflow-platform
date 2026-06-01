"""Cherry-pick operations for Bitbucket Data Center.

Bitbucket DC exposes a cherry-pick helper under
``/rest/api/latest/projects/{k}/repos/{r}/cherry-pick`` that applies an
existing commit onto a target branch without leaving the platform. This
mixin wraps that endpoint for the ``toolset:bitbucket_commits`` tool
layer and surfaces conflicts as a structured :class:`CherryPickConflictError`
so the server-tool layer can map them onto a ``cherry_pick_conflict``
error code without re-parsing the upstream response shape.

Only the cherry-pick primitive lives here — the ``build_receipt``
wrapping around a successful pick is performed by the server-tool layer
on the resulting commit hash.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.cherry_pick")


class CherryPickConflictError(Exception):
    """Raised when Bitbucket reports a cherry-pick conflict (HTTP 409).

    The ``conflicts`` attribute carries the conflicting paths/hunks as
    reported by Bitbucket under ``errors[].conflicts`` in the 409
    response body. Callers (typically the server-tool layer) translate
    this into a structured ``cherry_pick_conflict`` error code per
    Requirement 13.2.
    """

    def __init__(
        self,
        message: str,
        *,
        conflicts: list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.conflicts: list[Any] = list(conflicts) if conflicts else []


class CherryPickMixin(BitbucketClient):
    """Mixin providing cherry-pick operations for Bitbucket DC."""

    def cherry_pick_commit(
        self,
        project_key: str,
        repo_slug: str,
        *,
        source_commit: str,
        target_branch: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Cherry-pick a commit onto a target branch.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            source_commit: Commit hash to cherry-pick (the ``commitId``
                field on the upstream request body)
            target_branch: Branch to apply the pick onto (the
                ``destinationBranch`` field on the upstream request
                body; may be given as ``main`` or
                ``refs/heads/main`` — Bitbucket accepts either)
            message: Optional override for the new commit's message.
                When omitted Bitbucket reuses the source commit's
                message.

        Returns:
            The upstream response dict, typically shaped like
            ``{"id": "<new-commit-hash>", ...}`` where ``id`` is the
            newly created commit on ``target_branch``.

        Raises:
            CherryPickConflictError: When Bitbucket responds with
                ``409 Conflict`` and a body containing
                ``errors[].conflicts`` — the conflicting paths are
                attached to the exception's ``conflicts`` attribute.
            requests.exceptions.HTTPError: For any other non-success
                response; callers (the server-tool layer) map these
                onto the generic error envelope.
            ValueError: When the upstream returns a non-dict payload.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/cherry-pick"
        )
        data: dict[str, Any] = {
            "commitId": source_commit,
            "destinationBranch": target_branch,
        }
        if message is not None:
            data["message"] = message

        try:
            result = self.bitbucket.post(url, data=data)
        except HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 409:
                conflicts = _extract_conflicts(e)
                logger.debug(
                    f"Cherry-pick of {source_commit} onto {target_branch} "
                    f"conflicted on {len(conflicts)} path(s)."
                )
                raise CherryPickConflictError(
                    f"Cherry-pick conflict applying {source_commit} "
                    f"onto {target_branch}",
                    conflicts=conflicts,
                ) from e
            raise

        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response cherry-picking {source_commit} "
                f"onto {target_branch}: {result}"
            )
        return result


def _extract_conflicts(error: HTTPError) -> list[Any]:
    """Extract ``errors[].conflicts`` entries from a 409 HTTPError body.

    Bitbucket DC 409 responses for cherry-pick conflicts follow the
    standard error envelope::

        {"errors": [{"context": None, "message": "...",
                      "conflicts": [{"ourChange": {...},
                                      "theirChange": {...}}, ...]}]}

    This helper walks that structure defensively — any unexpected
    shape collapses to an empty list so callers always receive a
    serializable ``list``.
    """
    response = getattr(error, "response", None)
    if response is None:
        return []

    try:
        body = response.json()
    except ValueError:
        return []

    if not isinstance(body, dict):
        return []

    errors = body.get("errors")
    if not isinstance(errors, list):
        return []

    conflicts: list[Any] = []
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        entry_conflicts = entry.get("conflicts")
        if isinstance(entry_conflicts, list):
            conflicts.extend(entry_conflicts)
    return conflicts
