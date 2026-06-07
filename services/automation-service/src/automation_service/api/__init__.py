"""``automation_service.api`` - externally addressable HTTP endpoints.

This package holds the API surface that lives **outside** of the
``/admin/*`` (operator) and ``/webhooks/*`` (Atlassian / Slack) prefixes.
Each module in here mounts a FastAPI router under ``/api/...`` and
follows the same wiring contract as the rest of the service:

* The router itself is **stateless** - no module-level Temporal client,
  Vault client, audit logger, etc.
* Collaborators are pulled off ``request.app.state.<key>`` (a frozen
  dependency container) so unit tests can inject hand-built fakes.

Modules:

* :mod:`automation_service.api.cancel` - ``POST
  /api/workflows/{workflow_id}/cancel`` with the
  ``is_cancel_authorized`` predicate.
* :mod:`automation_service.api.repo_sync` - ``POST
  /admin/departments/{id}/repo-mappings/sync``. Pure diff helper at
  :mod:`temporal_shared.repo_sync`; HTTP shim + admin RBAC live here.
* :mod:`automation_service.api.po_review` - ``GET /api/orphan-branches``
  + ``GET /api/po-review-inbox`` + the three per-PR POST actions. Pure
  set-algebra helpers at :mod:`temporal_shared.po_review`; HTTP
  shim + RBAC + diff-summary cache live here.
"""

from __future__ import annotations

from .cancel import (
    CancelEndpointDeps,
    IssueRef,
    is_cancel_authorized,
    router as cancel_router,
)
from .po_review import (
    BitbucketBranchScanner,
    BitbucketPullRequestScanner,
    DiffSummaryProvider,
    PoReviewActions,
    PoReviewEndpointDeps,
    router as po_review_router,
)
from .repo_sync import (
    BitbucketRepoScanner,
    RepoSyncEndpointDeps,
    SupportsDepartmentsRepo,
    router as repo_sync_router,
)
from .webhooks import (
    BITBUCKET_LOOP_GUARD_EVENTS,
    BITBUCKET_SUPPORTED_EVENTS,
    JIRA_SUPPORTED_EVENTS,
    WebhooksEndpointDeps,
    router as webhooks_router,
)

__all__ = [
    # cancel API
    "CancelEndpointDeps",
    "IssueRef",
    "cancel_router",
    "is_cancel_authorized",
    # po_review API
    "BitbucketBranchScanner",
    "BitbucketPullRequestScanner",
    "DiffSummaryProvider",
    "PoReviewActions",
    "PoReviewEndpointDeps",
    "po_review_router",
    # repo_sync API
    "BitbucketRepoScanner",
    "RepoSyncEndpointDeps",
    "SupportsDepartmentsRepo",
    "repo_sync_router",
    # webhooks API
    "BITBUCKET_LOOP_GUARD_EVENTS",
    "BITBUCKET_SUPPORTED_EVENTS",
    "JIRA_SUPPORTED_EVENTS",
    "WebhooksEndpointDeps",
    "webhooks_router",
]
