"""``PromptsRouter`` (`platform-gap-fill` task 14.1).

**Validates: Requirements 14.1, 14.2, 14.3, 14.4**

Admin-only prompt CRUD + sandbox-test + PR commit surface for the
files that live under ``platform/prompts/``. The router exposes four
endpoints under ``/api/v1/prompts``:

* ``GET    /api/v1/prompts``                  — list every ``.md``
  file under the configured prompts directory with its filesystem
  ``last_modified`` timestamp and a SHA-256 ``content_hash``
  (R14.1).
* ``GET    /api/v1/prompts/{name}``           — return the current
  content of one prompt (R14.1).
* ``POST   /api/v1/prompts/{name}/sandbox``   — run a single,
  isolated LLM round-trip against a candidate body without
  persisting the change (R14.2).
* ``POST   /api/v1/prompts/{name}/commit``    — create a draft
  branch, write the new body, push it, open a Bitbucket draft PR,
  and record the change in ``shared.prompt_versions`` plus
  ``shared.audit_events`` (R14.3, R14.4).

All endpoints are gated by :func:`require_admin` (mirrors the
``workflow_control`` / ``security`` routers in this service).

Protocol-based DI
-----------------

The router resolves three side-effect surfaces through ``app.state``
slots so production can wire real backends and tests can inject
fakes without monkey-patching:

* ``app.state.prompts_committer`` — :class:`SupportsPromptCommitter`
  adapter that creates the draft branch, writes the file, and pushes
  it to ``origin``. Production wires this against
  :class:`git_shared.GitRepo`.
* ``app.state.prompts_bitbucket`` — :class:`SupportsBitbucketClient`
  adapter that opens a draft pull request once the commit lands. The
  existing :class:`git_shared.PullRequestOpener` already implements
  this shape; the router accepts either one to avoid duplicating
  wiring.
* ``app.state.prompts_llm`` — :class:`SupportsLLMClient` adapter that
  performs the single LLM call backing the sandbox endpoint.

Each missing slot yields ``HTTP 503`` with a stable ``reason`` field
so the FE can render a "service not ready" state without parsing
generic 5xx pages.

Audit + version trail
---------------------

A successful commit writes:

1. One row into ``shared.prompt_versions`` —
   ``(prompt_name, content_hash, changed_by, pr_url, created_at)``.
   The ``(prompt_name, content_hash)`` pair is UNIQUE so re-committing
   identical content surfaces ``409 prompt_unchanged`` (R14.4 — no
   duplicate audit chain).
2. One ``prompt_updated`` audit event into ``shared.audit_events``
   carrying ``{action: "prompt_updated", prompt_name, content_hash,
   admin, pr_url, timestamp}`` (R14.4).

Audit-write failures never block the request — the version row is
the canonical record; the audit event is best-effort.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = [
    "router",
    "SupportsBitbucketClient",
    "SupportsLLMClient",
    "SupportsPromptCommitter",
    "BitbucketPullRequest",
    "CommitResult",
    "SandboxResult",
    "PromptListItem",
    "PromptListResponse",
    "PromptDetailResponse",
    "PromptSandboxRequest",
    "PromptSandboxResponse",
    "PromptCommitRequest",
    "PromptCommitResponse",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Audit action emitted on every successful commit (R14.4).
_AUDIT_ACTION_PROMPT_UPDATED: str = "prompt_updated"

#: Hard cap on prompt body size — markdown system prompts comfortably
#: fit under 64 KiB; rejecting larger payloads protects against
#: accidental binary uploads.
_MAX_PROMPT_BODY_BYTES: int = 64 * 1024

#: Regex used to validate prompt names taken from the URL path. We
#: accept a forward-slash hierarchy (``notifications/foo.md``) but
#: reject ``..`` segments, absolute paths, and Windows drive letters
#: at :func:`_safe_prompt_name`.
_VALID_NAME_CHARS_RE = re.compile(r"^[A-Za-z0-9._\-/]+$")

#: SQL to insert a new row into ``shared.prompt_versions``. The
#: UNIQUE constraint on ``(prompt_name, content_hash)`` means
#: re-committing identical content raises a ``UniqueViolationError``
#: which the router maps to HTTP 409.
_INSERT_PROMPT_VERSION_SQL = (
    "INSERT INTO shared.prompt_versions "
    "(prompt_name, content_hash, changed_by, pr_url) "
    "VALUES ($1, $2, $3, $4) "
    "RETURNING id, created_at"
)


# ---------------------------------------------------------------------------
# Protocols (the router's only contract with side-effect backends)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsLLMClient(Protocol):
    """Single-call LLM surface used by the sandbox endpoint.

    The sandbox makes exactly one round-trip: render the candidate
    prompt body against the supplied sample input, return the model
    output. Implementations are responsible for tagging the call with
    a ``cost_tag="sandbox"`` (or equivalent) so production budgets
    are not impacted.

    Tests inject an in-memory fake; production wires this against
    the shared assistant-service / vLLM client.
    """

    async def complete(
        self,
        *,
        prompt_body: str,
        sample_input: str,
    ) -> "SandboxResult":
        """Return the model's response for *(prompt_body, sample_input)*."""


