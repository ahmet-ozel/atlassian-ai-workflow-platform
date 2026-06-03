"""Pydantic v2 request/response models for the PromptsGitRouter.

Co-located with :mod:`src.routers.prompts_git` so the HTTP boundary
stays in one folder. The router itself stays Pydantic-free internally
(it works with :class:`git_shared.GitCommit` /
:class:`git_shared.PullRequestRef` frozen dataclasses) and adapts those
into the models below for FastAPI's serialiser.

Design references
-----------------
* design notes §`PromptsGitRouter` — endpoint matrix and JSON shapes.
* behavior 2.2 — ``GET /admin/prompts``, ``GET /admin/prompts/{path}``,
  ``POST .../draft``, ``POST .../pr``.
* behavior 2.9 — every write path is preceded by
  ``validate_template_format(body)`` at the router layer.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


class PromptListItem(BaseModel):
    """Row shape returned by ``GET /admin/prompts`` (behavior 2.2).

    Only ``path`` and ``commit_hash`` are mandatory — both come from
    the underlying :class:`git_shared.GitRepo`. ``size_bytes`` is
    populated when the router can stat the blob without an extra round
    trip; the Pydantic optional shape lets callers degrade gracefully
    when an old git index does not expose blob sizes.
    """

    model_config = ConfigDict(from_attributes=True)

    path: str = Field(
        description="Repository-relative POSIX path to the prompt file.",
    )
    commit_hash: str = Field(
        description=(
            "Short SHA of the commit that last touched this file on "
            "the configured main branch (behavior 2.6)."
        ),
    )
    size_bytes: Optional[int] = Field(
        default=None,
        ge=0,
        description="Blob size in bytes (omitted when unavailable).",
    )


class PromptListResponse(BaseModel):
    """``GET /admin/prompts`` 200 body."""

    items: list[PromptListItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Single file
# ---------------------------------------------------------------------------


class PromptDetail(BaseModel):
    """``GET /admin/prompts/{path}`` 200 body.

    Returns the raw Markdown body plus the metadata needed by the
    admin UI to drive the editor (commit hash for optimistic
    concurrency, branch the body was read from).
    """

    path: str
    branch: str
    commit_hash: str
    body: str


# ---------------------------------------------------------------------------
# Draft
# ---------------------------------------------------------------------------


class PromptDraftRequest(BaseModel):
    """``POST /admin/prompts/{path}/draft`` body.

    The router enforces three invariants before it touches git:

    1. ``body`` is non-empty (the schema does the work below).
    2. ``validate_template_format(body)`` passes (behavior 2.9).
    3. The path exists on the configured main branch — the router
       rejects writes to brand-new files at the API edge so the audit
       trail is "edit existing" and "create new" stays explicit.
    """

    body: str = Field(
        min_length=1,
        description="Full Markdown content to commit on the draft branch.",
    )
    message: Optional[str] = Field(
        default=None,
        max_length=200,
        description=(
            "Optional commit message override. Defaults at runtime "
            "to ``draft prompt change: <path>``."
        ),
    )


class PromptDraftResponse(BaseModel):
    """``POST /admin/prompts/{path}/draft`` 201 body."""

    path: str
    branch: str
    commit_hash: str
    short_hash: str


# ---------------------------------------------------------------------------
# PR
# ---------------------------------------------------------------------------


class PromptPrRequest(BaseModel):
    """``POST /admin/prompts/{path}/pr`` body.

    The draft branch must already exist (i.e. the caller has gone
    through ``POST .../draft`` first). The title / description fields
    are optional — when omitted the router falls back to deterministic
    defaults that include the prompt path and the diff against
    ``main``.
    """

    branch: str = Field(
        min_length=1,
        description=(
            "Draft branch to open the PR from. Must have been created "
            "via ``POST /admin/prompts/{path}/draft`` first."
        ),
    )
    title: Optional[str] = Field(
        default=None,
        max_length=120,
        description="Optional PR title override.",
    )
    description: Optional[str] = Field(
        default=None,
        description=(
            "Optional PR description override. When omitted the router "
            "renders a deterministic Markdown summary of the diff vs main."
        ),
    )


class PromptPrResponse(BaseModel):
    """``POST /admin/prompts/{path}/pr`` 201 body."""

    path: str
    pr_id: str
    pr_url: str
    source_branch: str
    target_branch: str
    provider: str


# ---------------------------------------------------------------------------
# Sandbox test (service lifecycle wiring — behavior 2.4)
# ---------------------------------------------------------------------------


class PromptSandboxRequest(BaseModel):
    """``POST /admin/prompts/{path}/sandbox-test`` body.

    The endpoint pairs a draft prompt body with a sample user input
    and asks :class:`src.sandbox.PromptSandbox` to issue a
    ``cost_tag='sandbox'`` LLM call (behavior 2.4). Two
    invariants are enforced at the schema layer:

    1. ``sample_input`` is non-empty — sandbox-test calls without a
       sample input would just echo the system prompt back at the
       LLM, which is rarely useful and skews the cost row's
       ``token_in`` figure.
    2. Either ``body`` or ``branch`` is provided. When ``branch`` is
       supplied the router reads the prompt body off that draft
       branch (so the sandbox tests *exactly* what was committed);
       when ``body`` is supplied the router uses it verbatim
       (covers the "Test in Sandbox" button before the user clicks
       "Save Draft"). The router cross-checks this in code because
       ``oneof`` is awkward in Pydantic v2 and the rule is easier to
       audit when expressed as an explicit handler.
    """

    sample_input: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "Sample user message forwarded to the LLM as the "
            "``user`` role. Required so sandbox-test calls always "
            "carry a meaningful prompt-input pair."
        ),
    )
    body: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Draft prompt body to test (system message). Mutually "
            "exclusive with ``branch``; supply this when the "
            "developer has unsaved edits in the editor."
        ),
    )
    branch: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Draft branch to read the prompt body from. Mutually "
            "exclusive with ``body``; supply this to test exactly "
            "what was committed to the draft branch."
        ),
    )
    dept_id: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "Optional department id carried through to the cost "
            "record so ``/admin/costs`` can attribute sandbox "
            "spend per team. Sandbox calls never deduct from a "
            "dept's budget regardless of this value."
        ),
    )


class PromptSandboxResponse(BaseModel):
    """``POST /admin/prompts/{path}/sandbox-test`` 200 body.

    Mirrors :class:`src.sandbox.SandboxResult` field-for-field so
    the JSON envelope is a one-to-one projection of the dataclass.
    The ``cost_tag`` field is always ``"sandbox"`` — exposing it
    on the response makes the isolation contract auditable from a
    single response without re-reading the source.

    ``sandbox_run_id`` was added by ``platform operations``
    prompt promotion flow. It is the
    UUID primary key of the ``automation.prompt_sandbox_runs`` row
    written by the endpoint after a successful sandbox invocation;
    the caller forwards it to ``POST /admin/prompts/{path}/promote``
    so the promote handler can verify the sandbox
    actually ``passed`` before opening a PR. The field is
    :class:`Optional` so the endpoint stays answerable even when the
    asyncpg pool is degraded — in that case the response carries
    ``sandbox_run_id=None`` and the promote endpoint will reject
    follow-up calls with 404 (``sandbox_run_not_found``).
    """

    path: str
    response_text: str
    token_in: int = Field(ge=0)
    token_out: int = Field(ge=0)
    # ``cost_usd`` is :class:`~decimal.Decimal` on the dataclass so
    # the JSON layer also serialises it as a string to preserve the
    # six-decimal precision that ``shared.cost_tracking`` carries.
    cost_usd: str
    invoked_at: str
    model: str
    provider: str
    cost_tag: str
    # Additive prompt-promotion metadata.
    sandbox_run_id: Optional[str] = Field(
        default=None,
        description=(
            "UUID of the ``automation.prompt_sandbox_runs`` row "
            "written for this invocation. ``None`` only when the "
            "Postgres pool was unavailable at request time; "
            "callers must treat it as the opaque handle to forward "
            "to ``POST /admin/prompts/{path}/promote``."
        ),
    )


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------


class PromoteRequest(BaseModel):
    """``POST /admin/prompts/{path}/promote`` body.

    The caller must supply the ``sandbox_run_id`` returned by a
    previous ``POST .../sandbox-test`` call. The promote endpoint
    looks up that row in ``automation.prompt_sandbox_runs``, verifies
    ``passed=true``, then delegates to the same PR-opening logic used
    by ``POST .../pr``.

    ``target_branch`` defaults to ``"main"`` matching the PR endpoint
    convention. ``title`` is required (the PR endpoint accepts
    ``None`` and falls back to a generated title, but the promote
    flow is explicit about intent). ``description`` is optional and
    follows the same fallback logic as the PR endpoint.
    """

    draft_branch: str = Field(
        min_length=1,
        description=(
            "Draft branch to promote. Must match the ``draft/<actor>-<ts>`` "
            "shape and exist in the repository."
        ),
    )
    sandbox_run_id: str = Field(
        min_length=1,
        description=(
            "UUID of the ``automation.prompt_sandbox_runs`` row from a "
            "previous sandbox-test call. The promote endpoint verifies "
            "``passed=true`` before opening a PR."
        ),
    )
    target_branch: Optional[str] = Field(
        default="main",
        min_length=1,
        description="Target branch for the PR. Defaults to ``main``.",
    )
    title: str = Field(
        min_length=1,
        max_length=120,
        description="PR title.",
    )
    description: Optional[str] = Field(
        default=None,
        description=(
            "Optional PR description override. When omitted the router "
            "renders a deterministic Markdown summary of the diff vs the "
            "target branch."
        ),
    )


class PromoteResponse(BaseModel):
    """``POST /admin/prompts/{path}/promote`` 201 body."""

    pr_url: str
    branch: str
    sandbox_run_id: str


__all__ = [
    "PromptDetail",
    "PromptDraftRequest",
    "PromptDraftResponse",
    "PromptListItem",
    "PromptListResponse",
    "PromptPrRequest",
    "PromptPrResponse",
    "PromptSandboxRequest",
    "PromptSandboxResponse",
    "PromoteRequest",
    "PromoteResponse",
]
