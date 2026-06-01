"""Exception hierarchy for the ``git_shared`` package.

The hierarchy is shallow on purpose — callers (the
``PromptsGitRouter`` in particular) only need to distinguish three
failure modes:

* :class:`BranchAlreadyExistsError` — the requested draft branch name
  is already taken. Mapped to ``409 Conflict`` by the router.
* :class:`BranchNotFoundError` — a branch the caller asked us to read
  from / commit to does not exist. Mapped to ``404 Not Found``.
* :class:`MergeConflictError` — opening a PR failed because the draft
  branch cannot be merged cleanly into ``main``. Mapped to
  ``409 Conflict`` and triggers a ``prompt_pr_conflict`` audit event.
* :class:`PullRequestError` — generic PR failure (Bitbucket API
  unreachable, etc.). Mapped to ``502 Bad Gateway``.

Every concrete subclass derives from :class:`GitRepoError` so callers
can install a single broad ``except`` clause as a fall-back.
"""

from __future__ import annotations


class GitRepoError(Exception):
    """Base class for all ``git_shared`` failures."""


class BranchAlreadyExistsError(GitRepoError):
    """Raised when ``create_branch_from_main`` collides with an existing ref."""


class BranchNotFoundError(GitRepoError):
    """Raised when a branch we tried to read / commit to does not exist."""


class MergeConflictError(GitRepoError):
    """Raised when a PR can be opened but ``main`` and the draft conflict.

    The router captures this exception, writes a ``prompt_pr_conflict``
    audit event (Requirement 2.2) and surfaces ``409 Conflict`` to the
    caller.
    """


class PullRequestError(GitRepoError):
    """Raised when the upstream PR provider (Bitbucket) rejects the call."""


__all__ = [
    "BranchAlreadyExistsError",
    "BranchNotFoundError",
    "GitRepoError",
    "MergeConflictError",
    "PullRequestError",
]
