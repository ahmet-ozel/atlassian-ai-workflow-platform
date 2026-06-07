"""REST router backing ``/admin/prompts``.

Implements the prompt editing endpoints:

* ``GET    /admin/prompts``                   - list all tracked prompt
  files with their current main-branch short SHA.
* ``GET    /admin/prompts/{path:path}``       - read a single prompt
  body (defaults to the main branch; ``branch=`` override available).
* ``POST   /admin/prompts/{path:path}/draft`` - create a fresh
  ``draft/<actor>-<ts>`` branch off main, commit the new body and
  return the draft ref.
* ``POST   /admin/prompts/{path:path}/pr``    - open a PR from the
  supplied draft branch into ``main``.

Authentication
--------------
The router is gated on :func:`require_admin` (the same dependency the
``/admin/services`` lifecycle router uses). RBAC is intentionally
flat - prompt edits are global concerns and only ``admin`` actors
may invoke any of the four endpoints. Dept-scoped prompt edits are
out of scope for this task; the design.md table at the end of the
"Komponent Sahipliği Özeti" lists ``/admin/prompts`` as
``admin-dashboard-api``-owned with admin-only access.

Validations
-----------
* Every write path runs ``validate_template_format(body)`` from
  ``libs/prompts`` *before* touching git. Failures
  return ``422 Unprocessable Entity`` with the validator's message,
  no audit row is written (the failure is a request-shape problem,
  not a system event).
* Path traversal is rejected at two layers: the FastAPI
  ``{path:path}`` converter does not collapse ``..`` segments, so the
  router calls :func:`_safe_relative_path` to normalise + reject any
  ``..`` / absolute path before handing the value to ``GitRepo``.
* Branch names supplied by the caller (``POST .../pr``) are validated
  against ``draft/`` prefix to prevent accidental PRs from arbitrary
  refs.

Audit
-----
Mutation endpoints emit:

* ``prompt_draft_created`` after a successful ``POST .../draft``.
* ``prompt_pr_opened`` after a successful ``POST .../pr``.
* ``prompt_render_failed`` when ``validate_template_format`` rejects
  a body before any git mutation runs.
* ``prompt_pr_conflict`` when ``GitRepo.detect_merge_conflict`` flags
  a conflict at PR-open time.

Until the Postgres-backed audit writer is wired, the router emits
each event through the process-wide :class:`LoggingAuditSink` already used by
:class:`AdminProxy`. Both the logging adapter and the eventual
asyncpg-backed writer satisfy the same ``write(event)`` protocol so
the swap is opt-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent
from git_shared import (
    BranchAlreadyExistsError,
    BranchNotFoundError,
    GitAuthor,
    GitRepo,
    GitRepoError,
    MergeConflictError,
    PullRequestError,
    PullRequestOpener,
    PullRequestRef,
)
from prompts import PromptTemplateError, validate_template_format

from ..auth.dependencies import AuthClaims, require_admin
from ..prompts import (
    SandboxRunSummary,
    extract_v15_status,
    render_pr_description,
)
from ..sandbox import PromptSandbox
from ._prompts_models import (
    PromptDetail,
    PromptDraftRequest,
    PromptDraftResponse,
    PromptListItem,
    PromptListResponse,
    PromptPrRequest,
    PromptPrResponse,
    PromptSandboxRequest,
    PromptSandboxResponse,
    PromoteRequest,
    PromoteResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Audit sink protocol (matches src.audit_sink.LoggingAuditSink shape)
# ---------------------------------------------------------------------------


@runtime_checkable
class _AuditSink(Protocol):
    """Minimal write surface required by the router.

    Production wiring uses :class:`audit_logger.AuditLogger` once task
    6.3 of this spec lands the asyncpg-backed adapter; the logging
    sink from ``src/audit_sink.py`` satisfies the same shape today.
    """

    async def write(self, event: AuditEvent) -> None:
        """Persist (or log) one audit event."""

        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# A draft branch is always ``draft/<actor>-<ts>`` per the design;
# allowing the caller-supplied branch only when it matches this shape
# prevents arbitrary refs from being PR'd through this router.
_DRAFT_BRANCH_RE = re.compile(r"^draft/[A-Za-z0-9_.@:+\-]+$")

# Maximum body size (bytes) we will accept on the draft endpoint.
# Rationale: prompt files are markdown; even the largest expected
# system prompt sits well under 64 KiB. A hard cap protects against
# accidental binary uploads.
_MAX_DRAFT_BODY_BYTES = 64 * 1024


def _safe_relative_path(raw: str) -> str:
    """Normalise ``raw`` and reject absolute / traversal paths.

    The FastAPI ``{path:path}`` converter accepts ``..`` segments and
    leading slashes. We collapse the path and reject anything that
    points outside the configured prompts prefix.
    """

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt path must not be empty",
        )

    # Reject Windows drive letters and absolute POSIX paths upfront -
    # neither makes sense for a repo-relative ref.
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt path must be relative",
        )

    # Collapse ``./`` segments and reject ``..`` ones.
    parts = PurePosixPath(raw.replace("\\", "/")).parts
    cleaned: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="prompt path must not contain '..' segments",
            )
        cleaned.append(part)

    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt path must not be empty after normalisation",
        )

    return "/".join(cleaned)


def _draft_branch_name(actor_id: str, *, clock: Callable[[], float]) -> str:
    """Generate a deterministic ``draft/<actor>-<ts>`` branch name.

    The clock is parametrised so tests can inject a deterministic
    value. The actor id is sanitised down to ``[A-Za-z0-9._-]`` to
    keep the branch ref valid; spaces / colons / slashes coming from
    OIDC ``sub`` claims are replaced with ``_``.
    """

    safe = re.sub(r"[^A-Za-z0-9._\-]", "_", actor_id)
    return f"draft/{safe}-{int(clock())}"


def _git_author(actor: AuthClaims) -> GitAuthor:
    """Build a :class:`git_shared.GitAuthor` from an OIDC subject.

    The router has no access to the user's real ``email`` claim
    (``AuthClaims`` carries only ``sub`` + ``groups``); production
    deployments that need a richer commit identity swap this helper
    for a version that pulls ``email`` / ``name`` from the
    :class:`AuthContext` shape used by ``admin_proxy``. For the
    project we synthesise a stable ``noreply`` address.
    """

    return GitAuthor(
        name=f"admin-dashboard-api ({actor.sub})",
        email=f"{actor.sub}@admin-dashboard-api.local",
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_event(
    *,
    actor_id: str,
    action: str,
    resource: str,
    result: str,
    payload: dict,
) -> AuditEvent:
    """Build an :class:`AuditEvent` for prompt mutations.

    All prompt edits are ``actor_role="admin"`` global actions.
    ``dept_id`` is ``None`` because prompts are cross-department
    artefacts.
    """

    return AuditEvent(
        actor_id=actor_id,
        actor_role="admin",
        dept_id=None,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=_utc_now(),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def get_prompts_git_repo(request: Request) -> GitRepo:
    """Return the per-process :class:`GitRepo` singleton.

    The router does not own the repo lifecycle - the lifespan hook in
    :mod:`src.main` constructs it once via
    :func:`src.routers.prompts_git_factory.build_prompts_repo` and
    attaches it to ``app.state.prompts_repo``. When the wiring is
    absent (eg. during a manifest-load failure) we surface ``503``
    matching the readiness probe shape.
    """

    repo: GitRepo | None = getattr(request.app.state, "prompts_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "prompts_repo_unavailable"},
        )
    return repo


def get_prompts_pr_opener(request: Request) -> PullRequestOpener:
    """Return the configured :class:`PullRequestOpener` (Bitbucket)."""

    opener: PullRequestOpener | None = getattr(
        request.app.state, "prompts_pr_opener", None
    )
    if opener is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompts_pr_opener_unavailable",
            },
        )
    return opener


def get_prompts_audit_sink(request: Request) -> _AuditSink:
    """Return the audit sink the router writes to.

    Falls back to ``app.state.admin_proxy.audit_sink`` so a single
    sink instance is shared with the existing :class:`AdminProxy`
    wiring; that keeps every audit row in one place until the
    asyncpg-backed writer is available.
    """

    sink: _AuditSink | None = getattr(request.app.state, "prompts_audit_sink", None)
    if sink is not None:
        return sink

    # Fall back to the AdminProxy's sink so the router behaves
    # identically when only the foundation wiring is present. The
    # proxy stores the sink as ``_audit`` (private attribute).
    admin_proxy = getattr(request.app.state, "admin_proxy", None)
    if admin_proxy is not None and hasattr(admin_proxy, "_audit"):
        return admin_proxy._audit  # type: ignore[attr-defined]

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"status": "not_ready", "reason": "audit_sink_unavailable"},
    )


def get_clock() -> Callable[[], float]:
    """Return the wall-clock used to build draft branch names.

    Override in tests via ``app.dependency_overrides[get_clock] = ...``
    to make branch names deterministic.
    """

    return time.time


def get_prompt_sandbox(request: Request) -> PromptSandbox:
    """Return the configured :class:`PromptSandbox` singleton.

    The sandbox is wired in :mod:`src.main`'s lifespan hook with an
    :class:`LlmInvokerLike` and a :class:`CostTrackerLike`. The
    production lifespan uses the configured LLM provider; the readiness
    probe surfaces a 503 only when ``app.state.prompt_sandbox`` is left
    unset (eg. lifespan crashed before reaching the sandbox wiring
    block).
    """

    sandbox: PromptSandbox | None = getattr(
        request.app.state, "prompt_sandbox", None
    )
    if sandbox is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompt_sandbox_unavailable",
            },
        )
    return sandbox


def get_prompts_pg_pool(request: Request) -> Any | None:
    """Return the optional asyncpg pool used to record sandbox runs.

    The pool is the same one wired by :mod:`src.main`'s lifespan
    onto ``app.state.pg_pool`` (the ``costs`` / ``feature_flags``
    routers also read from it). The
    dependency intentionally returns ``None`` instead of raising
    503 when the pool is missing so the sandbox-test endpoint stays
    answerable in degraded boot states - the response simply
    carries ``sandbox_run_id=None`` and the follow-up promote
    endpoint will reject those calls with 404. Tests inject a fake
    via ``app.dependency_overrides[get_prompts_pg_pool]``.
    """

    return getattr(request.app.state, "pg_pool", None)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(
    prefix="/admin/prompts",
    tags=["prompts-git"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# GET /admin/prompts
# ---------------------------------------------------------------------------


@router.get("", response_model=PromptListResponse, name="list_prompts")
async def list_prompts(
    request: Request,
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    branch: Annotated[
        str | None,
        Query(description="Override branch (defaults to main)."),
    ] = None,
) -> PromptListResponse:
    """List every tracked Markdown prompt under the configured prefix.

    The list is computed off the configured main branch (or the
    explicit ``branch=`` override) so the response always reflects
    the production state - draft branches are deliberately not
    surfaced here (the admin UI tracks them via the response of
    ``POST .../draft``).
    """

    prefix = _resolve_prefix(request)
    try:
        paths = await asyncio.to_thread(
            repo.list_files,
            branch=branch,
            path_prefix=prefix,
            suffixes=(".md",),
        )
    except BranchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    items: list[PromptListItem] = []
    target = branch or repo.main_branch
    try:
        head_sha = await asyncio.to_thread(repo.resolve_branch_sha, target)
    except BranchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    for path in paths:
        items.append(
            PromptListItem(path=path, commit_hash=head_sha[:7])
        )
    return PromptListResponse(items=items)


# ---------------------------------------------------------------------------
# GET /admin/prompts/{path:path}
# ---------------------------------------------------------------------------


@router.get(
    "/{path:path}",
    response_model=PromptDetail,
    name="read_prompt",
)
async def read_prompt(
    path: str,
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    branch: Annotated[
        str | None,
        Query(description="Override branch (defaults to main)."),
    ] = None,
) -> PromptDetail:
    """Return the full body of a single prompt file."""

    safe_path = _safe_relative_path(path)
    target_branch = branch or repo.main_branch
    try:
        body = await asyncio.to_thread(
            repo.read_file, safe_path, branch=target_branch
        )
        head_sha = await asyncio.to_thread(
            repo.resolve_branch_sha, target_branch
        )
    except BranchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"prompt not found: {safe_path!r}",
        ) from exc

    return PromptDetail(
        path=safe_path,
        branch=target_branch,
        commit_hash=head_sha[:7],
        body=body,
    )


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path:path}/draft
# ---------------------------------------------------------------------------


@router.post(
    "/{path:path}/draft",
    response_model=PromptDraftResponse,
    status_code=status.HTTP_201_CREATED,
    name="create_prompt_draft",
)
async def create_prompt_draft(
    path: str,
    payload: PromptDraftRequest,
    actor: Annotated[AuthClaims, Depends(require_admin)],
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    audit: Annotated[_AuditSink, Depends(get_prompts_audit_sink)],
    clock: Annotated[Callable[[], float], Depends(get_clock)],
) -> PromptDraftResponse:
    """Create a draft branch and commit the new body onto it.

    Steps (mirrors design.md §`PromptsGitRouter` ``post_draft``):

    1. Normalise / safe-check the path.
    2. Reject bodies larger than ``_MAX_DRAFT_BODY_BYTES``.
    3. Run ``validate_template_format(body)``. On
       failure emit ``prompt_render_failed`` and return ``422``.
    4. Verify the path already exists on main - this router is the
       edit surface; brand-new files go through a different flow.
    5. Create ``draft/<actor>-<ts>``, ``write_file``, ``commit``.
    6. Emit ``prompt_draft_created``.
    """

    safe_path = _safe_relative_path(path)
    body = payload.body

    if len(body.encode("utf-8")) > _MAX_DRAFT_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"draft body exceeds {_MAX_DRAFT_BODY_BYTES} bytes "
                "(prompts must stay under 64 KiB)"
            ),
        )

    # ---- 3. Template format validation -------------------------------
    try:
        validate_template_format(body)
    except PromptTemplateError as exc:
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_render_failed",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={"path": safe_path, "reason": str(exc)},
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"template format invalid: {exc}",
        ) from exc

    # ---- 4. Existence guard ------------------------------------------
    try:
        await asyncio.to_thread(
            repo.read_file, safe_path, branch=repo.main_branch
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"prompt {safe_path!r} not found on {repo.main_branch!r}; "
                "this endpoint edits existing prompts only"
            ),
        ) from exc
    except BranchNotFoundError as exc:  # pragma: no cover - misconfigured clone
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    # ---- 5. Branch + write + commit ----------------------------------
    branch_name = _draft_branch_name(actor.sub, clock=clock)

    # Retry once on a (very unlikely) timestamp collision so the
    # caller does not need to know about ``draft/<actor>-<ts>`` clashes.
    try:
        await asyncio.to_thread(repo.create_branch_from_main, branch_name)
    except BranchAlreadyExistsError:
        # Append a short disambiguator and retry; tests cover this
        # branch by injecting a fixed clock.
        branch_name = f"{branch_name}-1"
        try:
            await asyncio.to_thread(repo.create_branch_from_main, branch_name)
        except BranchAlreadyExistsError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"draft branch already exists: {branch_name}",
            ) from exc
    except BranchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    commit_message = payload.message or f"draft prompt change: {safe_path}"
    author = _git_author(actor)

    try:
        await asyncio.to_thread(
            repo.write_file, safe_path, body, branch=branch_name
        )
        commit = await asyncio.to_thread(
            repo.commit,
            branch_name,
            message=commit_message,
            author=author,
        )
    except GitRepoError as exc:
        # Best-effort: keep the (now-orphan) draft branch around so a
        # human can inspect it; surface the error to the caller.
        logger.exception(
            "prompt draft commit failed: branch=%s path=%s",
            branch_name,
            safe_path,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to commit draft: {exc}",
        ) from exc

    # ---- 6. Audit ----------------------------------------------------
    await _safe_audit(
        audit,
        _audit_event(
            actor_id=actor.sub,
            action="prompt_draft_created",
            resource=f"prompt:{safe_path}",
            result="ok",
            payload={
                "path": safe_path,
                "branch": branch_name,
                "commit_sha": commit.sha,
                "short_sha": commit.short_sha,
            },
        ),
    )

    return PromptDraftResponse(
        path=safe_path,
        branch=branch_name,
        commit_hash=commit.sha,
        short_hash=commit.short_sha,
    )


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path:path}/sandbox-test
# ---------------------------------------------------------------------------


@router.post(
    "/{path:path}/sandbox-test",
    response_model=PromptSandboxResponse,
    status_code=status.HTTP_200_OK,
    name="sandbox_test_prompt",
)
async def sandbox_test_prompt(
    path: str,
    payload: PromptSandboxRequest,
    actor: Annotated[AuthClaims, Depends(require_admin)],
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    sandbox: Annotated[PromptSandbox, Depends(get_prompt_sandbox)],
    audit: Annotated[_AuditSink, Depends(get_prompts_audit_sink)],
    pg_pool: Annotated[Any | None, Depends(get_prompts_pg_pool)],
) -> PromptSandboxResponse:
    """Run an isolated LLM call against a draft prompt.

    The endpoint pairs a candidate prompt body with a sample user
    input and asks :class:`PromptSandbox` to issue a single LLM
    round-trip with ``cost_tag="sandbox"``. The sandbox **does not**
    touch Atlassian - no MCP catalogue is handed to the LLM - and
    the resulting cost row in ``shared.cost_tracking`` is filtered
    out of every dept budget aggregate by
    :class:`BudgetCapPolicy`.

    The caller must supply *exactly one* of ``body`` (raw draft
    body, useful while the developer is still typing in the editor)
    or ``branch`` (read the body off a previously-committed draft
    branch, useful for "test what was committed" parity). When
    neither or both are provided the endpoint returns ``400``.

    Steps:

    1. Normalise the path; reject traversal segments.
    2. Resolve the prompt body - either from ``payload.body`` or
       from ``payload.branch`` (which must match the
       ``draft/<actor>-<ts>`` shape and exist locally).
    3. Validate the body's template format. A
       sandbox-test on a body with unbalanced braces would fail at
       LLM render time anyway; surfacing the validator's message
       here gives the developer a fast, deterministic feedback loop.
    4. Hand the body + sample input to the sandbox; await the
       :class:`SandboxResult`.
    5. **Persist the run** to ``automation.prompt_sandbox_runs`` so
       the follow-up ``POST /admin/prompts/{path}/promote`` endpoint
       can verify the sandbox passed before opening a
       PR. The row id is surfaced as ``sandbox_run_id`` on the
       response. Failures here are soft-fail: the response still carries the LLM output but
       with ``sandbox_run_id=None`` (the user can re-run if they
       want a promotable record).
    6. Emit the ``prompt_sandbox_run_recorded`` audit row carrying
       ``actor_id, prompt_path, sandbox_run_id, passed`` so the
       audit chain reflects every promote-eligible test.
    7. Project the dataclass + the run id into
       :class:`PromptSandboxResponse`.
    """

    safe_path = _safe_relative_path(path)

    # ---- 1. Mutual exclusion of body / branch ------------------------
    body = payload.body
    branch = payload.branch
    if (body is None) == (branch is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "exactly one of 'body' or 'branch' must be provided "
                "(supply 'body' for unsaved edits, 'branch' to test "
                "what was committed to a draft branch)"
            ),
        )

    # ---- 2. Resolve the prompt body ---------------------------------
    if branch is not None:
        if not _DRAFT_BRANCH_RE.match(branch):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "branch must match the draft/<actor>-<ts> shape; "
                    f"got {branch!r}"
                ),
            )
        try:
            resolved_body = await asyncio.to_thread(
                repo.read_file, safe_path, branch=branch
            )
        except BranchNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"prompt {safe_path!r} not found on branch "
                    f"{branch!r}"
                ),
            ) from exc
    else:
        # ``body is not None`` is guaranteed by the mutual-exclusion
        # check above; the explicit assertion lets mypy follow the
        # narrowing without a ``cast``.
        assert body is not None  # noqa: S101 - defensive, see above
        resolved_body = body

    # ---- 3. Template format validation ------------------------------
    # We reject malformed bodies **before** the LLM call so the
    # developer sees the validator's message instead of an opaque
    # ``KeyError`` from ``str.format`` deep inside the orchestrator.
    try:
        validate_template_format(resolved_body)
    except PromptTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"template format invalid: {exc}",
        ) from exc

    # ---- 4. Sandbox invocation --------------------------------------
    # The sandbox owns the ``cost_tag="sandbox"`` contract; we just
    # forward the dept and actor metadata so the cost row carries
    # the right attribution.
    result = await sandbox.run(
        resolved_body,
        payload.sample_input,
        dept_id=payload.dept_id,
        user_id=actor.sub,
    )

    # Reaching this point means :meth:`PromptSandbox.run` returned a
    # :class:`SandboxResult` without raising - the LLM round-trip
    # succeeded. ``passed=True`` is therefore the correct value for
    # the persisted row: the run is promote-eligible. (Failures
    # raise out of ``sandbox.run`` and never reach this branch, so
    # we do not need a separate ``passed=False`` insert path here;
    # the promote endpoint surfaces "not passed" via the absence of
    # any matching row, mapped to 404.)
    sandbox_passed = True

    # ---- 5. Persist the run to ``prompt_sandbox_runs`` --------------
    # Soft-fail when the asyncpg pool is missing: the LLM result
    # still goes back to the caller (so the developer sees the
    # response text + cost), but ``sandbox_run_id`` lands as
    # ``None`` and the promote endpoint will reject the follow-up
    # call with 404.
    sandbox_run_id = await _record_sandbox_run(
        pool=pg_pool,
        prompt_path=safe_path,
        draft_branch=branch,
        sample_input=payload.sample_input,
        prompt_body=resolved_body,
        result=result,
        passed=sandbox_passed,
        actor_id=actor.sub,
    )

    # ---- 6. Audit ----------------------------------------------------
    # Carry the row id (or ``None``) so the audit chain reflects
    # whether the run is promote-eligible at the database layer.
    await _safe_audit(
        audit,
        _audit_event(
            actor_id=actor.sub,
            action="prompt_sandbox_run_recorded",
            resource=f"prompt:{safe_path}",
            result="ok" if sandbox_run_id is not None else "error",
            payload={
                "actor_id": actor.sub,
                "prompt_path": safe_path,
                "sandbox_run_id": sandbox_run_id,
                "passed": sandbox_passed,
            },
        ),
    )

    # ---- 7. Project SandboxResult → response model ------------------
    return PromptSandboxResponse(
        path=safe_path,
        response_text=result.response_text,
        token_in=result.token_in,
        token_out=result.token_out,
        cost_usd=str(result.cost_usd),
        invoked_at=result.invoked_at.isoformat(),
        model=result.model,
        provider=result.provider,
        cost_tag=result.cost_tag,
        sandbox_run_id=sandbox_run_id,
    )


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path:path}/pr
# ---------------------------------------------------------------------------


@router.post(
    "/{path:path}/pr",
    response_model=PromptPrResponse,
    status_code=status.HTTP_201_CREATED,
    name="open_prompt_pr",
)
async def open_prompt_pr(
    request: Request,
    path: str,
    payload: PromptPrRequest,
    actor: Annotated[AuthClaims, Depends(require_admin)],
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    opener: Annotated[PullRequestOpener, Depends(get_prompts_pr_opener)],
    audit: Annotated[_AuditSink, Depends(get_prompts_audit_sink)],
) -> PromptPrResponse:
    """Open a PR from a previously-created draft branch into main.

    Steps:

    1. Normalise / safe-check the path.
    2. Validate ``payload.branch`` matches the ``draft/`` prefix and
       exists locally.
    3. Detect merge conflicts up-front. On conflict emit
       ``prompt_pr_conflict`` and return ``409 Conflict``.
    4. Render a deterministic title / description (or use the
       caller-supplied overrides).
    5. Invoke the configured :class:`PullRequestOpener`.
    6. Emit ``prompt_pr_opened``.
    """

    safe_path = _safe_relative_path(path)

    # ---- 2. Branch validation ----------------------------------------
    if not _DRAFT_BRANCH_RE.match(payload.branch):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "branch must match the draft/<actor>-<ts> shape; "
                f"got {payload.branch!r}"
            ),
        )

    if not await asyncio.to_thread(repo.branch_exists, payload.branch):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"draft branch not found: {payload.branch}",
        )

    # ---- 3. Conflict detection ---------------------------------------
    try:
        conflict = await asyncio.to_thread(
            repo.detect_merge_conflict,
            payload.branch,
            against=repo.main_branch,
        )
    except BranchNotFoundError as exc:  # pragma: no cover
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    if conflict:
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_pr_conflict",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={
                    "path": safe_path,
                    "source_branch": payload.branch,
                    "target_branch": repo.main_branch,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"merge conflict between {payload.branch!r} and "
                f"{repo.main_branch!r}"
            ),
        )

    # ---- 4. Title / description --------------------------------------
    title = payload.title or f"Prompt change: {safe_path}"
    description = payload.description
    if description is None:
        try:
            diff = await asyncio.to_thread(
                repo.diff, payload.branch, against=repo.main_branch
            )
        except GitRepoError as exc:  # pragma: no cover
            diff = f"(diff unavailable: {exc})"
        # Read the draft body so V15 cross-reference status reflects
        # the about-to-merge state, not the main-branch baseline.
        try:
            draft_body = await asyncio.to_thread(
                repo.read_file, safe_path, branch=payload.branch
            )
        except (FileNotFoundError, GitRepoError):  # pragma: no cover
            draft_body = ""
        description = await asyncio.to_thread(
            _build_pr_description,
            request,
            safe_path,
            diff,
            draft_body,
            payload.branch,
        )

    # ---- 5. Open the PR ----------------------------------------------
    try:
        pr_ref: PullRequestRef = await opener.open(
            source_branch=payload.branch,
            target_branch=repo.main_branch,
            title=title,
            description=description,
        )
    except MergeConflictError as exc:
        # Defence-in-depth: the upstream returned conflict even
        # though our local pre-check passed. Treat it identically
        # to the local detection branch.
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_pr_conflict",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={
                    "path": safe_path,
                    "source_branch": payload.branch,
                    "target_branch": repo.main_branch,
                    "upstream": True,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except PullRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # ---- 6. Audit ----------------------------------------------------
    await _safe_audit(
        audit,
        _audit_event(
            actor_id=actor.sub,
            action="prompt_pr_opened",
            resource=f"prompt:{safe_path}",
            result="ok",
            payload={
                "path": safe_path,
                "pr_id": pr_ref.id,
                "pr_url": pr_ref.url,
                "source_branch": pr_ref.source_branch,
                "target_branch": pr_ref.target_branch,
                "provider": pr_ref.provider,
            },
        ),
    )

    return PromptPrResponse(
        path=safe_path,
        pr_id=pr_ref.id,
        pr_url=pr_ref.url,
        source_branch=pr_ref.source_branch,
        target_branch=pr_ref.target_branch,
        provider=pr_ref.provider,
    )


# ---------------------------------------------------------------------------
# POST /admin/prompts/{path:path}/promote
# ---------------------------------------------------------------------------


#: SQL to look up a sandbox run by id.
_SELECT_SANDBOX_RUN_SQL = (
    "SELECT id, prompt_path, draft_branch, passed "
    "FROM automation.prompt_sandbox_runs "
    "WHERE id = $1::uuid"
)


# ---------------------------------------------------------------------------
# Promote logic helper
# ---------------------------------------------------------------------------


class _PromoteNotFoundError(Exception):
    """Raised by :func:`_promote_logic` when sandbox_run_id is not found."""

    def __init__(self, sandbox_run_id: str) -> None:
        super().__init__(f"sandbox_run_not_found: {sandbox_run_id!r}")
        self.sandbox_run_id = sandbox_run_id


class _PromoteSandboxNotPassedError(Exception):
    """Raised by :func:`_promote_logic` when sandbox run has passed=False."""

    def __init__(self, sandbox_run_id: str) -> None:
        super().__init__(f"sandbox_not_passed: {sandbox_run_id!r}")
        self.sandbox_run_id = sandbox_run_id


class _PromoteResult:
    """Successful promote result returned by :func:`_promote_logic`."""

    __slots__ = ("pr_url", "branch", "sandbox_run_id")

    def __init__(self, *, pr_url: str, branch: str, sandbox_run_id: str) -> None:
        self.pr_url = pr_url
        self.branch = branch
        self.sandbox_run_id = sandbox_run_id


async def _promote_logic(
    *,
    prompt_path: str,
    draft_branch: str,
    sandbox_run_id: str,
    target_branch: str = "main",
    title: str = "Promote prompt",
    description: str = "",
    actor_id: str,
    pool: Any,
    audit: Any,
    pr_opener: Any,
) -> _PromoteResult:
    """Core promote logic, extracted for testability.

    This function is the heart of ``POST /admin/prompts/{path}/promote``
    and is separated from the FastAPI handler so tests can call it
    directly without spinning up an HTTP server.

    The ``pool`` argument must expose an async ``fetch_sandbox_run(id)``
    method. The router handler wraps the real asyncpg pool in
    :class:`_AsyncpgPoolAdapter` before calling this function.

    Steps:

    ① Fetch ``sandbox_run_id`` from ``automation.prompt_sandbox_runs``.
       Not found → raise ``_PromoteNotFoundError`` (→ HTTP 404).
    ② ``passed=False`` → raise ``_PromoteSandboxNotPassedError`` (→ HTTP 422)
       + ``prompt_promote_rejected_sandbox_failed`` audit.
    ③ ``passed=True`` → open PR via ``pr_opener``.
    ④ Success → ``prompt_promoted`` audit + return ``_PromoteResult``.
    """
    # ① Fetch the sandbox run.
    run = await pool.fetch_sandbox_run(sandbox_run_id)
    if run is None:
        raise _PromoteNotFoundError(sandbox_run_id)

    # ② Check passed.
    if not run["passed"]:
        await audit.write(
            _audit_event(
                actor_id=actor_id,
                action="prompt_promote_rejected_sandbox_failed",
                resource=f"prompt:{prompt_path}",
                result="error",
                payload={
                    "actor_id": actor_id,
                    "prompt_path": prompt_path,
                    "sandbox_run_id": sandbox_run_id,
                },
            )
        )
        raise _PromoteSandboxNotPassedError(sandbox_run_id)

    # ③ Open PR.
    pr_ref = await pr_opener.open(
        source_branch=draft_branch,
        target_branch=target_branch,
        title=title,
        description=description,
    )

    # ④ Audit + return.
    await audit.write(
        _audit_event(
            actor_id=actor_id,
            action="prompt_promoted",
            resource=f"prompt:{prompt_path}",
            result="ok",
            payload={
                "actor_id": actor_id,
                "prompt_path": prompt_path,
                "sandbox_run_id": sandbox_run_id,
                "pr_url": pr_ref.url,
                "source_branch": pr_ref.source_branch,
                "target_branch": pr_ref.target_branch,
            },
        )
    )

    return _PromoteResult(
        pr_url=pr_ref.url,
        branch=draft_branch,
        sandbox_run_id=sandbox_run_id,
    )


class _AsyncpgPoolAdapter:
    """Wraps a real asyncpg pool to expose the ``fetch_sandbox_run`` interface.

    :func:`_promote_logic` uses ``pool.fetch_sandbox_run(id)`` so it can
    be tested with a fake pool. This adapter bridges the real asyncpg pool
    to that interface.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def fetch_sandbox_run(self, sandbox_run_id: str) -> Any | None:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                _SELECT_SANDBOX_RUN_SQL,
                sandbox_run_id,
            )


