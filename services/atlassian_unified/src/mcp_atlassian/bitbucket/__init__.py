"""Bitbucket Data Center API module for mcp_atlassian.

This module provides Bitbucket Server/Data Center API client implementations.
"""

from .admin import AdminMixin
from .branches import BranchesMixin
from .branching_model import BranchingModelMixin
from .cherry_pick import CherryPickMixin
from .client import BitbucketClient
from .code_insights import CodeInsightsMixin
from .commit_comments import CommitCommentsMixin
from .commits import CommitsMixin
from .config import BitbucketConfig
from .default_reviewers import DefaultReviewersMixin
from .deployments import DeploymentsMixin
from .markup import MarkupMixin
from .pr_participants import PRParticipantsMixin
from .pr_tasks import PullRequestTasksMixin
from .pull_requests import PullRequestsMixin
from .reactions import ReactionsMixin
from .repo_labels import RepoLabelsMixin
from .repositories import RepositoriesMixin
from .required_builds import RequiredBuildsMixin
from .users import UsersMixin
from .watching import WatchingMixin
from .webhooks import WebhooksMixin


class BitbucketFetcher(
    PullRequestsMixin,
    PullRequestTasksMixin,
    RepositoriesMixin,
    BranchesMixin,
    CommitsMixin,
    CodeInsightsMixin,
    UsersMixin,
    # --- atlassian-dc-tool-parity mixins (DC-only additions) ---
    DefaultReviewersMixin,
    WebhooksMixin,
    RequiredBuildsMixin,
    AdminMixin,
    ReactionsMixin,
    WatchingMixin,
    CommitCommentsMixin,
    MarkupMixin,
    RepoLabelsMixin,
    DeploymentsMixin,
    PRParticipantsMixin,
    CherryPickMixin,
    BranchingModelMixin,
):
    """Main Bitbucket client class providing access to all Bitbucket DC operations.

    This class inherits from multiple mixins that provide specific functionality:
    - PullRequestsMixin: Pull request operations (list, get, create, merge, approve, etc.)
    - PullRequestTasksMixin: PR task / blocker-comment operations
    - RepositoriesMixin: Repository and project operations, incl. file write/delete
    - BranchesMixin: Branch and tag operations (create/delete), branch restrictions (read)
    - CommitsMixin: Commit, diff, compare, and build-status operations
    - CodeInsightsMixin: Code Insights reports and annotations
    - UsersMixin: User lookup and search
    - DefaultReviewersMixin: Default-reviewer rule CRUD
    - WebhooksMixin: Repository webhook CRUD (DC 5.4+)
    - RequiredBuildsMixin: Required-builds merge-check CRUD (plugin-gated)
    - AdminMixin: Repository and project admin create/update/fork (no delete)
    - ReactionsMixin: PR comment emoji reactions (DC 8.8+)
    - WatchingMixin: Self-scoped watch/unwatch for PRs and repositories
    - CommitCommentsMixin: General and inline commit comment CRUD
    - MarkupMixin: Render Bitbucket-flavoured markup to HTML (read-only preview)
    - RepoLabelsMixin: Repository label list/add/remove (idempotent add)
    - DeploymentsMixin: Deployment list/get (read-only, DC 7.10+)
    - PRParticipantsMixin: Pull-request participants read-only listing
    - CherryPickMixin: Cherry-pick a commit onto a target branch (maps 409 conflicts)
    - BranchingModelMixin: Branching-model read (development/production refs
      and feature/release/hotfix/bugfix prefix matchers; no write)
    """

    pass


__all__ = [
    "BitbucketFetcher",
    "BitbucketConfig",
    "BitbucketClient",
]