@runtime_checkable
class SupportsPromptCommitter(Protocol):
    """Git-side surface: branch + write + push.

    Production wires this against :class:`git_shared.GitRepo` (one
    adapter that calls ``create_branch_from_main``, ``write_file``,
    ``commit``, ``push``). The protocol is intentionally narrow so a
    test fake can satisfy it with a single async method.

    The returned :class:`CommitResult` carries the branch name + the
    full commit SHA; the router uses both when opening the PR and
    composing the audit row.
    """

    async def commit_prompt(
        self,
        *,
        prompt_name: str,
        body: str,
        actor_id: str,
        message: str,
    ) -> "CommitResult":
        """Create branch, write file, commit + push, return refs."""


@runtime_checkable
class SupportsBitbucketClient(Protocol):
    """PR-opening surface used by the commit endpoint.

    Mirrors the existing :class:`git_shared.PullRequestOpener` shape
    so the production wiring can pass that opener straight through —
    we re-declare the protocol here to keep this module
    self-contained and to surface the contract explicitly in the
    router's docstring.
    """

    async def open_pull_request(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> "BitbucketPullRequest":
        """Open a draft PR; return the upstream id + URL."""


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------


class SandboxResult(BaseModel):
    """LLM result projected into the sandbox response.

    Kept as a Pydantic model so it can serialise straight back to
    the FE without an additional projection layer.
    """

    response_text: str = Field(..., description="Raw LLM output.")
    model: str | None = Field(
        default=None, description="Model identifier reported by the provider."
    )
    provider: str | None = Field(
        default=None,
        description="Provider name (eg. ``openai``, ``anthropic``, ``vllm``).",
    )
    token_in: int | None = Field(
        default=None, description="Prompt-side token count."
    )
    token_out: int | None = Field(
        default=None, description="Completion-side token count."
    )


class CommitResult(BaseModel):
    """Outcome of :meth:`SupportsPromptCommitter.commit_prompt`."""

    branch: str = Field(..., description="Pushed branch name.")
    commit_sha: str = Field(..., description="Full commit SHA.")


class BitbucketPullRequest(BaseModel):
    """Outcome of :meth:`SupportsBitbucketClient.open_pull_request`."""

    pr_id: str = Field(..., description="Upstream PR id (string-coerced).")
    pr_url: str = Field(..., description="Absolute https:// link.")


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class PromptListItem(BaseModel):
    """Single entry in :class:`PromptListResponse`.

    ``name`` is the path under the configured prompts directory using
    forward slashes (eg. ``notifications/build_failed.md``) so the FE
    can use the same string in subsequent ``GET /api/v1/prompts/{name}``
    calls without re-encoding.
    """

    name: str = Field(..., description="Prompt path (relative to prompts dir).")
    last_modified: str = Field(
        ..., description="ISO-8601 UTC mtime of the on-disk file."
    )
    content_hash: str = Field(
        ..., description="SHA-256 hex digest of the current content."
    )
    size_bytes: int = Field(..., description="File size in bytes.")


class PromptListResponse(BaseModel):
    """Response shape for ``GET /api/v1/prompts``."""

    items: list[PromptListItem]


class PromptDetailResponse(BaseModel):
    """Response shape for ``GET /api/v1/prompts/{name}``."""

    name: str
    content: str
    content_hash: str
    last_modified: str
    size_bytes: int


class PromptSandboxRequest(BaseModel):
    """Request body for ``POST /api/v1/prompts/{name}/sandbox``."""

    body: str = Field(
        ...,
        description=(
            "Edited prompt body to test. Must be UTF-8 text under "
            f"{_MAX_PROMPT_BODY_BYTES} bytes."
        ),
        min_length=1,
        max_length=_MAX_PROMPT_BODY_BYTES,
    )
    sample_input: str = Field(
        default="",
        description="Sample user input forwarded to the LLM.",
        max_length=_MAX_PROMPT_BODY_BYTES,
    )


class PromptSandboxResponse(BaseModel):
    """Response shape for ``POST /api/v1/prompts/{name}/sandbox``."""

    name: str
    response_text: str
    model: str | None = None
    provider: str | None = None
    token_in: int | None = None
    token_out: int | None = None


class PromptCommitRequest(BaseModel):
    """Request body for ``POST /api/v1/prompts/{name}/commit``."""

    body: str = Field(
        ...,
        description="New prompt body. Must differ from the current content.",
        min_length=1,
        max_length=_MAX_PROMPT_BODY_BYTES,
    )
    message: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional commit message override. Defaults to "
            "``prompt: update {name}``."
        ),
    )
    pr_title: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Optional PR title override. Defaults to ``Prompt update: "
            "{name}``."
        ),
    )
    pr_description: str | None = Field(
        default=None,
        max_length=4096,
        description=(
            "Optional PR description override. Defaults to a short "
            "stub identifying the actor + content_hash."
        ),
    )


