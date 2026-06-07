"""``GET /api/orphan-branches`` + PO Review Inbox API endpoints.

* ``GET /api/orphan-branches?dept_id=<id>`` - list ``ai/*`` branches
  in the dept's Bitbucket workspace that have no associated pull
  request. Each entry carries an LLM-rendered diff summary served
  through :class:`DiffSummaryCacheRepo` (cache hit path: only the
  *first* observer of a given
  ``diff_hash`` pays the LLM call).
* ``GET /api/po-review-inbox?dept_id=<id>`` - list the dept's draft
  pull requests authored by a known bot account (the "PO Review
  Inbox" surfaced in the Streamlit page).
* ``POST /api/po-review-inbox/{pr_id}/open-draft`` - flip a
  closed/declined draft PR back to ``open`` so the bot can re-iterate.
* ``POST /api/po-review-inbox/{pr_id}/request-changes`` - post a
  PO-side "request changes" review on the PR.
* ``POST /api/po-review-inbox/{pr_id}/approve-note`` - post a
  PO-side approval **note** (an inline comment, **not** a Bitbucket
  approval) so the bot can advance without claiming the human
  promoted the PR.

Authorization
-------------

Every endpoint is gated by :func:`auth_shared.check`:

* ``viewer`` role (or higher) is required to *read* the two list
  endpoints - the PO Review pages render in the Streamlit dashboard
  and any authenticated user with dept membership may consult them.
* ``lead`` role (or higher) is required to *act* on a PR (open-draft,
  request-changes, approve-note). ``lead`` is the lowest role that
  can perform PO actions.
* ``dept_admin`` is dept-scoped: it may **only** access PRs / branches
  inside its own ``dept_ids``. A ``dept_admin`` requesting another
  ``dept_id`` receives HTTP 403 + ``rbac_denied`` audit.
* ``admin`` is global and bypasses the dept-scope check.

The dept-scope check is a single line - ``check(actor_ctx,
required_role, dept_id=...)`` - because the foundation
:func:`auth_shared.check` already encodes "admin always passes;
dept-scoped roles must match ``dept_ids``".

``temporal_shared.po_review`` owns the pure set-algebra
  helpers (:func:`compute_orphan_branches`,
  :func:`compute_po_review_inbox`); this module is the HTTP /
  authorization / MCP wiring shim.
The module mirrors the dependency-container pattern used by
  :mod:`automation_service.api.cancel` and
  :mod:`automation_service.api.repo_sync` for collaborator
  injection - the router itself is stateless; tests build a
  :class:`PoReviewEndpointDeps` directly.

Pure pieces of the decision live in
:mod:`temporal_shared.po_review` (:class:`Branch`,
:class:`PullRequest`, :func:`compute_orphan_branches`,
:func:`compute_po_review_inbox`); this module owns nothing beyond
the HTTP shim, the MCP / cache wiring, and the audit + RBAC plumbing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from auth_shared import (
    AuthContext,
    InvalidTokenError,
    OIDCValidator,
    PermissionDenied,
    Role,
    check as auth_check,
)
from temporal_shared import (
    Branch,
    PullRequest,
    compute_orphan_branches,
    compute_po_review_inbox,
)


__all__ = [
    "BitbucketBranchScanner",
    "BitbucketPullRequestScanner",
    "DiffSummaryProvider",
    "PoReviewActions",
    "PoReviewEndpointDeps",
    "router",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit action / resource constants - single source of truth
# ---------------------------------------------------------------------------

#: Audit ``action`` token written when a non-bot actor reads the
#: orphan-branches list. ``ok`` for success; mirrors the foundation
#: convention of "every endpoint emits one audit row per call".
_AUDIT_ACTION_ORPHAN_LISTED: str = "orphan_branches_listed"

#: Audit ``action`` token written when a non-bot actor reads the PO
#: Review Inbox.
_AUDIT_ACTION_INBOX_LISTED: str = "po_review_inbox_listed"

#: Audit ``action`` token written when an actor flips a draft PR
#: back to open via ``POST /api/po-review-inbox/{pr_id}/open-draft``.
_AUDIT_ACTION_OPEN_DRAFT: str = "po_review_open_draft"

#: Audit ``action`` token written when an actor posts a
#: request-changes review on a PR.
_AUDIT_ACTION_REQUEST_CHANGES: str = "po_review_request_changes"

#: Audit ``action`` token written when an actor posts an approve
#: **note** on a PR (deliberately *not* a Bitbucket approval - the
#: human is leaving feedback, not promoting the PR).
_AUDIT_ACTION_APPROVE_NOTE: str = "po_review_approve_note"

#: Audit ``action`` token written on every authorisation failure.
_AUDIT_ACTION_RBAC_DENIED: str = "rbac_denied"

#: Audit ``action`` token written when the upstream Bitbucket scan
#: fails so operators can distinguish "no permission" from
#: "Bitbucket flaked".
_AUDIT_ACTION_SCAN_FAILED: str = "po_review_scan_failed"


# ---------------------------------------------------------------------------
# Collaborator protocols - keep the router trivially mockable
# ---------------------------------------------------------------------------


@runtime_checkable
class BitbucketBranchScanner(Protocol):
    """Structural type for the MCP-side branch scanner.

    Production wiring binds this to a thin coroutine around
    ``mcp_client.atlassian_client``'s Bitbucket branch listing tool.
    The Protocol is declared here rather than imported so the
    endpoint is exercisable in unit tests without a live MCP service.

    Returns a sequence of branch descriptors ``{"name", "last_commit_at",
    "diff_hash", ...}``. The router projects these into
    :class:`temporal_shared.po_review.Branch` instances before
    handing them to the pure helper. The ``diff_hash`` field is
    consumed separately by the diff-summary cache (it is *not* part
    of the pure helper's input).
    """

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class BitbucketPullRequestScanner(Protocol):
    """Structural type for the MCP-side pull request scanner.

    Production wiring binds this to a thin coroutine around
    ``mcp_client.atlassian_client``'s Bitbucket pull request listing
    tool.

    Returns a sequence of PR descriptors ``{"id", "source_branch",
    "is_draft", "author_account_id", "title"}``. The router projects
    these into :class:`temporal_shared.po_review.PullRequest`
    instances.
    """

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]: ...


#: Async callable producing an LLM diff summary for ``diff_hash``.
#: The endpoint passes this to
#: :meth:`DiffSummaryProvider.get_or_compute` - production wiring
#: binds the LLM-side render coroutine; tests inject a fake that
#: returns canned strings.
LlmDiffCallback = Callable[[str], Awaitable[str]]


@runtime_checkable
class DiffSummaryProvider(Protocol):
    """Structural type matching the slice of
    :class:`automation_service.diff_summary_cache.DiffSummaryCacheRepo`
    used by the orphan-branches endpoint.

    Production wiring binds this directly to the
    :class:`DiffSummaryCacheRepo` instance held on the application
    state. The router only needs the read + cached-compute surface
    (only the first observer of a given
    ``diff_hash`` pays the LLM call); the protocol declares both
    methods so test doubles can short-circuit either path.
    """

    async def get(self, diff_hash: str) -> str | None: ...

    async def get_or_compute(
        self,
        diff_hash: str,
        llm_callback: LlmDiffCallback,
    ) -> str: ...


@runtime_checkable
class PoReviewActions(Protocol):
    """Structural type for the action endpoints' Bitbucket adapter.

    The three POST endpoints (``open-draft``, ``request-changes``,
    ``approve-note``) each call exactly one method on this object.
    Splitting them into a single, well-typed protocol keeps the
    router free of Bitbucket-specific knowledge and lets unit tests
    record the calls without touching the wider MCP client.

    Each method receives the dept_id (so the adapter can resolve
    credentials) and the Bitbucket-assigned numeric PR id.
    Implementations are responsible for translating the action into
    the appropriate Bitbucket REST call (or MCP tool invocation).
    """

    async def open_draft(self, dept_id: str, pr_id: int) -> None: ...

    async def request_changes(
        self, dept_id: str, pr_id: int, *, comment: str
    ) -> None: ...

    async def approve_note(
        self, dept_id: str, pr_id: int, *, comment: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# Dependency container - injected via ``request.app.state.po_review``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoReviewEndpointDeps:
    """Collaborators the PO Review router pulls from ``app.state``.

    The router owns no state of its own. Production wiring builds
    one of these in :func:`automation_service.app.create_app`; tests
    construct the dataclass directly with hand-built fakes.

    Attributes
    ----------
    oidc_validator:
        :class:`auth_shared.OIDCValidator` authenticating the bearer
        token. Production wiring uses ``OIDCValidator(OIDCConfig
        .from_env())``; dev / test wiring may pass a dev-mode
        validator (``auth_mode="dev"``).
    branch_scanner:
        Coroutine returning the dept's Bitbucket branches as a
        sequence of ``{"name", "last_commit_at", "diff_hash"}``
        mappings. Bound to ``mcp_client.atlassian_client``'s branch
        listing tool in production.
    pr_scanner:
        Coroutine returning the dept's Bitbucket pull requests as a
        sequence of ``{"id", "source_branch", "is_draft",
        "author_account_id", "title"}`` mappings.
    bot_account_ids:
        Coroutine returning the frozen set of bot ``account_id``
        values for the dept. Production wiring reads this from the
        foundation departments registry; tests inject a canned set.
    diff_summary_cache:
        :class:`DiffSummaryProvider` (typically
        :class:`DiffSummaryCacheRepo`) used by the orphan-branches
        endpoint to attach an LLM diff summary to each row.
    llm_diff_callback:
        Async callable that produces the LLM summary for a given
        ``diff_hash`` on cache miss. The endpoint never calls this
        directly - it hands it to
        :meth:`DiffSummaryProvider.get_or_compute`.
    actions:
        :class:`PoReviewActions` adapter the three POST endpoints
        call into.
    audit_logger:
        Audit sink for every endpoint's audit row.
    clock:
        Optional callable returning the current UTC datetime. When
        omitted, the router uses ``datetime.now(timezone.utc)``.
        Tests inject a frozen clock so audit timestamps are
        deterministic.
    """

    oidc_validator: OIDCValidator
    branch_scanner: BitbucketBranchScanner
    pr_scanner: BitbucketPullRequestScanner
    bot_account_ids: Callable[[str], Awaitable[frozenset[str]]]
    diff_summary_cache: DiffSummaryProvider
    llm_diff_callback: LlmDiffCallback
    actions: PoReviewActions
    audit_logger: AuditLogger
    clock: Callable[[], datetime] | None = None


def _deps(request: Request) -> PoReviewEndpointDeps:
    """Pull the :class:`PoReviewEndpointDeps` off ``app.state``.

    Surfaces a deployment misconfiguration (router mounted but
    collaborators not wired) as a clear 500 instead of a downstream
    :class:`AttributeError`.
    """

    deps = getattr(request.app.state, "po_review", None)
    if not isinstance(deps, PoReviewEndpointDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="po_review router is not wired (app.state.po_review missing)",
        )
    return deps


def _now(deps: PoReviewEndpointDeps) -> datetime:
    """Return the current UTC timestamp using the injected clock."""

    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# AuthN helpers - bearer token extraction + OIDC validation
# ---------------------------------------------------------------------------


def _extract_bearer_token(authorization: str | None) -> str:
    """Return the bearer token from an ``Authorization`` header.

    Raises :class:`HTTPException` ``401`` for missing or malformed
    headers. The error detail is intentionally generic so the
    response body never leaks whether the token format vs the token
    contents was the problem.
    """

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed Authorization header",
        )
    return parts[1].strip()


def _resolve_actor_user_id(claims: Mapping[str, Any]) -> str | None:
    """Pick the ``actor_user_id`` from a decoded claim dict.

    Prefers ``account_id`` (the Atlassian-specific claim sometimes
    minted by an SSO bridge), falling back to the canonical OIDC
    ``sub``. Returns ``None`` when neither is present so the caller
    can map the case to HTTP 401.
    """

    for key in ("account_id", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_auth_context(claims: Mapping[str, Any]) -> AuthContext | None:
    """Construct an :class:`AuthContext` from decoded OIDC claims.

    Returns ``None`` when the claims are missing the
    ``account_id`` / ``sub`` pair the foundation guard relies on.
    The role is read from the canonical ``role`` claim; missing or
    non-string roles fall through to ``"viewer"`` (the lowest
    privilege) so the foundation guard rejects elevated requests by
    default - matching the wider service convention of preferring
    "deny on ambiguous" for sensitive endpoints.
    """

    actor_id = _resolve_actor_user_id(claims)
    if actor_id is None:
        return None

    raw_role = claims.get("role")
    actor_role: str = (
        raw_role
        if isinstance(raw_role, str)
        and raw_role in {"viewer", "lead", "admin", "dept_admin"}
        else "viewer"
    )

    raw_dept_ids = claims.get("dept_ids") or ()
    dept_ids: frozenset[str]
    if isinstance(raw_dept_ids, (list, tuple, frozenset, set)):
        dept_ids = frozenset(
            str(d) for d in raw_dept_ids if isinstance(d, str) and d
        )
    else:
        dept_ids = frozenset()

    return AuthContext(
        actor_id=actor_id,
        actor_role=actor_role,  # type: ignore[arg-type]
        dept_ids=dept_ids,
    )


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    *,
    actor_id: str,
    actor_role: str,
    dept_id: str | None,
    action: str,
    resource: str,
    result: str,
    timestamp: datetime,
    payload: dict[str, Any] | None,
) -> AuditEvent:
    """Construct an :class:`AuditEvent` with a safe ``actor_role``.

    Mirrors :mod:`automation_service.api.cancel` /
    :mod:`automation_service.api.repo_sync` - unrecognised roles
    fall back to ``"system"`` and the original role is stashed on
    ``payload["claimed_role"]`` so audit forensics can still see
    what was offered.
    """

    safe_role = (
        actor_role
        if actor_role in ("viewer", "lead", "admin", "dept_admin", "system")
        else "system"
    )
    enriched_payload: dict[str, Any] | None = payload
    if safe_role != actor_role:
        enriched_payload = dict(payload or {})
        enriched_payload["claimed_role"] = actor_role
    return AuditEvent(
        actor_id=actor_id,
        actor_role=safe_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=timestamp,
        payload=enriched_payload,
    )


async def _emit_audit(audit_logger: AuditLogger, event: AuditEvent) -> None:
    """Best-effort audit write - never let an audit error 500 the call."""

    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        _LOG.warning(
            "po_review.audit_write_failed action=%s resource=%s err=%s",
            event.action,
            event.resource,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Authorisation gate - shared by every endpoint in this router
# ---------------------------------------------------------------------------


async def _authorize(
    deps: PoReviewEndpointDeps,
    *,
    authorization: str | None,
    dept_id: str,
    required_role: Role,
    resource: str,
    audit_payload_extra: dict[str, Any] | None = None,
) -> AuthContext:
    """Authenticate the caller and enforce the dept-scoped guard.

    1. Extracts and validates the bearer token (HTTP 401 on
       malformed / unknown / invalid tokens).
    2. Builds an :class:`AuthContext` from the OIDC claims (HTTP 401
       when the token is missing the ``account_id`` / ``sub`` claim).
    3. Calls :func:`auth_shared.check(actor_ctx, required_role,
       dept_id=dept_id)` - failure raises :class:`PermissionDenied`,
       which we translate into HTTP 403 + a single ``rbac_denied``
       audit row carrying the rejected request's ``required_role``
       and ``dept_id``.

    The dept-scope check is encoded in
    :func:`auth_shared.check` itself: ``admin`` always passes; every
    other role must match the actor's ``dept_ids``. So a
    ``dept_admin`` requesting another ``dept_id`` is rejected with
    HTTP 403 by exactly the same line of code that rejects a
    ``viewer`` requesting an ``admin``-only endpoint.

    Returns the resolved :class:`AuthContext` so the caller can
    populate the success-side audit row.
    """

    token = _extract_bearer_token(authorization)
    try:
        claims = deps.oidc_validator.validate(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
        ) from exc

    actor_ctx = _build_auth_context(claims)
    if actor_ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing account_id / sub claim",
        )

    try:
        auth_check(actor_ctx, required_role, dept_id=dept_id)
    except PermissionDenied:
        payload: dict[str, Any] = {
            "required_role": required_role,
            "dept_id": dept_id,
        }
        if audit_payload_extra:
            payload.update(audit_payload_extra)
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_ctx.actor_id,
                actor_role=actor_ctx.actor_role,
                dept_id=dept_id,
                action=_AUDIT_ACTION_RBAC_DENIED,
                resource=resource,
                result="denied",
                timestamp=_now(deps),
                payload=payload,
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized",
        )

    return actor_ctx


# ---------------------------------------------------------------------------
# Branch / PR projection helpers - fold MCP responses into pure helpers
# ---------------------------------------------------------------------------


def _project_branches(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[tuple[Branch, ...], dict[str, str | None]]:
    """Project the MCP branch response into pure-helper inputs.

    Returns a pair of:

    * a tuple of :class:`Branch` dataclass instances keyed by name
      (the input the pure :func:`compute_orphan_branches` helper
      expects), and
    * a name  ``diff_hash`` lookup so the orphan-branches endpoint
      can attach a cached diff summary to each surviving entry. The
      lookup contains :data:`None` when the MCP did not supply a
      hash (degenerate branches with no commits yet).

    Malformed entries (missing ``name``) are silently skipped - they
    are not actionable for the operator.
    """

    branches: list[Branch] = []
    diff_hash_by_name: dict[str, str | None] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue

        last_commit_raw = entry.get("last_commit_at")
        last_commit_at: datetime | None
        if isinstance(last_commit_raw, datetime):
            last_commit_at = last_commit_raw
        else:
            last_commit_at = None

        branches.append(Branch(name=name, last_commit_at=last_commit_at))

        diff_hash = entry.get("diff_hash")
        diff_hash_by_name[name] = (
            diff_hash if isinstance(diff_hash, str) and diff_hash else None
        )

    return tuple(branches), diff_hash_by_name


def _project_pull_requests(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[PullRequest, ...]:
    """Project the MCP PR response into :class:`PullRequest` values.

    Malformed entries (missing ``id``, ``source_branch``, or
    ``author_account_id``) are silently skipped - they are not
    actionable for the operator and the pure helpers only need the
    well-typed subset.
    """

    prs: list[PullRequest] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        pr_id = entry.get("id")
        source_branch = entry.get("source_branch")
        author_account_id = entry.get("author_account_id")
        if not isinstance(pr_id, int):
            continue
        if not isinstance(source_branch, str) or not source_branch:
            continue
        if (
            not isinstance(author_account_id, str)
            or not author_account_id
        ):
            continue

        is_draft = bool(entry.get("is_draft", False))
        title_raw = entry.get("title", "")
        title = title_raw if isinstance(title_raw, str) else ""

        prs.append(
            PullRequest(
                id=pr_id,
                source_branch=source_branch,
                is_draft=is_draft,
                author_account_id=author_account_id,
                title=title,
            )
        )
    return tuple(prs)


def _age_days(now: datetime, last_commit_at: datetime | None) -> int | None:
    """Return the integer number of days since ``last_commit_at``.

    Tolerates a ``None`` input (returns ``None`` so the JSON
    response simply omits an age for branches whose commit metadata
    is missing). Both timestamps are assumed UTC; mismatched
    tzinfo is normalised by ``timedelta`` arithmetic on
    ``datetime.now(timezone.utc)`` and ``last_commit_at`` so a
    naive timestamp would surface as a noisy age - we prefer that
    over silently returning ``0``.
    """

    if last_commit_at is None:
        return None
    delta = now - last_commit_at
    # ``days`` is non-negative for any sane "last commit in the past"
    # so we clamp at zero to keep the JSON tidy in the (rare) case of
    # clock skew between the request handler and the Bitbucket
    # backend. A negative age would be confusing in the UI.
    return max(0, delta.days)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api", tags=["po-review"])


@router.get(
    "/orphan-branches",
    status_code=status.HTTP_200_OK,
)
async def list_orphan_branches(
    request: Request,
    dept_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Return the dept's ``ai/*`` branches that have no associated PR.

    The endpoint:

    1. Authenticates and authorises the caller (``viewer`` role
       suffices; ``dept_admin`` must match ``dept_id``; ``admin``
       passes globally).
    2. Lists branches and PRs for the dept via the injected MCP
       scanners.
    3. Runs the pure :func:`compute_orphan_branches` helper to
       isolate the bot-authored branches that have no PR.
    4. For each orphan looks up an LLM-rendered diff summary via
       :meth:`DiffSummaryProvider.get_or_compute` (cache hit on
       repeat calls; only the first observer of a given
       ``diff_hash`` pays the LLM call).
    5. Returns the orphan list as JSON, sorted oldest-first by
       ``last_commit_at`` so the longest-orphan branch surfaces at
       the top of the Streamlit page.
    """

    deps = _deps(request)
    actor_ctx = await _authorize(
        deps,
        authorization=authorization,
        dept_id=dept_id,
        required_role="viewer",
        resource=f"orphan_branches:{dept_id}",
        audit_payload_extra={
            "endpoint": "GET /api/orphan-branches",
        },
    )

    # ---------- Bitbucket scan (branches + PRs) ------------------------------
    try:
        raw_branches = await deps.branch_scanner(dept_id)
        raw_prs = await deps.pr_scanner(dept_id)
    except Exception as exc:  # noqa: BLE001 - translate to 502
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_ctx.actor_id,
                actor_role=actor_ctx.actor_role,
                dept_id=dept_id,
                action=_AUDIT_ACTION_SCAN_FAILED,
                resource=f"orphan_branches:{dept_id}",
                result="error",
                timestamp=_now(deps),
                payload={
                    "endpoint": "GET /api/orphan-branches",
                    "reason": type(exc).__name__,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"bitbucket scan failed: {type(exc).__name__}",
        ) from exc

    branches, diff_hash_by_name = _project_branches(raw_branches)
    prs = _project_pull_requests(raw_prs)

    orphans = compute_orphan_branches(branches, prs)

    # ---------- Sort oldest-first by last_commit_at --------------------------
    # ``None`` last_commit_at sorts *first* (oldest position) so a
    # branch with missing commit metadata is still surfaced - the PO
    # would otherwise miss it forever. Using a plain tuple key keeps
    # the comparison total without resorting to ``functools.cmp``.
    sorted_orphans = sorted(
        orphans,
        key=lambda b: (
            # None sorts before any real datetime when wrapped this way
            0 if b.last_commit_at is None else 1,
            b.last_commit_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
            b.name,
        ),
    )

    # ---------- Attach LLM diff summary (cache hit path) ---------------------
    now_utc = _now(deps)
    rows: list[dict[str, Any]] = []
    for branch in sorted_orphans:
        diff_hash = diff_hash_by_name.get(branch.name)
        if diff_hash:
            try:
                summary: str | None = await deps.diff_summary_cache.get_or_compute(
                    diff_hash, deps.llm_diff_callback
                )
            except Exception as exc:  # noqa: BLE001 - non-fatal
                _LOG.warning(
                    "po_review.diff_summary_failed branch=%s err=%s",
                    branch.name,
                    type(exc).__name__,
                )
                summary = None
        else:
            summary = None

        rows.append(
            {
                "name": branch.name,
                "age_days": _age_days(now_utc, branch.last_commit_at),
                "diff_summary": summary,
            }
        )

    # ---------- Audit (success) ---------------------------------------------
    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            actor_id=actor_ctx.actor_id,
            actor_role=actor_ctx.actor_role,
            dept_id=dept_id,
            action=_AUDIT_ACTION_ORPHAN_LISTED,
            resource=f"orphan_branches:{dept_id}",
            result="ok",
            timestamp=now_utc,
            payload={
                "endpoint": "GET /api/orphan-branches",
                "branch_count": len(rows),
            },
        ),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"branches": rows},
    )


