"""``automation_service.api`` — externally addressable HTTP endpoints.

This package holds the API surface that lives **outside** of the
``/admin/*`` (operator) and ``/webhooks/*`` (Atlassian / Slack) prefixes.
Each module in here mounts a FastAPI router under ``/api/...`` and
follows the same wiring contract as the rest of the service:

* The router itself is **stateless** — no module-level Temporal client,
  Vault client, audit logger, etc.
* Collaborators are pulled off ``request.app.state.<key>`` (a frozen
  dependency container) so unit tests can inject hand-built fakes.

Modules:

* :mod:`automation_service.api.cancel` — ``POST
  /api/workflows/{workflow_id}/cancel`` (R11.1, design Property 11
  predicate ``is_cancel_authorized``).
* :mod:`automation_service.api.repo_sync` — ``POST
  /admin/departments/{id}/repo-mappings/sync`` (R10.7 / MIMARI §16.16
  N7, design "repo_mapping_sync API"). Pure diff helper at
  :mod:`temporal_shared.repo_sync`; HTTP shim + admin RBAC live here.
* :mod:`automation_service.api.po_review` — ``GET /api/orphan-branches``
  + ``GET /api/po-review-inbox`` + the three per-PR POST actions
  (R10.3, R10.4 — workflows spec, design "PO Review API"). Pure
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
    # cancel API (R11.1)
    "CancelEndpointDeps",
    "IssueRef",
    "cancel_router",
    "is_cancel_authorized",
    # po_review API (R10.3, R10.4)
    "BitbucketBranchScanner",
    "BitbucketPullRequestScanner",
    "DiffSummaryProvider",
    "PoReviewActions",
    "PoReviewEndpointDeps",
    "po_review_router",
    # repo_sync API (R10.7 / N7)
    "BitbucketRepoScanner",
    "RepoSyncEndpointDeps",
    "SupportsDepartmentsRepo",
    "repo_sync_router",
    # webhooks API (R3.1, R3.2, R3.3, R3.9)
    "BITBUCKET_LOOP_GUARD_EVENTS",
    "BITBUCKET_SUPPORTED_EVENTS",
    "JIRA_SUPPORTED_EVENTS",
    "WebhooksEndpointDeps",
    "webhooks_router",
]
