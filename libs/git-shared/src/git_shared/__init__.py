"""git-shared — GitPython-backed adapter for prompt CRUD / PR flows.

Re-exports the public API so callers can simply do::

    from git_shared import GitRepo, PullRequestOpener, PullRequestRef
    from git_shared import GitRepoError, BranchAlreadyExistsError, MergeConflictError

The package mirrors the design in
``.kiro/specs/platform-mimari-ops/design.md`` §`PromptsGitRouter` and
backs Requirement 2.2 (git CRUD endpoints) and Requirement 2.9
(template-format validation prior to writing).
"""

from .errors import (
    BranchAlreadyExistsError,
    BranchNotFoundError,
    GitRepoError,
    MergeConflictError,
    PullRequestError,
)
from .pull_request import (
    BitbucketPullRequestOpener,
    PullRequestOpener,
    PullRequestRef,
)
from .repo import GitRepo, GitAuthor, GitCommit

__all__ = [
    "BitbucketPullRequestOpener",
    "BranchAlreadyExistsError",
    "BranchNotFoundError",
    "GitAuthor",
    "GitCommit",
    "GitRepo",
    "GitRepoError",
    "MergeConflictError",
    "PullRequestError",
    "PullRequestOpener",
    "PullRequestRef",
]