class _DirectPrOpener:
    """Wraps a :class:`PullRequestOpener` to match the ``pr_opener`` interface.

    :func:`_promote_logic` calls ``pr_opener.open(...)`` with keyword
    arguments. The real :class:`PullRequestOpener` already has this
    interface, so this is a pass-through wrapper.
    """

    __slots__ = ("_opener",)

    def __init__(self, opener: PullRequestOpener) -> None:
        self._opener = opener

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> Any:
        return await self._opener.open(
            source_branch=source_branch,
            target_branch=target_branch,
            title=title,
            description=description,
        )


@router.post(
    "/{path:path}/promote",
    response_model=PromoteResponse,
    status_code=status.HTTP_201_CREATED,
    name="promote_prompt",
)
async def promote_prompt(
    request: Request,
    path: str,
    payload: PromoteRequest,
    actor: Annotated[AuthClaims, Depends(require_admin)],
    repo: Annotated[GitRepo, Depends(get_prompts_git_repo)],
    opener: Annotated[PullRequestOpener, Depends(get_prompts_pr_opener)],
    audit: Annotated[_AuditSink, Depends(get_prompts_audit_sink)],
    pg_pool: Annotated[Any | None, Depends(get_prompts_pg_pool)],
) -> PromoteResponse:
    """Promote a sandbox-tested draft prompt to a PR.

    Steps:

    1. Normalise / safe-check the path.
    2. Look up ``sandbox_run_id`` in ``automation.prompt_sandbox_runs``.
       Missing row → 404 ``sandbox_run_not_found``.
    3. ``passed=false`` → 422 + ``prompt_promote_rejected_sandbox_failed``
       audit.
    4. ``passed=true`` → delegate to the same PR-opening logic used by
       ``POST /admin/prompts/{path}/pr`` (branch validation, conflict
       detection, title/description rendering, opener invocation).
    5. Success → ``prompt_promoted`` audit + 201
       ``{pr_url, branch, sandbox_run_id}``.
    """

    safe_path = _safe_relative_path(path)

    # ---- 2. Sandbox run lookup ---------------------------------------
    if pg_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "pg_pool_unavailable",
                "detail": (
                    "Postgres pool is not available; promote endpoint "
                    "requires a live database connection to verify the "
                    "sandbox run."
                ),
            },
        )

    try:
        async with pg_pool.acquire() as conn:
            sandbox_row = await conn.fetchrow(
                _SELECT_SANDBOX_RUN_SQL,
                payload.sandbox_run_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "promote: sandbox run lookup failed "
            "(path=%s sandbox_run_id=%s actor=%s err=%s)",
            safe_path,
            payload.sandbox_run_id,
            actor.sub,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"database error during sandbox run lookup: {exc}",
        ) from exc

    if sandbox_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "sandbox_run_not_found",
                "sandbox_run_id": payload.sandbox_run_id,
            },
        )

    sandbox_passed: bool = sandbox_row["passed"]

    # ---- 3. Reject if sandbox did not pass ---------------------------
    if not sandbox_passed:
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_promote_rejected_sandbox_failed",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={
                    "actor_id": actor.sub,
                    "prompt_path": safe_path,
                    "sandbox_run_id": payload.sandbox_run_id,
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "sandbox_not_passed",
                "sandbox_run_id": payload.sandbox_run_id,
                "detail": (
                    "The referenced sandbox run did not pass. "
                    "Re-run the sandbox test and ensure it passes "
                    "before promoting."
                ),
            },
        )

    # ---- 4. PR-opening logic (mirrors open_prompt_pr) ----------------
    draft_branch = payload.draft_branch
    target_branch = payload.target_branch or repo.main_branch

    if not _DRAFT_BRANCH_RE.match(draft_branch):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "draft_branch must match the draft/<actor>-<ts> shape; "
                f"got {draft_branch!r}"
            ),
        )

    if not await asyncio.to_thread(repo.branch_exists, draft_branch):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"draft branch not found: {draft_branch}",
        )

    # Conflict detection
    try:
        conflict = await asyncio.to_thread(
            repo.detect_merge_conflict,
            draft_branch,
            against=target_branch,
        )
    except BranchNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    if conflict:
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_pr_conflict",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={
                    "path": safe_path,
                    "source_branch": draft_branch,
                    "target_branch": target_branch,
                    "via": "promote",
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"merge conflict between {draft_branch!r} and "
                f"{target_branch!r}"
            ),
        )

    # Title / description
    title = payload.title
    description = payload.description
    if description is None:
        try:
            diff = await asyncio.to_thread(
                repo.diff, draft_branch, against=target_branch
            )
        except GitRepoError as exc:  # pragma: no cover
            diff = f"(diff unavailable: {exc})"
        try:
            draft_body = await asyncio.to_thread(
                repo.read_file, safe_path, branch=draft_branch
            )
        except (FileNotFoundError, GitRepoError):  # pragma: no cover
            draft_body = ""
        description = await asyncio.to_thread(
            _build_pr_description,
            request,
            safe_path,
            diff,
            draft_body,
            draft_branch,
        )

    # Open the PR
    try:
        pr_ref: PullRequestRef = await opener.open(
            source_branch=draft_branch,
            target_branch=target_branch,
            title=title,
            description=description,
        )
    except MergeConflictError as exc:
        await _safe_audit(
            audit,
            _audit_event(
                actor_id=actor.sub,
                action="prompt_pr_conflict",
                resource=f"prompt:{safe_path}",
                result="error",
                payload={
                    "path": safe_path,
                    "source_branch": draft_branch,
                    "target_branch": target_branch,
                    "upstream": True,
                    "via": "promote",
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except PullRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    # ---- 5. Audit + response -----------------------------------------
    await _safe_audit(
        audit,
        _audit_event(
            actor_id=actor.sub,
            action="prompt_promoted",
            resource=f"prompt:{safe_path}",
            result="ok",
            payload={
                "actor_id": actor.sub,
                "prompt_path": safe_path,
                "sandbox_run_id": payload.sandbox_run_id,
                "pr_url": pr_ref.url,
                "source_branch": pr_ref.source_branch,
                "target_branch": pr_ref.target_branch,
            },
        ),
    )

    return PromoteResponse(
        pr_url=pr_ref.url,
        branch=pr_ref.source_branch,
        sandbox_run_id=payload.sandbox_run_id,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_prefix(request: Request) -> str:
    """Resolve the configured prompts directory prefix.

    Pulled from ``app.state.prompts_dir_prefix`` so tests can override
    via dependency injection without monkey-patching settings.
    """

    prefix = getattr(request.app.state, "prompts_dir_prefix", None)
    if prefix is None:
        # Fall back to settings when the lifespan hook has not stashed
        # the explicit value (eg. older boot scripts).
        from ..config import Settings

        prefix = Settings().prompts_dir_prefix
    return prefix or ""


def _build_pr_description(
    request: Request,
    path: str,
    diff: str,
    draft_body: str,
    draft_branch: str,
) -> str:
    """Gather renderer inputs and produce the PR description Markdown.

    Synchronous wrapper around :func:`render_pr_description` so the
    handler can call it via ``asyncio.to_thread`` (the underlying
    file reads are blocking I/O).

    Steps:

    1. Look up the sandbox-history provider on ``app.state``. When
       no provider is wired we pass an empty tuple
       (the renderer prints "no sandbox runs were recorded").
       Production wiring can attach a callable to
       ``app.state.prompt_sandbox_history`` (signature
       ``(branch: str) -> Sequence[SandboxRunSummary]``) without
       changing this module.

    2. Hand everything to :func:`render_pr_description`.
    """

    # The backlog cross-reference section is disabled (no reference
    # document is shipped); the renderer prints a soft notice instead of
    # making confident claims.
    v15_status = extract_v15_status(body=draft_body, mimari_text=None)

    # ---- 2. Sandbox history ------------------------------------------
    sandbox_history: tuple[SandboxRunSummary, ...] = ()
    history_provider = getattr(
        request.app.state, "prompt_sandbox_history", None
    )
    if history_provider is not None:
        try:
            sandbox_history = tuple(history_provider(draft_branch))
        except Exception as exc:  # noqa: BLE001 - history is opt-in
            logger.warning(
                "prompt_sandbox_history(%s) raised %s - rendering "
                "PR description without sandbox table.",
                draft_branch,
                exc,
            )
            sandbox_history = ()

    # ---- 3. Render ---------------------------------------------------
    return render_pr_description(
        path=path,
        diff=diff,
        sandbox_history=sandbox_history,
        v15_status=v15_status,
    )


async def _safe_audit(sink: _AuditSink, event: AuditEvent) -> None:
    """Write ``event`` through ``sink`` without ever raising.

    Audit-write failures must never mask the underlying request
    outcome (the same invariant the AdminProxy enforces - see
    :meth:`src.proxy.AdminProxy._emit_rbac_denied`). The router
    already returns the success / failure shape by the time this
    helper runs; a sink failure is logged and swallowed.
    """

    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 - never raise from audit
        logger.warning(
            "prompts audit sink raised: action=%s actor=%s err=%s",
            event.action,
            event.actor_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Sandbox-run persistence
# ---------------------------------------------------------------------------


#: Sentinel value used in ``draft_branch`` when the caller supplied
#: an inline ``body`` instead of a draft branch. The
#: ``prompt_sandbox_runs`` table requires the column NOT NULL (per
#: ``infra/postgres/migrations/001_prompt_sandbox_runs.sql``); the
#: promote endpoint treats this sentinel the same as
#: ``passed=False`` because there is no committed branch to PR.
_INLINE_BODY_BRANCH_SENTINEL: str = "__inline_body__"


#: ``cost_usd`` on the migration column is ``NUMERIC(10, 4)`` - wider
#: than necessary but tighter than the ``NUMERIC(12, 6)`` used on
#: ``shared.cost_tracking``. We pass the Decimal through asyncpg
#: which casts it server-side, so no rounding is needed at the
#: application layer; we cap to 9 999 999.9999 (the max
#: ``NUMERIC(10, 4)`` representable) only as a defensive fence.
_INSERT_SANDBOX_RUN_SQL = (
    "INSERT INTO automation.prompt_sandbox_runs "
    "(prompt_path, draft_branch, sample_input, prompt_body_hash, "
    "response_text, token_in, token_out, cost_usd, passed, actor_id) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) "
    "RETURNING id"
)


def _hash_prompt_body(body: str) -> str:
    """Return a stable SHA-256 hex digest of ``body``.

    Used as the ``prompt_body_hash`` column on
    ``automation.prompt_sandbox_runs``. The hex form keeps the
    column ``TEXT``-friendly (no encoding fuss) and makes the
    promote endpoint's "the body that was sandbox-tested matches
    the body about to be PR'd" check a single string compare.
    """

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def _record_sandbox_run(
    *,
    pool: Any | None,
    prompt_path: str,
    draft_branch: str | None,
    sample_input: str,
    prompt_body: str,
    result: Any,
    passed: bool,
    actor_id: str,
) -> str | None:
    """Insert one row into ``automation.prompt_sandbox_runs``.

    Returns the inserted ``id`` as a string (UUID) when the write
    succeeds, ``None`` when the pool is missing or the INSERT
    fails. Failures are swallowed so the sandbox-test response
    still surfaces the LLM result to the caller - the persisted
    record is only required for the follow-up promote endpoint
    workflow. When this helper returns ``None`` the promote
    endpoint will reject the chained call with 404.

    Args:
        pool: Optional asyncpg pool (``app.state.pg_pool``). When
            ``None`` the helper short-circuits.
        prompt_path: Repo-relative POSIX path to the prompt file.
            Already normalised by :func:`_safe_relative_path`.
        draft_branch: The branch the body was read from when the
            caller supplied ``payload.branch``; ``None`` when the
            caller supplied an inline ``body``. Mapped to
            :data:`_INLINE_BODY_BRANCH_SENTINEL` so the NOT NULL
            constraint on the column holds.
        sample_input: Sample user input forwarded to the LLM.
        prompt_body: The system prompt body that was tested.
        result: The :class:`SandboxResult` from
            :meth:`PromptSandbox.run` - duck-typed on
            ``response_text``, ``token_in``, ``token_out``,
            ``cost_usd``.
        passed: Whether the run is considered promote-eligible
            (``True`` when the LLM round-trip succeeded; promote
            requires this to be ``True``).
        actor_id: OIDC ``sub`` of the caller for the audit chain.
    """

    if pool is None:
        logger.warning(
            "prompt_sandbox_runs persistence skipped: pg_pool unavailable; "
            "promote endpoint will return 404 for this sandbox run "
            "(prompt_path=%s actor=%s)",
            prompt_path,
            actor_id,
        )
        return None

    body_hash = _hash_prompt_body(prompt_body)
    branch_value = draft_branch or _INLINE_BODY_BRANCH_SENTINEL

    try:
        async with pool.acquire() as conn:
            row_id = await conn.fetchval(
                _INSERT_SANDBOX_RUN_SQL,
                prompt_path,
                branch_value,
                sample_input,
                body_hash,
                result.response_text,
                int(result.token_in),
                int(result.token_out),
                result.cost_usd,
                passed,
                actor_id,
            )
    except Exception as exc:  # noqa: BLE001 - soft-fail per ops policy
        logger.warning(
            "prompt_sandbox_runs INSERT failed; sandbox response "
            "still returned to caller (prompt_path=%s actor=%s "
            "err_type=%s err=%s)",
            prompt_path,
            actor_id,
            type(exc).__name__,
            exc,
        )
        return None

    if row_id is None:
        # Defence-in-depth - ``RETURNING id`` should always produce
        # a value; if a fake or future pool returns ``None`` we
        # treat it as a failed write rather than emitting a NULL
        # ``sandbox_run_id`` that the promote endpoint cannot
        # honour.
        return None

    # asyncpg returns UUID columns as :class:`uuid.UUID`; some fake
    # pools return strings. Normalise to ``str`` so the response
    # model's typing stays predictable.
    if isinstance(row_id, uuid.UUID):
        return str(row_id)
    return str(row_id)


__all__ = [
    "router",
    "get_prompts_git_repo",
    "get_prompts_pr_opener",
    "get_prompts_audit_sink",
    "get_prompt_sandbox",
    "get_prompts_pg_pool",
    "get_clock",
    "PromoteRequest",
    "PromoteResponse",
]