class PromptCommitResponse(BaseModel):
    """Response shape for ``POST /api/v1/prompts/{name}/commit``."""

    name: str
    branch: str
    commit_sha: str
    pr_id: str
    pr_url: str
    content_hash: str
    version_id: int


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/prompts",
    tags=["prompts"],
)


# ---------------------------------------------------------------------------
# Helpers — path / hash / file IO
# ---------------------------------------------------------------------------


def _safe_prompt_name(raw: str) -> str:
    """Normalise *raw* and reject path-traversal / absolute paths.

    The FastAPI ``{name:path}`` converter passes ``..`` segments and
    leading slashes through; we collapse the path and reject anything
    that points outside the configured prompts directory. We also
    require the basename to end with ``.md`` so the GET endpoint can
    avoid serving non-prompt files that happen to live alongside the
    markdown ones.
    """

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt name must not be empty",
        )

    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt name must be relative",
        )

    parts = PurePosixPath(raw.replace("\\", "/")).parts
    cleaned: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="prompt name must not contain '..' segments",
            )
        cleaned.append(part)

    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt name must not be empty after normalisation",
        )

    name = "/".join(cleaned)
    if not _VALID_NAME_CHARS_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "prompt name may only contain letters, digits, '.', "
                "'_', '-', and '/'"
            ),
        )

    if not name.endswith(".md"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt name must end with '.md'",
        )

    return name