@router.get(
    "/po-review-inbox",
    status_code=status.HTTP_200_OK,
)
async def list_po_review_inbox(
    request: Request,
    dept_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Return the dept's draft PRs authored by a known bot account.

    The endpoint:

    1. Authenticates and authorises the caller (``viewer`` role
       suffices; ``dept_admin`` must match ``dept_id``).
    2. Lists PRs for the dept via the injected MCP scanner.
    3. Resolves the dept's bot ``account_id`` set.
    4. Runs the pure :func:`compute_po_review_inbox` helper to
       isolate draft PRs whose author is a known bot.
    5. Returns the inbox as JSON.
    """

    deps = _deps(request)
    actor_ctx = await _authorize(
        deps,
        authorization=authorization,
        dept_id=dept_id,
        required_role="viewer",
        resource=f"po_review_inbox:{dept_id}",
        audit_payload_extra={
            "endpoint": "GET /api/po-review-inbox",
        },
    )

    try:
        raw_prs = await deps.pr_scanner(dept_id)
        bot_ids = await deps.bot_account_ids(dept_id)
    except Exception as exc:  # noqa: BLE001 - translate to 502
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_ctx.actor_id,
                actor_role=actor_ctx.actor_role,
                dept_id=dept_id,
                action=_AUDIT_ACTION_SCAN_FAILED,
                resource=f"po_review_inbox:{dept_id}",
                result="error",
                timestamp=_now(deps),
                payload={
                    "endpoint": "GET /api/po-review-inbox",
                    "reason": type(exc).__name__,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"bitbucket scan failed: {type(exc).__name__}",
        ) from exc

    prs = _project_pull_requests(raw_prs)
    inbox = compute_po_review_inbox(prs, bot_ids)

    # Sort by id (ascending) so the response is stable across calls;
    # the Streamlit page renders a small list and the natural-key
    # ordering is the most predictable.
    sorted_inbox = sorted(inbox, key=lambda pr: pr.id)
    rows = [
        {
            "id": pr.id,
            "source_branch": pr.source_branch,
            "is_draft": pr.is_draft,
            "author_account_id": pr.author_account_id,
            "title": pr.title,
        }
        for pr in sorted_inbox
    ]

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            actor_id=actor_ctx.actor_id,
            actor_role=actor_ctx.actor_role,
            dept_id=dept_id,
            action=_AUDIT_ACTION_INBOX_LISTED,
            resource=f"po_review_inbox:{dept_id}",
            result="ok",
            timestamp=_now(deps),
            payload={
                "endpoint": "GET /api/po-review-inbox",
                "pr_count": len(rows),
            },
        ),
    )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"pull_requests": rows},
    )


# ---------------------------------------------------------------------------
# POST action endpoints - open-draft / request-changes / approve-note
# ---------------------------------------------------------------------------


async def _run_pr_action(
    deps: PoReviewEndpointDeps,
    *,
    pr_id: int,
    dept_id: str,
    actor_ctx: AuthContext,
    audit_action: str,
    resource: str,
    invoke: Callable[[], Awaitable[None]],
    audit_payload: dict[str, Any] | None = None,
) -> None:
    """Run a single Bitbucket-side action behind the audit envelope.

    The three action endpoints share the same surrounding audit
    plumbing - only the actual MCP call differs. Factoring the
    plumbing out keeps the per-endpoint bodies small and ensures
    every action emits exactly one audit row regardless of outcome.
    """

    try:
        await invoke()
    except Exception as exc:  # noqa: BLE001 - translate to 502
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_ctx.actor_id,
                actor_role=actor_ctx.actor_role,
                dept_id=dept_id,
                action=audit_action,
                resource=resource,
                result="error",
                timestamp=_now(deps),
                payload={
                    **(audit_payload or {}),
                    "reason": type(exc).__name__,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"bitbucket action failed: {type(exc).__name__}",
        ) from exc

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            actor_id=actor_ctx.actor_id,
            actor_role=actor_ctx.actor_role,
            dept_id=dept_id,
            action=audit_action,
            resource=resource,
            result="ok",
            timestamp=_now(deps),
            payload=audit_payload,
        ),
    )


@router.post(
    "/po-review-inbox/{pr_id}/open-draft",
    status_code=status.HTTP_202_ACCEPTED,
)
async def open_draft(
    pr_id: int,
    request: Request,
    dept_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Flip a draft PR back to open so the bot can re-iterate.

    Required role: ``lead`` because PO actions are above viewer.
    ``dept_admin`` must match ``dept_id`` (dept-scope
    enforcement); ``admin`` passes globally.
    """

    deps = _deps(request)
    actor_ctx = await _authorize(
        deps,
        authorization=authorization,
        dept_id=dept_id,
        required_role="lead",
        resource=f"po_review_pr:{pr_id}",
        audit_payload_extra={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/open-draft",
            "pr_id": pr_id,
        },
    )

    await _run_pr_action(
        deps,
        pr_id=pr_id,
        dept_id=dept_id,
        actor_ctx=actor_ctx,
        audit_action=_AUDIT_ACTION_OPEN_DRAFT,
        resource=f"po_review_pr:{pr_id}",
        invoke=lambda: deps.actions.open_draft(dept_id, pr_id),
        audit_payload={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/open-draft",
            "pr_id": pr_id,
        },
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={"pr_id": pr_id, "action": "open_draft", "applied": True},
    )


@router.post(
    "/po-review-inbox/{pr_id}/request-changes",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_changes(
    pr_id: int,
    request: Request,
    dept_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Post a request-changes review on a draft bot PR.

    Required role: ``lead``. The PO leaves a structured comment
    asking the bot for revisions; the bot picks the comment up via
    its normal PR comment webhook and starts the next iteration.

    The request body is parsed for an optional ``"comment"`` field;
    when omitted the endpoint falls back to a canned message
    ("`` Lütfen revize edin.``") so the action can be invoked from
    a Streamlit button without forcing the operator to type a
    message every time.
    """

    deps = _deps(request)
    actor_ctx = await _authorize(
        deps,
        authorization=authorization,
        dept_id=dept_id,
        required_role="lead",
        resource=f"po_review_pr:{pr_id}",
        audit_payload_extra={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/request-changes",
            "pr_id": pr_id,
        },
    )

    body = await _read_optional_json_body(request)
    comment = _coerce_comment(
        body.get("comment") if body else None,
        default=" Lütfen revize edin.",
    )

    await _run_pr_action(
        deps,
        pr_id=pr_id,
        dept_id=dept_id,
        actor_ctx=actor_ctx,
        audit_action=_AUDIT_ACTION_REQUEST_CHANGES,
        resource=f"po_review_pr:{pr_id}",
        invoke=lambda: deps.actions.request_changes(
            dept_id, pr_id, comment=comment
        ),
        audit_payload={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/request-changes",
            "pr_id": pr_id,
            # Record only the length of the comment so audit rows
            # never embed the full PO message - keeps the audit
            # table small and avoids accidental PII echo.
            "comment_length": len(comment),
        },
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "pr_id": pr_id,
            "action": "request_changes",
            "applied": True,
        },
    )


@router.post(
    "/po-review-inbox/{pr_id}/approve-note",
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_note(
    pr_id: int,
    request: Request,
    dept_id: str = Query(..., min_length=1),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Post an approve **note** on a draft bot PR.

    Required role: ``lead``. The note is an inline comment, *not* a
    Bitbucket approval - the platform never marks a bot-authored
    PR as approved on the human's behalf. The note signals intent so the workflow can
    advance without the human relinquishing the merge decision.
    """

    deps = _deps(request)
    actor_ctx = await _authorize(
        deps,
        authorization=authorization,
        dept_id=dept_id,
        required_role="lead",
        resource=f"po_review_pr:{pr_id}",
        audit_payload_extra={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/approve-note",
            "pr_id": pr_id,
        },
    )

    body = await _read_optional_json_body(request)
    comment = _coerce_comment(
        body.get("comment") if body else None,
        default=" PO onay notu: bu yön doğru görünüyor.",
    )

    await _run_pr_action(
        deps,
        pr_id=pr_id,
        dept_id=dept_id,
        actor_ctx=actor_ctx,
        audit_action=_AUDIT_ACTION_APPROVE_NOTE,
        resource=f"po_review_pr:{pr_id}",
        invoke=lambda: deps.actions.approve_note(
            dept_id, pr_id, comment=comment
        ),
        audit_payload={
            "endpoint": "POST /api/po-review-inbox/{pr_id}/approve-note",
            "pr_id": pr_id,
            "comment_length": len(comment),
        },
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "pr_id": pr_id,
            "action": "approve_note",
            "applied": True,
        },
    )


# ---------------------------------------------------------------------------
# Body-parsing helpers - accept JSON, but tolerate empty / non-JSON body
# ---------------------------------------------------------------------------


async def _read_optional_json_body(request: Request) -> dict[str, Any] | None:
    """Return the parsed JSON body or ``None`` when absent / invalid.

    The action endpoints accept an optional ``{"comment": "..."}``
    body so Streamlit buttons can fire them without a payload. We
    swallow parse errors quietly because the canned fallback message
    is always usable and a malformed body is unlikely to be
    intentional from a UI client.
    """

    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001 - defensive; treat as "no body"
        return None
    if not raw:
        return None
    try:
        import json

        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def _coerce_comment(raw: Any, *, default: str) -> str:
    """Return ``raw`` as a stripped non-empty string, else ``default``.

    Trims to a sane upper bound so a malicious caller cannot stuff
    100 KB into a single audit row. The bound (4 KB) is generous
    relative to real PO comments but tight enough to prevent abuse.
    """

    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped:
            return stripped[:4096]
    return default
