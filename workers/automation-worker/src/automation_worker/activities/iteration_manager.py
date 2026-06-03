"""Iteration Manager activity for the automation-worker.

Implements the ``prepare_iteration`` Temporal activity which prepares the
context for a re-run triggered by an ``[iterate]`` comment on an already
processed Jira issue.

The activity is the first stage of the iteration re-run flow described in
``design.md`` §"Iteration Re-Run Flow":

1. **Authorization gate ** — the comment author MUST be in the
 department's ``approvers`` list OR equal the issue reporter; any
 other author causes the activity to return a *not authorized*
 result and the orchestrating workflow drops the request.
2. **Iteration number** — load the highest ``iteration_number`` for
 ``issue_key`` from ``shared.workflow_iterations`` and increment to
 ``N+1``. The first ever iteration on an issue resolves to
 ``1`` (the original automated run is iter-0 by convention).
3. **Workspace path ** — derive ``{base}/{issue_key}/iter-{N+1}``
 via the same canonical helper used by ``execution-runner-worker``.
 The path is *guaranteed distinct* from any earlier iteration on
 the same issue so workspace state cannot leak across iterations.
4. **PR / branch carry-over (/)** — if the latest stored
 iteration recorded a ``previous_pr_id``, surface it in the result
 so the workflow can choose to push commits to the *same* PR.
 Otherwise the result carries ``previous_pr_id=None`` and the
 workflow opens a fresh branch + PR.
5. **Extra instructions ** — extract any text following the
 ``[iterate]`` keyword from the comment body and forward it as a
 free-form instruction string for the LLM context.
6. **Persist ** — insert a ``shared.workflow_iterations`` row
 with status ``'pending'``. The activity returns the workflow_id
 placeholder it generated; the caller (workflow) overwrites
 ``status`` with ``'in_progress'`` once the inner workflow start
 succeeds.

Dependency-injection pattern
----------------------------

Mirrors:mod:`audit_prune` exactly. Two collaborators are pulled
through module-level setters configured at worker boot:

*:func:`set_db_pool` — asyncpg-shaped Postgres pool used to read /
 write ``shared.workflow_iterations``. Tests inject an in-memory
 fake (see ``tests/unit/test_iteration_manager.py``).
*:func:`set_iteration_store` — an alternative override that bypasses
 the SQL path entirely and lets tests stub the persistence surface
 with a hand-rolled:class:`IterationStore` implementation. This is
 the path used by sibling 's table migration when it lands
 *after* this activity ships — it lets (this task) be
 shipped, exercised, and unit-tested before the migration runs..
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Protocol, runtime_checkable

from temporalio import activity

__all__ = (
    # Public activity + result types
    "prepare_iteration",
    "PrepareIterationInput",
    "IterationContext",
    # Iteration store protocol + DI setters
    "IterationStore",
    "IterationRecord",
    "set_db_pool",
    "set_iteration_store",
    "set_workspace_base_path",
    "get_db_pool",
    "get_iteration_store",
    "get_workspace_base_path",
    # Pure helpers (re-exported for unit tests + Invariant tests)
    "extract_extra_instructions",
    "build_iteration_workspace_path",
    "is_iterate_command",
    "is_authorized_for_iterate",
    # Constants
    "DEFAULT_WORKSPACE_BASE_PATH",
    "ITERATE_PATTERN",
    "MAX_ITERATION_NUMBER",
    "MAX_ITERATIONS_PER_ISSUE",
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default workspace root used when no explicit base path is configured
#: through:func:`set_workspace_base_path`. Matches the ``RUNNER_BASE_PATH``
#: default of ``execution-runner-worker`` (see ``runners/workspace_path``)
#: so every worker derives ``{base}/{issue_key}/iter-{N}`` consistently.
DEFAULT_WORKSPACE_BASE_PATH: Final[str] = "/var/ai-runner"

#: Inclusive upper bound for the iteration counter. Mirrors
#: ``execution-runner-worker.runners.workspace_path.MAX_ITER`` so the two
#: helpers stay in lock-step. Issues that exceed this bound trigger an
#: explicit error rather than silently overflowing the path layout.
MAX_ITERATION_NUMBER: Final[int] = 999

#: Storm-guard cap on the number of automated iterations a single
#: issue may accumulate. Independent of:data:`MAX_ITERATION_NUMBER`
#: (which is the *path-layout* upper bound). The intent is to break
#: runaway ``[iterate]`` loops — a misbehaving approver, a flaky LLM
#: that keeps re-emitting a "please re-run" comment, or a feedback
#: loop between two bots — at a count that is well above any realistic
#: engineering session but well below the path-layout bound. The
#: activity returns an unauthorized result with
#: ``reason="max_iteration_exceeded"`` once the *current* iteration
#: count for the issue has reached this cap; the workflow surfaces a
#: Jira comment and an audit row but does **not** raise so a stray
#: ``[iterate]`` cannot crash the worker. Inclusive upper bound: a
#: row whose ``iteration_number`` already equals this value blocks the
#: next ``[iterate]``.
MAX_ITERATIONS_PER_ISSUE: Final[int] = 10

#: Case-insensitive regex matching the ``[iterate]`` command keyword in
#: a Jira comment body. Mirrors the pattern used by
#::class:`webhooks.dispatcher.WebhookDispatcher` so both sides of the
#: signal hand-off agree on what counts as an iterate command.
ITERATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\[iterate\]", re.IGNORECASE
)

#: Jira-style issue key validator. Reused so an injected ``issue_key``
#: cannot escape the ``{base}/{issue_key}/iter-{N}`` template — see the
#: matching regex in ``execution-runner-worker.runners.workspace_path``.
_ISSUE_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z0-9_]*-\d+$"
)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepareIterationInput:
    """Input for the:func:`prepare_iteration` activity.

 The shape mirrors the dispatcher's ``[iterate]`` payload (see
 ``WebhookDispatcher._start_iteration``) plus the dept config bits
 needed for the authorization check.

 Attributes:
 issue_key: The Jira issue key (e.g. ``"PAY-4211"``).
 comment_body: Full body text of the ``[iterate]`` comment —
 used both for authorization tracing (the audit log mirrors
 the original text) and for ``extra_instructions``
 extraction.
 comment_author_account_id: Atlassian ``accountId`` of the
 comment author. Compared against ``dept_config.approvers``
 and ``issue_reporter_account_id`` to gate authorization.
 issue_reporter_account_id: ``accountId`` of the Jira issue
 reporter. ``None`` when the webhook payload did not carry
 a reporter (e.g. the issue was created by a bot — in that
 case only the dept ``approvers`` list grants access).
 dept_id: Department identifier. Forwarded to the resulting
 workflow input so the inner re-run wires up the correct
 credentials.
 dept_config: Department configuration mapping. The activity
 only reads the ``approvers`` key (a list of authorized
 ``accountId`` strings); other fields are ignored. Passing
 the whole dict keeps the surface flexible for future
 authorization rules without breaking the input contract.
 trace_id: Trace identifier propagated for log correlation.
 When empty the activity generates one so downstream logs
 can still be correlated.
 """

    issue_key: str
    comment_body: str
    comment_author_account_id: str
    issue_reporter_account_id: str | None
    dept_id: str
    dept_config: dict[str, Any]
    trace_id: str = ""


@dataclass(frozen=True)
class IterationContext:
    """Result of the:func:`prepare_iteration` activity.

 The workflow consumes this dataclass to build the input for the
 inner re-run workflow. ``authorized=False`` means the activity
 declined to start an iteration — the workflow logs and
 drops the request without surfacing an exception so a stray
 ``[iterate]`` from an unauthorized user cannot crash the cron.

 Attributes:
 authorized: ``True`` if the comment author may trigger a
 re-run. ``False`` otherwise — every other field except
 ``reason`` is the empty / default value.
 reason: When ``authorized=False``, a short machine-readable
 reason code (e.g. ``"not_in_approvers"``,
 ``"max_iteration_exceeded"``). When ``authorized=True``
 the reason is ``""``.
 issue_key: Echoed for convenience.
 iteration_number: The new iteration number ``N+1``. Always
 ``>= 1``; ``0`` represents "not assigned" when the result
 is unauthorized.
 workflow_id: Suggested workflow id for the inner re-run
 (``iteration-{ISSUE_KEY}-{N}-{shortuuid}``). The caller
 may override but the activity persists this value as the
 ``workflow_id`` column of the new ``workflow_iterations``
 row.
 workspace_path: Canonical workspace path for iter-(N+1).
 Forward-slash separators, distinct from any earlier
 iteration on the same issue (/).
 previous_branch: Branch name from the most recent stored
 iteration, or ``None`` when no prior iteration exists.
 previous_pr_id: PR id from the most recent stored iteration,
 or ``None`` when no prior PR exists. The workflow uses
 this to decide whether to commit to the same PR 
 or open a new branch + PR.
 extra_instructions: Free-form text extracted from the
 ``[iterate]`` comment body following the keyword, or
 ``None`` when the user did not supply additional
 instructions.
 dept_id: Echoed for convenience.
 trace_id: Echoed (or generated) trace identifier.
 current_count: When ``authorized=False`` and
 ``reason="max_iteration_exceeded"``, the *current* highest
 iteration number stored for this issue (i.e. the count we
 refused to increment). ``None`` for every other path —
 including authorized results — to keep the field a
 no-overhead diagnostic that callers can rely on for the
 storm-guard surface only.
 """

    authorized: bool
    reason: str
    issue_key: str
    iteration_number: int
    workflow_id: str
    workspace_path: str
    previous_branch: str | None
    previous_pr_id: int | None
    extra_instructions: str | None
    dept_id: str
    trace_id: str
    current_count: int | None = None


@dataclass(frozen=True)
class IterationRecord:
    """A single ``shared.workflow_iterations`` row exposed to the activity.

 Mirrors the column layout introduced by sibling 's
 migration (``009_workflow_iterations.sql``) but is declared here so
 the activity can be unit-tested against an in-memory store before
 the migration lands. The activity only ever reads ``previous_*`` /
 ``iteration_number`` and writes a fresh row; column-level fidelity
 matters less than field shape.
 """

    issue_key: str
    iteration_number: int
    workflow_id: str
    previous_branch: str | None
    previous_pr_id: int | None
    workspace_path: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# IterationStore Protocol — abstraction over ``shared.workflow_iterations``
# ---------------------------------------------------------------------------


@runtime_checkable
class IterationStore(Protocol):
    """Persistence surface required by:func:`prepare_iteration`.

 The activity needs three operations on the ``workflow_iterations``
 table; all of them are wrapped in this protocol so:

 1. The activity can be unit-tested against a hand-rolled in-memory
 fake without spinning up Postgres.
 2. Sibling (which delivers the SQL migration) can be
 implemented in parallel — until the migration lands, production
 deployments wire:class:`PostgresIterationStore` (defined
 below) which is a no-op when the table is missing, while
 focused unit tests inject the in-memory fake directly via:func:`set_iteration_store`.

 Implementations MUST be safe to call concurrently from Temporal
 activity workers; the SQL implementation funnels through asyncpg
 pool acquisition which is already concurrency-safe.
 """

    async def latest_iteration(
        self, issue_key: str
    ) -> IterationRecord | None:
        """Return the highest-numbered iteration for ``issue_key``.

 ``None`` is returned when no row exists — the caller treats
 this as "first iteration" and the new ``iteration_number``
 becomes ``1``.
 """
        ...

    async def insert_iteration(
        self,
        *,
        issue_key: str,
        iteration_number: int,
        workflow_id: str,
        previous_branch: str | None,
        previous_pr_id: int | None,
        workspace_path: str,
        status: str,
    ) -> None:
        """Persist the new iteration row.

 Implementations MUST honour the ``UNIQUE(issue_key,
 iteration_number)`` constraint and raise a clear error on
 conflict; the activity does not retry on conflict — concurrent
 ``[iterate]`` comments race for the same iteration number and
 the second writer is rejected so the workflow can surface the
 race to the user via Jira.
 """
        ...


# ---------------------------------------------------------------------------
# Dependency-injection registry
# ---------------------------------------------------------------------------


@runtime_checkable
class _AsyncPoolLike(Protocol):
    """Minimal asyncpg pool surface used by the default SQL store.

 Mirrors:class:`audit_prune._AsyncPoolLike` so the worker boot
 script can hand the *same* pool to both activity families. Tests
 that exercise the SQL path inject an in-memory fake whose
 ``acquire`` returns a context manager yielding a fake connection
 with ``fetchrow`` / ``execute`` methods.
 """

    def acquire(self) -> Any:  # noqa: D401 - protocol shape
        """Return an async context manager yielding a connection."""
        ...


_db_pool: _AsyncPoolLike | None = None
_iteration_store: IterationStore | None = None
_workspace_base_path: str = DEFAULT_WORKSPACE_BASE_PATH


def set_db_pool(pool: _AsyncPoolLike) -> None:
    """Register the asyncpg-shaped pool for the default SQL store.

 Called once at worker boot (``automation_worker.main``). When a
 custom:class:`IterationStore` has been wired through:func:`set_iteration_store`, the pool is unused — but registering
 it anyway is harmless and keeps the boot script symmetric with
 the audit-prune wiring.
 """
    global _db_pool  # noqa: PLW0603
    _db_pool = pool


def get_db_pool() -> _AsyncPoolLike:
    """Resolve the registered pool or fail loudly.

 Surfaced as a clear ``RuntimeError`` so misconfiguration (forgot
 to call:func:`set_db_pool`) is obvious in worker logs rather
 than an ``AttributeError`` deep inside the SQL emitter.
 """
    if _db_pool is None:
        raise RuntimeError(
            "iteration_manager activity: db pool not initialised; "
            "call set_db_pool during worker startup or supply an "
            "IterationStore via set_iteration_store."
        )
    return _db_pool


def set_iteration_store(store: IterationStore | None) -> None:
    """Override the iteration persistence surface.

 Passing ``None`` reverts to the default:class:`PostgresIterationStore`
 backed by the pool registered through:func:`set_db_pool`. Tests
 use this to inject an in-memory fake; production typically does
 not call this setter.
 """
    global _iteration_store  # noqa: PLW0603
    _iteration_store = store


def get_iteration_store() -> IterationStore:
    """Resolve the active:class:`IterationStore`.

 When:func:`set_iteration_store` has been called with a non-``None``
 value, that store is returned directly. Otherwise the helper
 constructs an on-demand:class:`PostgresIterationStore` bound to
 the registered DB pool.
 """
    if _iteration_store is not None:
        return _iteration_store
    return PostgresIterationStore(pool=get_db_pool())


def set_workspace_base_path(base: str) -> None:
    """Override the workspace base path.

 Defaults to:data:`DEFAULT_WORKSPACE_BASE_PATH`. The boot script
 sets this from ``RUNNER_BASE_PATH`` (with the deprecated
 ``SSH_BASE_PATH`` alias) so workspace paths produced here line up
 with the execution-runner.
 """
    global _workspace_base_path  # noqa: PLW0603
    _workspace_base_path = base or DEFAULT_WORKSPACE_BASE_PATH


def get_workspace_base_path() -> str:
    """Return the currently configured workspace base path."""
    return _workspace_base_path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def is_iterate_command(comment_body: str | None) -> bool:
    """Return ``True`` if ``comment_body`` contains the ``[iterate]`` keyword.

 Matches the dispatcher's regex (case-insensitive). Useful for
 callers that want a shared definition of "iterate command" between
 the dispatcher and the activity.
 """
    if not comment_body:
        return False
    return bool(ITERATE_PATTERN.search(comment_body))


def extract_extra_instructions(comment_body: str | None) -> str | None:
    """Extract free-form instructions following the ``[iterate]`` keyword.

 The Jira comment shape is::

 [iterate] add exponential backoff to the retry helper

 Everything after the closing ``]`` is treated as the extra
 instruction. Leading / trailing whitespace is stripped. Multiple
 ``[iterate]`` markers are tolerated — only the *first* match is
 used as the anchor and the rest of the body (verbatim) becomes the
 instruction text.

 Returns ``None`` when the comment is empty, missing the keyword,
 or carries no text after it. /.
 """
    if not comment_body:
        return None
    match = ITERATE_PATTERN.search(comment_body)
    if match is None:
        return None
    remainder = comment_body[match.end():].strip()
    return remainder or None


def is_authorized_for_iterate(
    *,
    author_account_id: str,
    approvers: list[str],
    issue_reporter_account_id: str | None,
) -> bool:
    """Return ``True`` if the comment author may trigger a re-run.

 The contract is the union of two predicates:

 * ``author_account_id`` is in the department's ``approvers`` list, or
 * ``author_account_id`` equals the issue's ``reporter`` accountId.

 Empty strings never authorize anyone — a misconfigured webhook
 that drops the actor accountId must not silently grant access.
 """
    if not author_account_id:
        return False
    if author_account_id in approvers:
        return True
    if (
        issue_reporter_account_id
        and author_account_id == issue_reporter_account_id
    ):
        return True
    return False


def build_iteration_workspace_path(
    base: str,
    issue_key: str,
    iteration_number: int,
) -> str:
    """Return the canonical ``{base}/{issue_key}/iter-{N}`` path.

 Mirrors:func:`runners.workspace_path.build_workspace_path` from
 ``execution-runner-worker``. The two helpers must agree
 byte-for-byte: the iteration manager records the path in the
 ``workflow_iterations`` table and the runner expects to find the
 same string when it provisions the workspace on the SSH host.

 Raises ``ValueError`` when ``issue_key`` fails the Jira-style
 pattern (``^[A-Z][A-Z0-9_]*-\\d+$``) or ``iteration_number`` is
 not an int in ``[1, MAX_ITERATION_NUMBER]``. Booleans are rejected
 explicitly because ``isinstance(True, int)`` is ``True`` in Python
 and a boolean iteration number is almost certainly a caller bug.
 """
    if not isinstance(issue_key, str):
        raise ValueError(
            f"issue_key must be a string, got {type(issue_key).__name__}"
        )
    if _ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        raise ValueError(
            f"issue_key={issue_key!r} does not match "
            f"{_ISSUE_KEY_PATTERN.pattern!r}"
        )
    if isinstance(iteration_number, bool) or not isinstance(
        iteration_number, int
    ):
        raise ValueError(
            f"iteration_number must be an int, "
            f"got {type(iteration_number).__name__}"
        )
    if iteration_number < 1 or iteration_number > MAX_ITERATION_NUMBER:
        raise ValueError(
            f"iteration_number={iteration_number} out of range "
            f"[1, {MAX_ITERATION_NUMBER}]"
        )

    normalised_base = (
        base.rstrip("/") if isinstance(base, str) and base else base
    )
    return f"{normalised_base}/{issue_key}/iter-{iteration_number}"


# ---------------------------------------------------------------------------
# Default Postgres-backed IterationStore
# ---------------------------------------------------------------------------


@dataclass
class PostgresIterationStore:
    """Default:class:`IterationStore` backed by ``shared.workflow_iterations``.

 The class is intentionally tiny — it's mostly just two SQL
 statements bound to the column layout introduced by sibling 's migration. We declare it here (rather than waiting on the
 migration) so the activity can ship and be exercised end-to-end
 against a Postgres test container; if the migration is missing in
 a development environment the queries fail loudly with a
 ``"relation shared.workflow_iterations does not exist"`` Postgres
 error, which is the correct signal.
 """

    pool: _AsyncPoolLike

    async def latest_iteration(
        self, issue_key: str
    ) -> IterationRecord | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
 SELECT issue_key,
 iteration_number,
 workflow_id,
 previous_branch,
 previous_pr_id,
 workspace_path,
 status,
 created_at
 FROM shared.workflow_iterations
 WHERE issue_key = $1
 ORDER BY iteration_number DESC
 LIMIT 1
 """,
                issue_key,
            )
        if row is None:
            return None
        return IterationRecord(
            issue_key=row["issue_key"],
            iteration_number=int(row["iteration_number"]),
            workflow_id=str(row["workflow_id"]),
            previous_branch=(
                str(row["previous_branch"])
                if row["previous_branch"] is not None
                else None
            ),
            previous_pr_id=(
                int(row["previous_pr_id"])
                if row["previous_pr_id"] is not None
                else None
            ),
            workspace_path=str(row["workspace_path"]),
            status=str(row["status"]),
            created_at=row["created_at"],
        )

    async def insert_iteration(
        self,
        *,
        issue_key: str,
        iteration_number: int,
        workflow_id: str,
        previous_branch: str | None,
        previous_pr_id: int | None,
        workspace_path: str,
        status: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
 INSERT INTO shared.workflow_iterations (issue_key,
 iteration_number,
 workflow_id,
 previous_branch,
 previous_pr_id,
 workspace_path,
 status) VALUES ($1, $2, $3, $4, $5, $6, $7)
 """,
                issue_key,
                iteration_number,
                workflow_id,
                previous_branch,
                previous_pr_id,
                workspace_path,
                status,
            )


# ---------------------------------------------------------------------------
# Activity entry point
# ---------------------------------------------------------------------------


@activity.defn(name="prepare_iteration")
async def prepare_iteration(
    input: PrepareIterationInput,
) -> IterationContext:
    """Prepare the context for an ``[iterate]`` re-run.

 See module docstring for the full pipeline. Highlights:

 * Authorization is checked *before* any DB write so an unauthorized
 attempt leaves no trace in ``workflow_iterations``.
 * The new iteration number is the highest stored ``iteration_number``
 for ``issue_key`` plus one, defaulting to ``1`` when no prior
 row exists.
 * The workspace path is rendered through:func:`build_iteration_workspace_path`
 which guarantees uniqueness across iterations..
 """
    # Bind the inbound trace_id onto the activity's contextvar so log
    # records emitted by this activity carry the same trace_id as the
    # originating webhook. Falls back to a generated id when the
    # caller did not supply one.
    trace_id = input.trace_id or str(uuid.uuid4())
    if input.trace_id:
        try:  # pragma: no cover - defensive
            from observability import set_trace_id

            set_trace_id(input.trace_id)
        except Exception:  # pragma: no cover - defensive
            pass

    activity.logger.info(
        "iteration_manager: preparing iteration for %s "
        "(dept=%s, author=%s, trace=%s)",
        input.issue_key,
        input.dept_id,
        input.comment_author_account_id,
        trace_id,
    )

    # ------------------------------------------------------------------
    # Step 1 — Authorization gate 
    # ------------------------------------------------------------------
    approvers_raw = input.dept_config.get("approvers") if input.dept_config else None
    approvers: list[str] = (
        [str(a) for a in approvers_raw if isinstance(a, str)]
        if isinstance(approvers_raw, list)
        else []
    )

    if not is_authorized_for_iterate(
        author_account_id=input.comment_author_account_id,
        approvers=approvers,
        issue_reporter_account_id=input.issue_reporter_account_id,
    ):
        activity.logger.info(
            "iteration_manager: rejecting [iterate] from %s on %s — "
            "not in approvers and not the reporter",
            input.comment_author_account_id,
            input.issue_key,
        )
        return _unauthorized_result(
            input,
            reason="not_authorized",
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------
    # Step 2 — Load latest iteration (,)
    # ------------------------------------------------------------------
    store = get_iteration_store()
    latest = await store.latest_iteration(input.issue_key)
    previous_iteration_number = latest.iteration_number if latest else 0
    new_iteration_number = previous_iteration_number + 1

    # Storm-guard cap (independent of the workspace-path bound).
    # The cap is on the *current* iteration count for this issue:
    # once we already have ``MAX_ITERATIONS_PER_ISSUE`` rows we refuse
    # to allocate row N+1. The check sits between the latest-row read
    # and the path-bound check so a runaway ``[iterate]`` storm is
    # broken at 10, not at 999.
    if previous_iteration_number >= MAX_ITERATIONS_PER_ISSUE:
        activity.logger.warning(
            "prepare_iteration: max_iteration_exceeded for issue=%s "
            "current_count=%d cap=%d",
            input.issue_key,
            previous_iteration_number,
            MAX_ITERATIONS_PER_ISSUE,
        )
        return _unauthorized_result(
            input,
            reason="max_iteration_exceeded",
            trace_id=trace_id,
            current_count=previous_iteration_number,
        )

    if new_iteration_number > MAX_ITERATION_NUMBER:
        activity.logger.warning(
            "iteration_manager: %s has reached the max iteration "
            "count %d — refusing to start iter-%d",
            input.issue_key,
            MAX_ITERATION_NUMBER,
            new_iteration_number,
        )
        return _unauthorized_result(
            input,
            reason="max_iteration_exceeded",
            trace_id=trace_id,
            current_count=previous_iteration_number,
        )

    # ------------------------------------------------------------------
    # Step 3 — Workspace path (,)
    # ------------------------------------------------------------------
    try:
        workspace_path = build_iteration_workspace_path(
            get_workspace_base_path(),
            input.issue_key,
            new_iteration_number,
        )
    except ValueError as exc:
        activity.logger.warning(
            "iteration_manager: cannot build workspace path for "
            "%s iter-%d: %s",
            input.issue_key,
            new_iteration_number,
            exc,
        )
        return _unauthorized_result(
            input,
            reason="invalid_workspace_path",
            trace_id=trace_id,
        )

    # ------------------------------------------------------------------
    # Step 4 — Carry-over branch / PR (,)
    # ------------------------------------------------------------------
    previous_branch = latest.previous_branch if latest else None
    previous_pr_id = latest.previous_pr_id if latest else None
    # If the *latest* iteration row doesn't carry a branch/PR (eg. it
    # was a noop_test that never opened a PR) we still want to use its
    # own ``workspace_path`` references rather than reaching further
    # back in history. the "previous PR" is whichever PR is
    # *currently* tracked against the issue — the dispatcher records
    # exactly that on each iteration so the latest row is the source
    # of truth.

    # ------------------------------------------------------------------
    # Step 5 — Extra instructions 
    # ------------------------------------------------------------------
    extra_instructions = extract_extra_instructions(input.comment_body)

    # ------------------------------------------------------------------
    # Step 6 — Persist the new iteration row 
    # ------------------------------------------------------------------
    workflow_id = (
        f"iteration-{input.issue_key}-{new_iteration_number}-"
        f"{uuid.uuid4().hex[:8]}"
    )

    try:
        await store.insert_iteration(
            issue_key=input.issue_key,
            iteration_number=new_iteration_number,
            workflow_id=workflow_id,
            previous_branch=previous_branch,
            previous_pr_id=previous_pr_id,
            workspace_path=workspace_path,
            status="pending",
        )
    except Exception as exc:  # noqa: BLE001
        # The most likely failure here is a UNIQUE-constraint
        # violation from a racing ``[iterate]`` that grabbed
        # iteration_number first. We surface it as an
        # ``insert_failed`` reason so the workflow drops the request
        # without retrying — the user can re-issue ``[iterate]`` and
        # the next call sees a higher ``previous_iteration_number``.
        activity.logger.warning(
            "iteration_manager: failed to insert iteration row for "
            "%s iter-%d: %s",
            input.issue_key,
            new_iteration_number,
            exc,
        )
        return _unauthorized_result(
            input,
            reason="insert_failed",
            trace_id=trace_id,
        )

    activity.logger.info(
        "iteration_manager: prepared %s iter-%d "
        "(workspace=%s, prev_pr=%s, prev_branch=%s, "
        "extra_instructions=%s)",
        input.issue_key,
        new_iteration_number,
        workspace_path,
        previous_pr_id,
        previous_branch,
        bool(extra_instructions),
    )

    return IterationContext(
        authorized=True,
        reason="",
        issue_key=input.issue_key,
        iteration_number=new_iteration_number,
        workflow_id=workflow_id,
        workspace_path=workspace_path,
        previous_branch=previous_branch,
        previous_pr_id=previous_pr_id,
        extra_instructions=extra_instructions,
        dept_id=input.dept_id,
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unauthorized_result(
    input: PrepareIterationInput,
    *,
    reason: str,
    trace_id: str,
    current_count: int | None = None,
) -> IterationContext:
    """Build a *not-authorized* / *deny* result.

 Used by every early-return path so the shape of the deny response
 is identical regardless of the underlying reason. Callers
 distinguish via:attr:`IterationContext.reason`. The
 ``current_count`` argument is only meaningful for the
 storm-guard path (``max_iteration_exceeded``); every other
 rejection leaves it as ``None``.
 """
    return IterationContext(
        authorized=False,
        reason=reason,
        issue_key=input.issue_key,
        iteration_number=0,
        workflow_id="",
        workspace_path="",
        previous_branch=None,
        previous_pr_id=None,
        extra_instructions=None,
        dept_id=input.dept_id,
        trace_id=trace_id,
        current_count=current_count,
    )