def _sha256(content: str) -> str:
    """Stable SHA-256 hex digest of *content* (UTF-8 encoded)."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _resolve_prompts_dir(request: Request) -> Path:
    """Resolve the absolute filesystem path of the prompts directory.

    Combines ``app.state.workspace_root`` with the configured
    ``prompts_dir_prefix`` from :class:`Settings`. When neither slot
    is wired (extremely unusual — would imply lifespan never ran)
    the helper raises ``HTTP 503`` so the endpoint surface degrades
    cleanly rather than dereferencing a ``None``.
    """

    workspace_root = getattr(request.app.state, "workspace_root", None)
    prefix = getattr(request.app.state, "prompts_dir_prefix", None)

    if workspace_root is None or prefix is None:
        # Fall back to Settings so the dev case (no lifespan wiring)
        # still works for unit tests that instantiate the router
        # against an empty FastAPI app.
        from ..config import Settings

        settings = Settings()
        workspace_root = workspace_root or settings.workspace_root
        prefix = prefix if prefix is not None else settings.prompts_dir_prefix

    if workspace_root is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompts_dir_unavailable",
            },
        )

    base = Path(workspace_root) / (prefix or "")
    return base


def _resolve_prompt_path(prompts_dir: Path, name: str) -> Path:
    """Resolve a safe prompt path; raises 404 if missing or outside dir."""

    target = (prompts_dir / name).resolve()
    try:
        target.relative_to(prompts_dir.resolve())
    except ValueError as exc:
        # Defence in depth — _safe_prompt_name already rejects
        # traversal, but realpath resolution can still surface a
        # symlink that escapes the prompts dir. Treat as 400 so the
        # FE distinguishes it from a "file not on disk" 404.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="prompt path escapes prompts directory",
        ) from exc
    return target


# ---------------------------------------------------------------------------
# Helpers — dependency lookup
# ---------------------------------------------------------------------------


def _get_llm_client(request: Request) -> SupportsLLMClient:
    """Return the wired :class:`SupportsLLMClient` (sandbox endpoint)."""

    client = getattr(request.app.state, "prompts_llm", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompts_llm_unavailable",
            },
        )
    return client


def _get_committer(request: Request) -> SupportsPromptCommitter:
    """Return the wired :class:`SupportsPromptCommitter` (commit endpoint)."""

    committer = getattr(request.app.state, "prompts_committer", None)
    if committer is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompts_committer_unavailable",
            },
        )
    return committer


def _get_bitbucket(request: Request) -> SupportsBitbucketClient:
    """Return the wired :class:`SupportsBitbucketClient` (commit endpoint)."""

    bitbucket = getattr(request.app.state, "prompts_bitbucket", None)
    if bitbucket is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "prompts_bitbucket_unavailable",
            },
        )
    return bitbucket


def _get_pg_pool(request: Request) -> Any:
    """Return the asyncpg pool used to insert into ``shared.prompt_versions``.

    Raises:
        HTTPException(503): When ``app.state.pg_pool`` is missing.
            The commit endpoint cannot run without a database — the
            audit chain mandated by R14.4 (``shared.prompt_versions``
            row + ``shared.audit_events`` row) requires a live pool.
    """

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "pg_pool_unavailable",
            },
        )
    return pool


def _get_audit_sink(request: Request) -> Any | None:
    """Return the audit sink used for ``prompt_updated`` events.

    Mirrors :func:`workflow_control._get_audit_sink`: prefers an
    explicit ``app.state.prompts_audit_sink`` slot, falls back to
    the AdminProxy's audit sink so the events land in the same
    stream as other admin actions. Returns ``None`` when neither
    is wired — :func:`_emit_prompt_updated_audit` becomes a no-op
    in that case.
    """

    explicit = getattr(request.app.state, "prompts_audit_sink", None)
    if explicit is not None:
        return explicit
    proxy = getattr(request.app.state, "admin_proxy", None)
    if proxy is not None:
        return getattr(proxy, "_audit", None)
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=PromptListResponse,
    summary="List prompt files under platform/prompts/ (admin only)",
    dependencies=[Depends(require_admin)],
)
async def list_prompts(request: Request) -> PromptListResponse:
    """Return every ``.md`` file under the configured prompts directory.

    **Validates: Requirement 14.1**

    The list is computed off the local filesystem rather than via
    git so the response reflects on-disk reality (uncommitted edits
    by the operator are visible). Each item carries:

    * ``name`` — repo-relative POSIX path (eg. ``planner.md`` or
      ``notifications/build_failed.md``).
    * ``last_modified`` — ISO-8601 UTC ``mtime`` of the file.
    * ``content_hash`` — SHA-256 of the file body so the FE can
      detect drift between two reads.
    """

    prompts_dir = _resolve_prompts_dir(request)
    if not prompts_dir.is_dir():
        # Empty list rather than 503 — the directory may simply not
        # exist yet in a fresh checkout, and the FE renders that as
        # "no prompts" without surfacing a hard error.
        return PromptListResponse(items=[])

    items: list[PromptListItem] = []
    base_resolved = prompts_dir.resolve()

    def _scan() -> list[PromptListItem]:
        scanned: list[PromptListItem] = []
        for path in sorted(prompts_dir.rglob("*.md")):
            try:
                # Reject symlink escapes defensively.
                path.resolve().relative_to(base_resolved)
            except ValueError:
                logger.warning(
                    "prompts: skipping symlink escape candidate %s",
                    path,
                )
                continue
            try:
                stat = path.stat()
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "prompts: could not read %s: %s", path, exc
                )
                continue
            rel = path.relative_to(prompts_dir).as_posix()
            scanned.append(
                PromptListItem(
                    name=rel,
                    last_modified=datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    content_hash=_sha256(content),
                    size_bytes=stat.st_size,
                )
            )
        return scanned

    items = await asyncio.to_thread(_scan)
    return PromptListResponse(items=items)


@router.get(
    "/{name:path}",
    response_model=PromptDetailResponse,
    summary="Read a single prompt file (admin only)",
    dependencies=[Depends(require_admin)],
)
async def read_prompt(name: str, request: Request) -> PromptDetailResponse:
    """Return the current content of one prompt.

    **Validates: Requirement 14.1**
    """

    safe_name = _safe_prompt_name(name)
    prompts_dir = _resolve_prompts_dir(request)
    target = _resolve_prompt_path(prompts_dir, safe_name)

    def _read() -> tuple[str, int, float]:
        stat = target.stat()
        content = target.read_text(encoding="utf-8")
        return content, stat.st_size, stat.st_mtime

    try:
        content, size_bytes, mtime = await asyncio.to_thread(_read)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"prompt not found: {safe_name!r}",
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"failed to read prompt {safe_name!r}: {exc}",
        ) from exc

    return PromptDetailResponse(
        name=safe_name,
        content=content,
        content_hash=_sha256(content),
        last_modified=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        size_bytes=size_bytes,
    )


@router.post(
    "/{name:path}/sandbox",
    response_model=PromptSandboxResponse,
    summary="Run an isolated LLM call against an edited prompt (admin only)",
    dependencies=[Depends(require_admin)],
)
async def sandbox_prompt(
    name: str,
    request: Request,
    body: PromptSandboxRequest = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> PromptSandboxResponse:
    """Render a draft prompt body against the LLM without persisting.

    **Validates: Requirement 14.2**

    The endpoint pairs a candidate prompt body with a sample user
    input, asks the wired :class:`SupportsLLMClient` to perform a
    single round-trip, and returns the model output. Nothing is
    written to disk, git, or the database — the caller's edits
    remain in their local editor session.

    The LLM client implementation is responsible for tagging the
    call with a ``cost_tag="sandbox"`` so the rendered cost row is
    excluded from production budgets (consistent with the existing
    :class:`PromptSandbox` pattern in ``prompts_git``).
    """

    safe_name = _safe_prompt_name(name)
    llm = _get_llm_client(request)

    if len(body.body.encode("utf-8")) > _MAX_PROMPT_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"sandbox body exceeds {_MAX_PROMPT_BODY_BYTES} bytes "
                "(prompts must stay under 64 KiB)"
            ),
        )

    try:
        result = await llm.complete(
            prompt_body=body.body,
            sample_input=body.sample_input,
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream failure
        logger.exception(
            "prompt sandbox LLM call failed (name=%s actor=%s): %s",
            safe_name,
            actor.sub,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "llm_call_failed",
                "message": str(exc),
            },
        ) from exc

    return PromptSandboxResponse(
        name=safe_name,
        response_text=result.response_text,
        model=result.model,
        provider=result.provider,
        token_in=result.token_in,
        token_out=result.token_out,
    )


@router.post(
    "/{name:path}/commit",
    response_model=PromptCommitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Commit a prompt change to a draft branch + open a PR (admin only)",
    dependencies=[Depends(require_admin)],
)
async def commit_prompt(
    name: str,
    request: Request,
    body: PromptCommitRequest = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> PromptCommitResponse:
    """Commit a prompt change and open a Bitbucket draft PR.

    **Validates: Requirements 14.3, 14.4**

    Steps:

    1. Normalise the prompt name; reject traversal segments.
    2. Resolve the existing on-disk content; reject ``409
       prompt_unchanged`` when the new body matches it byte-for-byte
       (idempotent re-commit guard).
    3. Delegate to :class:`SupportsPromptCommitter` — it creates a
       fresh draft branch off main, writes the new file, commits,
       and pushes to ``origin``.
    4. Open the PR via :class:`SupportsBitbucketClient`. On failure
       the branch is left in place so the operator can inspect /
       retry; the router surfaces ``HTTP 502``.
    5. Insert one row into ``shared.prompt_versions``. The UNIQUE
       constraint on ``(prompt_name, content_hash)`` would have
       been caught by step 2 already, but a race between two
       concurrent commits is handled here as ``409 prompt_unchanged``.
    6. Emit a ``prompt_updated`` audit event into
       ``shared.audit_events`` (best-effort).
    """

    safe_name = _safe_prompt_name(name)
    if len(body.body.encode("utf-8")) > _MAX_PROMPT_BODY_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"commit body exceeds {_MAX_PROMPT_BODY_BYTES} bytes "
                "(prompts must stay under 64 KiB)"
            ),
        )

    new_hash = _sha256(body.body)
    committer = _get_committer(request)
    bitbucket = _get_bitbucket(request)
    pool = _get_pg_pool(request)

    # ---- 2. Idempotent re-commit guard ------------------------------
    prompts_dir = _resolve_prompts_dir(request)
    target = _resolve_prompt_path(prompts_dir, safe_name)

    existing_content: str | None = None
    if target.is_file():
        try:
            existing_content = await asyncio.to_thread(
                target.read_text, encoding="utf-8"
            )
        except OSError as exc:
            logger.warning(
                "prompt commit: could not read existing %s: %s",
                target,
                exc,
            )
            existing_content = None

    if existing_content is not None and _sha256(existing_content) == new_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "prompt_unchanged",
                "message": "new content matches the current file byte-for-byte",
            },
        )

    # ---- 3. Git-side commit -----------------------------------------
    commit_message = body.message or f"prompt: update {safe_name}"
    try:
        commit_result = await committer.commit_prompt(
            prompt_name=safe_name,
            body=body.body,
            actor_id=actor.sub,
            message=commit_message,
        )
    except Exception as exc:  # noqa: BLE001 — surface git failure
        logger.exception(
            "prompt commit (git phase) failed (name=%s actor=%s): %s",
            safe_name,
            actor.sub,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "git_commit_failed",
                "message": str(exc),
            },
        ) from exc

    # ---- 4. Open the PR ---------------------------------------------
    pr_title = body.pr_title or f"Prompt update: {safe_name}"
    pr_description = body.pr_description or (
        f"Automated prompt change.\n\n"
        f"Prompt: `{safe_name}`\n"
        f"Author: {actor.sub}\n"
        f"Content hash: `{new_hash}`\n"
    )
    try:
        pr = await bitbucket.open_pull_request(
            source_branch=commit_result.branch,
            target_branch="main",
            title=pr_title,
            description=pr_description,
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream failure
        logger.exception(
            "prompt commit (PR phase) failed (name=%s branch=%s actor=%s): %s",
            safe_name,
            commit_result.branch,
            actor.sub,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "pr_open_failed",
                "message": str(exc),
                "branch": commit_result.branch,
            },
        ) from exc

    # ---- 5. shared.prompt_versions row ------------------------------
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                _INSERT_PROMPT_VERSION_SQL,
                safe_name,
                new_hash,
                actor.sub,
                pr.pr_url,
            )
    except Exception as exc:  # noqa: BLE001
        # asyncpg.UniqueViolationError → 409 prompt_unchanged. We
        # match on the exception class name to avoid importing
        # asyncpg at the top of this module (the dependency is wired
        # at lifespan level, not router level).
        exc_name = type(exc).__name__
        if exc_name == "UniqueViolationError":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "prompt_unchanged",
                    "message": (
                        "another commit already recorded this content_hash; "
                        "the PR was opened but no version row was added"
                    ),
                    "branch": commit_result.branch,
                    "pr_url": pr.pr_url,
                },
            ) from exc
        logger.exception(
            "prompt commit: shared.prompt_versions insert failed "
            "(name=%s hash=%s actor=%s): %s",
            safe_name,
            new_hash,
            actor.sub,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "version_insert_failed",
                "message": str(exc),
                "branch": commit_result.branch,
                "pr_url": pr.pr_url,
            },
        ) from exc

    if row is None:  # pragma: no cover — RETURNING always yields a row
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="prompt_versions insert returned no row",
        )

    version_id = int(row["id"])
    created_at: datetime = row["created_at"]

    # ---- 6. Audit event (best-effort) -------------------------------
    await _emit_prompt_updated_audit(
        request,
        actor=actor,
        prompt_name=safe_name,
        content_hash=new_hash,
        pr_url=pr.pr_url,
        timestamp=created_at,
    )

    return PromptCommitResponse(
        name=safe_name,
        branch=commit_result.branch,
        commit_sha=commit_result.commit_sha,
        pr_id=pr.pr_id,
        pr_url=pr.pr_url,
        content_hash=new_hash,
        version_id=version_id,
    )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _emit_prompt_updated_audit(
    request: Request,
    *,
    actor: AuthClaims,
    prompt_name: str,
    content_hash: str,
    pr_url: str,
    timestamp: datetime,
) -> None:
    """Write a single ``prompt_updated`` audit event (R14.4).

    Failures are swallowed — the canonical record is the
    ``shared.prompt_versions`` row inserted in step 5 of
    :func:`commit_prompt`. The audit event is observability-only and
    must not fail the request.
    """

    sink = _get_audit_sink(request)
    if sink is None:
        logger.info(
            "prompt_updated audit: no sink wired (name=%s actor=%s)",
            prompt_name,
            actor.sub,
        )
        return

    payload: dict[str, Any] = {
        "action": _AUDIT_ACTION_PROMPT_UPDATED,
        "prompt_name": prompt_name,
        "content_hash": content_hash,
        "admin": actor.sub,
        "pr_url": pr_url,
        "timestamp": timestamp.isoformat() if isinstance(timestamp, datetime) else None,
    }

    event = AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=None,
        action=_AUDIT_ACTION_PROMPT_UPDATED,
        resource=f"prompt:{prompt_name}",
        result="ok",
        timestamp=timestamp if isinstance(timestamp, datetime) else datetime.now(tz=timezone.utc),
        payload=payload,
    )

    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 — never raise from audit
        logger.warning(
            "prompt_updated audit write failed (name=%s actor=%s): %s",
            prompt_name,
            actor.sub,
            exc,
        )
