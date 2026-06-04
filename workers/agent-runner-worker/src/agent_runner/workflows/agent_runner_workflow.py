"""``AgentRunnerWorkflow`` — LLM + MCP iteration orchestrator.

Canonical home of the AgentRunnerWorkflow. The workflow runs on the
``agent-runner-tq`` task queue and is dispatched as a child by
:class:`automation_worker.workflows.automation_workflow.AutomationWorkflow`
once the capability gate plus branch-pattern routing have validated
the request.

Responsibilities:

    1. Maintain a replay-safe iteration state (``IterationState`` from
       :mod:`temporal_shared.messages`) carrying ``iter_count``,
       ``last_fix_trigger_at``, ``test_results_by_diff_hash``,
       ``explain_cache``, and ``needs_info_streak``. State evolves by
       :func:`dataclasses.replace`, never by in-place mutation, so every
       Temporal replay reaches the same value.
    2. Carry workflow-local sets/lists for cross-iteration dedup and
       partial-failure reporting:

           ``previous_findings: set[str]`` — PR review hash dedup
           ``confluence_section_hashes: set[str]`` — Confluence dedup
           ``output_actions_partial: list[str]`` — best-effort failures
           reported in the final summary

    3. Expose four signals — ``comment_added``, ``fix_triggered``,
       ``explain_triggered`` and ``cancel_requested`` — each of which
       calls a pre-condition (``should_advance_iter(state, MAX_ITER)``)
       *before* mutating state. When the cap is reached the workflow
       transitions to ``out_of_scope`` and refuses to advance further;
       any in-flight signal is ignored (state remains untouched).
    4. Drive the per-workflow-type body via activities. The full body
       (code-change / pr_review / confluence / research / multi_step),
       with signals, state, and the iteration cap exercised end-to-end.

Replay-safety invariants:

* No ``datetime.now()``, ``time.time()``, ``random.*``, ``uuid.uuid4()``
  in the workflow body — only ``workflow.now()`` and the activities
  emit timestamps.
* No direct ``httpx`` / ``requests`` / ``aiohttp`` / ``openai`` /
  ``anthropic`` calls — every side effect is wrapped in an activity.
* Any module that performs I/O is imported inside the
  ``workflow.unsafe.imports_passed_through()`` sandbox-escape block.

Forward-compatibility note:

The pure helpers ``should_advance_iter``, ``is_fix_debounced``,
``fix_should_skip_retest``, ``explain_should_skip_llm``,
``needs_info_should_terminate`` are scheduled to land in
:mod:`temporal_shared.iteration`. Until that module exists
this file imports them lazily inside ``workflow.unsafe.imports_passed_through()``;
when the import fails we fall back to inline placeholder functions that
encode the same contract. Once that module ships the placeholders go
unused automatically — no rewrite required.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

# ---------------------------------------------------------------------------
# Sandbox-safe imports for shared messages + iteration helpers
#
# ``workflow.unsafe.imports_passed_through()`` is the documented escape
# hatch for importing modules whose transitive dependencies (httpx,
# asyncpg, …) would otherwise trip the Temporal sandbox. The static
# determinism tests explicitly tolerate this
# with-block.
# ---------------------------------------------------------------------------

with workflow.unsafe.imports_passed_through():
    from temporal_shared.messages import (
        AgentRunnerStatus,
        AgentRunnerWorkflowInput,
        AgentRunnerWorkflowOutput,
        ExecutionRunWorkflowInput,
        ExecutionRunWorkflowOutput,
        ExplainCacheEntry,
        IterationState,
        OutputAction,
    )

    # Pure formatters / routers used by the ``code_change_*`` flow.
    # All three modules are ``@dataclass``/pure-function only, so they
    # are safe to import inside the workflow sandbox.
    from temporal_shared.branch_rules import (
        DEFAULT_BRANCH_PATTERN_RULES,
        route_by_branch_pattern,
    )
    from temporal_shared.code_change import (
        compute_branch_name,
        format_commit_message,
    )
    from temporal_shared.confluence import (
        compute_provenance_footer,
        format_page_title,
    )
    from temporal_shared.confluence_dedup import (
        AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
        AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
        is_probe_page,
        should_skip_overwrite,
        should_skip_section_update,
    )
    from temporal_shared.output_actions import (
        ApplyResult,
        partition as partition_output_actions,
    )
    from temporal_shared.output_size_cap import (
        format_final_jira_comment,
        redirect_oversized_payload,
    )
    from temporal_shared.research import (
        format_research_publish_confluence_body,
        format_research_summary_jira_comment,
    )
    from mcp_client.deployment_router import select_pr_create_tool

    # Until ``temporal_shared.llm_dedup`` is available, inline a trivial
    # set-difference helper. When the module appears the import below
    # shadows the placeholder automatically.
    # TODO: remove the placeholder once
    # ``temporal_shared.llm_dedup.dedup_findings`` is available.
    try:
        from temporal_shared.llm_dedup import (  # type: ignore[import-not-found]
            dedup_findings as _dedup_findings_impl,
        )

        _LLM_DEDUP_MODULE_AVAILABLE = True
    except ImportError:  # pragma: no cover - covered once the module lands
        _LLM_DEDUP_MODULE_AVAILABLE = False
        _dedup_findings_impl = None  # type: ignore[assignment]

    # Iteration helpers live in ``temporal_shared.iteration``.
    # Until that module exists we fall back to local placeholder
    # implementations that encode the same runtime contract.
    try:
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            explain_should_skip_llm as _explain_should_skip_llm_impl,
        )
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            fix_should_skip_retest as _fix_should_skip_retest_impl,
        )
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            is_fix_debounced as _is_fix_debounced_impl,
        )
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            needs_info_should_terminate as _needs_info_should_terminate_impl,
        )
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            should_advance_iter as _should_advance_iter_impl,
        )

        _ITERATION_MODULE_AVAILABLE = True
    except ImportError:  # pragma: no cover - covered once the module lands
        _ITERATION_MODULE_AVAILABLE = False
        _should_advance_iter_impl = None  # type: ignore[assignment]
        _is_fix_debounced_impl = None  # type: ignore[assignment]
        _fix_should_skip_retest_impl = None  # type: ignore[assignment]
        _explain_should_skip_llm_impl = None  # type: ignore[assignment]
        _needs_info_should_terminate_impl = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants — magic numbers the workflow consults each iteration
# ---------------------------------------------------------------------------

#: Default activity timeout for short Atlassian / DB calls.
_SHORT_TIMEOUT: timedelta = timedelta(minutes=2)

#: Default activity timeout for LLM calls (PR review / explain / research).
_LLM_TIMEOUT: timedelta = timedelta(minutes=5)

#: Execution-timeout budget for each Epic subtask child workflow in the
#: multi_step fan-out. Generous enough for a full per-subtask automation
#: run (analysis + code/PR or research) while bounding a stuck child.
_EPIC_SUBTASK_TIMEOUT: timedelta = timedelta(minutes=30)

#: Default retry policy for short side-effecting activities.
_DEFAULT_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)

#: Hard cap on iterations. The workflow input may
#: lower this — but we treat the input value as advisory and clamp to
#: this constant so a misconfigured caller can't bypass the cap.
MAX_ITER: int = 5

#: ``[fix]`` debounce window.
FIX_DEBOUNCE_WINDOW: timedelta = timedelta(seconds=60)

#: ``[explain]`` cache TTL.
EXPLAIN_CACHE_TTL: timedelta = timedelta(minutes=5)

#: ``needs_info`` consecutive-comment cap.
NEEDS_INFO_MAX_STREAK: int = 3

#: Wall-clock budget the workflow waits for a signal before terminating
#: with ``out_of_scope``. Implemented via ``workflow.wait_condition`` so
#: replay determinism holds.
SIGNAL_WAIT_TIMEOUT: timedelta = timedelta(days=7)


#: Activity-level token cap. Any LLM activity
#: whose ``input_tokens`` argument exceeds this value is aborted before
#: the network call so the platform never spends compute on an over-cap
#: prompt. The cap is enforced at the *workflow* layer (not the
#: activity) so the audit trail records the refusal next to the
#: workflow id; the activity body itself can stay cap-agnostic.
MAX_ACTIVITY_TOKEN_CAP: int = 8000

#: Stable error type discriminator emitted by
#: :class:`TokenCapExceededError`. Surfaces in audit events and in the
#: ``ApplicationError.type`` field consumed by Temporal's failure
#: handling.
TOKEN_CAP_ERROR_TYPE: str = "TokenCapExceeded"

#: Stable audit action emitted when the token cap fires.
TOKEN_CAP_AUDIT_ACTION: str = "token_cap_exceeded"

#: Stable audit action emitted when the iter==3 banner fires the very
#: first time.
ITER_WARNING_AUDIT_ACTION: str = "iter_warning_at_three_posted"

#: Stable audit action emitted when ``[fix]`` is silently dropped by the
#: 60-second debounce window.
FIX_DEBOUNCE_AUDIT_ACTION: str = "fix_debounce_dropped"

#: Stable audit action emitted when ``[fix]`` re-uses a cached test
#: result instead of re-running the ExecutionRunWorkflow.
FIX_RETEST_PROTECTED_AUDIT_ACTION: str = "fix_re_test_protected"

#: Stable audit action emitted when ``[explain]`` is served from the
#: 5-minute LRU cache instead of calling the LLM.
EXPLAIN_CACHE_HIT_AUDIT_ACTION: str = "explain_cache_hit"

#: Stable audit action emitted when a Confluence update target is a
#: ``_AI_PROBE_*`` foundation probe page.
#: The bot must never overwrite a probe artifact, so the page is
#: filtered out of the update queue and an audit row is written so
#: operators can see the skip surfaced alongside the workflow run.
CONFLUENCE_PROBE_PAGE_SKIPPED_AUDIT_ACTION: str = (
    "confluence_probe_page_skipped"
)

#: Stable audit action emitted when a workflow is cancelled by an
#: end-user actor. Used by
#: :meth:`AgentRunnerWorkflow._handle_cancel` after the compensation
#: chain completes so the audit trail records who initiated the
#: cancel without leaking the OIDC subject into the workflow body.
CANCEL_BY_END_USER_AUDIT_ACTION: str = "workflow_cancelled_by_end_user"

#: Stable audit action emitted when a workflow is cancelled by an
#: admin / dept_admin actor. Mirrors
#: :data:`CANCEL_BY_END_USER_AUDIT_ACTION`; the workflow consults the
#: cancel signal's ``actor_role`` to decide which audit action to
#: emit.
CANCEL_BY_ADMIN_AUDIT_ACTION: str = "workflow_cancelled_by_admin"

#: Closed vocabulary of cancel ``actor_role`` values recognised by the
#: cancel signal handler. ``end_user`` maps to
#: :data:`CANCEL_BY_END_USER_AUDIT_ACTION`; ``admin`` and
#: ``dept_admin`` map to :data:`CANCEL_BY_ADMIN_AUDIT_ACTION`. Any
#: other value (including ``None`` / empty string) defaults to
#: ``end_user`` ("if not in the closed set, default to
#: workflow_cancelled_by_end_user").
_CANCEL_ROLE_END_USER: Final[str] = "end_user"
_CANCEL_ROLE_ADMIN: Final[str] = "admin"
_CANCEL_ROLE_DEPT_ADMIN: Final[str] = "dept_admin"
_CANCEL_ADMIN_ROLES: Final[frozenset[str]] = frozenset(
    {_CANCEL_ROLE_ADMIN, _CANCEL_ROLE_DEPT_ADMIN}
)
_CANCEL_RECOGNISED_ROLES: Final[frozenset[str]] = (
    _CANCEL_ADMIN_ROLES | frozenset({_CANCEL_ROLE_END_USER})
)


def _audit_action_for_cancel_role(actor_role: str | None) -> str:
    """Map a cancel signal ``actor_role`` to the matching audit action.

    Pure helper (no I/O, no clock, no randomness) — safe to call from
    the signal handler. Returns
    :data:`CANCEL_BY_ADMIN_AUDIT_ACTION` for ``admin`` / ``dept_admin``,
    :data:`CANCEL_BY_END_USER_AUDIT_ACTION` for everything else,
    including unknown / empty / ``None``.
    """

    if isinstance(actor_role, str) and actor_role in _CANCEL_ADMIN_ROLES:
        return CANCEL_BY_ADMIN_AUDIT_ACTION
    return CANCEL_BY_END_USER_AUDIT_ACTION

#: Iteration count at which the banner-once warning fires; banner
#: state field flips ``iter_warning_at_three=True``).
ITER_WARNING_THRESHOLD: int = 3

#: Banner text mirrored into Jira when ``iter_count >= ITER_WARNING_THRESHOLD``
#: for the first time.
ITER_WARNING_BANNER_TEXT: str = (
    "⚠️ Bu görev şu ana kadar 3 iterasyon sürdü. Yeni bir task açmayı "
    "düşünün — devam ederseniz iterasyon sayısı 5'i bulduğunda iş otomatik "
    "olarak kapatılacaktır."
)

#: Retry policy for LLM activities that participate in the token cap
#: path. ``maximum_attempts=1`` guarantees fail-fast: a
#: :class:`TokenCapExceededError` raised inside the activity (or
#: pre-flighted by :meth:`AgentRunnerWorkflow._execute_llm_activity`)
#: never triggers a retry, ensuring the platform does not spend tokens
#: on a known-overflow prompt.
LLM_RETRY_POLICY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=1,
)


#: Stable failure-reason discriminator emitted when a critical output
#: action fails during ``_execute_output_actions``. The body
#: traps an internal :class:`_OutputActionCriticalFailure` exception
#: and routes the workflow into the cancel/compensation path with
#: this stable category in the workflow output.
OUTPUT_ACTION_CRITICAL_FAILED_REASON: Final[str] = (
    "output_action_critical_failed"
)


#: Activity name dispatch table for :class:`OutputAction.kind` values
#: emitted by activity ``output_actions`` tuples. The
#: workflow body invokes the matching activity via
#: :func:`workflow.execute_activity` after the size-cap helper has
#: optionally redirected oversized payloads to MinIO.
#:
#: The mapping is ``MappingProxyType``-wrapped so a stray attempt to
#: mutate the table at runtime raises ``TypeError`` rather than
#: silently corrupting the dispatch.
_OUTPUT_ACTION_DISPATCH: Final = MappingProxyType(
    {
        "jira_comment": "jira_add_comment",
        "jira_attachment": "upload_artifact_to_jira",
        "bitbucket_create_pr": "bitbucket_open_pr",
        "confluence_create_page": "confluence_create_page",
        "confluence_update_page": "confluence_update_page",
        "slack_notify": "slack_notify",
        "email_notify": "email_notify",
    }
)


# ---------------------------------------------------------------------------
# Keyword markers in comment bodies
#
# The webhook gateway is responsible for routing ``[fix]`` / ``[explain]``
# comments into the dedicated signals (``fix_triggered`` /
# ``explain_triggered``) — but ``comment_added`` also accepts keyword
# markers verbatim so that direct-signal tests, the Streamlit inline
# reply path, and any future caller that signals only this workflow can
# rely on a single entrypoint.
#
# Markers are matched case-insensitively and require enclosing square
# brackets so a free-form comment that happens to mention the word
# "fix" / "explain" / "needs info" is **not** treated as a trigger.
# ---------------------------------------------------------------------------

_FIX_KEYWORD_RE: re.Pattern[str] = re.compile(r"\[fix\]", re.IGNORECASE)
_EXPLAIN_KEYWORD_RE: re.Pattern[str] = re.compile(
    r"\[explain\]", re.IGNORECASE
)
_NEEDS_INFO_KEYWORD_RE: re.Pattern[str] = re.compile(
    r"\[needs[_-]?info\]", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Signal payload dataclasses
#
# Every signal carries a frozen dataclass so the wire shape is stable
# across replays and SDK versions. The handlers also accept raw dicts
# as a defensive fallback (Temporal converts unknown JSON payloads via
# the data converter, which usually yields a dict in tests).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentAddedSignal:
    """Payload for the ``comment_added`` signal.

    Forwarded by the webhook gateway whenever a Jira / Bitbucket comment
    passes the filter chain. The workflow consults
    :func:`should_advance_iter` before incrementing ``iter_count`` so
    MAX_ITER is honoured.

    Attributes
    ----------
    comment_text:
        Plain-text body of the comment (already debounce-coalesced by
        the gateway when applicable).
    actor_account_id:
        Atlassian ``account_id`` of the commenter — stored for audit
        only; the loop guard runs on the gateway side.
    diff_hash:
        Optional hash of the current diff at the time the comment was
        posted; populated by the gateway for ``[fix]`` re-test
        protection and by ``[explain]`` cache lookup.
    """

    comment_text: str = ""
    actor_account_id: str | None = None
    diff_hash: str | None = None


@dataclass(frozen=True)
class FixTriggeredSignal:
    """Payload for the ``[fix]`` keyword signal."""

    comment_text: str = ""
    actor_account_id: str | None = None
    diff_hash: str | None = None


@dataclass(frozen=True)
class ExplainTriggeredSignal:
    """Payload for the ``[explain]`` keyword signal."""

    comment_text: str = ""
    actor_account_id: str | None = None
    pr_diff_hash: str | None = None


@dataclass(frozen=True)
class CancelRequestedSignal:
    """Payload for the cancel signal.

    Carries enough metadata for the audit log to distinguish
    ``workflow_cancelled_by_end_user`` from ``workflow_cancelled_by_admin``
    without having to re-resolve the actor at compensation time.

    Attributes
    ----------
    actor_id:
        OIDC subject of the cancelling user (Atlassian
        ``account_id`` or platform ``user_id``).
    actor_role:
        Role of the cancelling user — one of ``end_user``, ``admin``,
        or ``dept_admin``. Drives the audit action selected by
        :func:`_audit_action_for_cancel_role`. Unknown / blank values
        default to ``end_user``.
    reason:
        Free-form cancel reason carried verbatim into the
        compensation context and the audit payload. Defaults to
        ``user_cancel`` so legacy callers that omit the field still
        produce a meaningful audit row.
    """

    actor_id: str = ""
    actor_role: str = _CANCEL_ROLE_END_USER
    reason: str = "user_cancel"


# ---------------------------------------------------------------------------
# TokenCapExceededError — non-retryable application error
# ---------------------------------------------------------------------------


class TokenCapExceededError(ApplicationError):
    """Raised when an LLM activity's ``input_tokens`` exceeds the cap.

    Subclasses :class:`temporalio.exceptions.ApplicationError` with
    ``non_retryable=True`` so Temporal's retry machinery treats the
    failure as terminal — no second attempt is scheduled, mirroring
    the fail-fast contract.

    The error type is :data:`TOKEN_CAP_ERROR_TYPE` so the workflow body
    (and any external observer of the workflow event history) can
    discriminate it from generic activity failures.

    The accompanying audit event (:data:`TOKEN_CAP_AUDIT_ACTION`) is
    emitted by the workflow body **before** raising — so even if the
    audit-emit activity itself fails, the workflow's terminal status
    still reflects the cap-exceeded condition.
    """

    def __init__(
        self,
        *,
        activity_name: str,
        input_tokens: int,
        cap: int = MAX_ACTIVITY_TOKEN_CAP,
    ) -> None:
        message = (
            f"LLM activity {activity_name!r} input tokens "
            f"({input_tokens}) exceed cap ({cap}); fail-fast (no retry)."
        )
        super().__init__(
            message,
            type=TOKEN_CAP_ERROR_TYPE,
            non_retryable=True,
        )
        self.activity_name = activity_name
        self.input_tokens = input_tokens
        self.cap = cap


# ---------------------------------------------------------------------------
# Pure helpers — placeholders for the not-yet-shipped temporal_shared.iteration
# module.
#
# These mirror the runtime contract for ``temporal_shared.iteration``.
# Once :mod:`temporal_shared.iteration` lands these placeholders go
# unused automatically — the production helpers take precedence in
# :func:`_should_advance_iter` and friends below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IterDecision:
    """Internal mirror of the iteration decision shape."""

    advance: bool
    reason: str = ""


def _placeholder_should_advance_iter(
    state: IterationState, max_iter: int
) -> _IterDecision:
    """Pure pre-condition: may we advance to ``state.iter_count + 1``?

    The decision is purely arithmetic — no clock, no randomness — so
    the workflow can call it from inside a signal handler without
    breaking replay determinism.
    """

    if max_iter <= 0:
        return _IterDecision(advance=False, reason="non_positive_max_iter")
    if state.iter_count >= max_iter:
        return _IterDecision(advance=False, reason="max_iter_reached")
    return _IterDecision(advance=True, reason="ok")


def _placeholder_is_fix_debounced(
    state: IterationState,
    now: datetime,
    window: timedelta = FIX_DEBOUNCE_WINDOW,
) -> bool:
    """Pure: True iff the last ``[fix]`` was less than ``window`` ago."""

    last = state.last_fix_trigger_at
    if last is None:
        return False
    return (now - last) < window


def _placeholder_fix_should_skip_retest(
    state: IterationState, current_diff_hash: str
) -> bool:
    """Pure: True iff we already have a test result for this diff."""

    if not current_diff_hash:
        return False
    return current_diff_hash in state.test_results_by_diff_hash


def _placeholder_explain_should_skip_llm(
    state: IterationState,
    pr_diff_hash: str,
    now: datetime,
    ttl: timedelta = EXPLAIN_CACHE_TTL,
) -> bool:
    """Pure: True iff a cached ``[explain]`` answer is still fresh."""

    if not pr_diff_hash:
        return False
    entry = state.explain_cache.get(pr_diff_hash)
    if entry is None:
        return False
    return (now - entry.issued_at) < ttl


def _placeholder_needs_info_should_terminate(
    state: IterationState, max_streak: int = NEEDS_INFO_MAX_STREAK
) -> bool:
    """Pure: True iff we've hit the ``needs_info`` consecutive-cap."""

    return state.needs_info_streak >= max_streak


def _should_advance_iter(state: IterationState, max_iter: int) -> _IterDecision:
    """Dispatch to ``temporal_shared.iteration`` if available, else placeholder."""

    if _ITERATION_MODULE_AVAILABLE and _should_advance_iter_impl is not None:
        return _should_advance_iter_impl(state, max_iter)  # type: ignore[misc]
    return _placeholder_should_advance_iter(state, max_iter)


def _is_fix_debounced(
    state: IterationState, now: datetime, window: timedelta = FIX_DEBOUNCE_WINDOW
) -> bool:
    if _ITERATION_MODULE_AVAILABLE and _is_fix_debounced_impl is not None:
        return _is_fix_debounced_impl(state, now, window)  # type: ignore[misc]
    return _placeholder_is_fix_debounced(state, now, window)


def _fix_should_skip_retest(state: IterationState, current_diff_hash: str) -> bool:
    if _ITERATION_MODULE_AVAILABLE and _fix_should_skip_retest_impl is not None:
        return _fix_should_skip_retest_impl(state, current_diff_hash)  # type: ignore[misc]
    return _placeholder_fix_should_skip_retest(state, current_diff_hash)


def _explain_should_skip_llm(
    state: IterationState,
    pr_diff_hash: str,
    now: datetime,
    ttl: timedelta = EXPLAIN_CACHE_TTL,
) -> bool:
    if _ITERATION_MODULE_AVAILABLE and _explain_should_skip_llm_impl is not None:
        return _explain_should_skip_llm_impl(state, pr_diff_hash, now, ttl)  # type: ignore[misc]
    return _placeholder_explain_should_skip_llm(state, pr_diff_hash, now, ttl)


def _needs_info_should_terminate(
    state: IterationState, max_streak: int = NEEDS_INFO_MAX_STREAK
) -> bool:
    if _ITERATION_MODULE_AVAILABLE and _needs_info_should_terminate_impl is not None:
        return _needs_info_should_terminate_impl(state, max_streak)  # type: ignore[misc]
    return _placeholder_needs_info_should_terminate(state, max_streak)


# ---------------------------------------------------------------------------
# Functional update helpers — keep ``IterationState`` evolutions explicit
# and side-effect free.
# ---------------------------------------------------------------------------


def _state_increment_iter(state: IterationState) -> IterationState:
    """Return a new state with ``iter_count`` advanced by one."""

    return dataclasses.replace(state, iter_count=state.iter_count + 1)


def _state_record_fix_trigger(
    state: IterationState, now: datetime
) -> IterationState:
    """Return a new state with ``last_fix_trigger_at`` set to ``now``."""

    return dataclasses.replace(state, last_fix_trigger_at=now)


def _state_record_explain_answer(
    state: IterationState, pr_diff_hash: str, answer: str, now: datetime
) -> IterationState:
    """Return a new state with the ``[explain]`` cache extended."""

    cache = dict(state.explain_cache)
    cache[pr_diff_hash] = ExplainCacheEntry(answer=answer, issued_at=now)
    return dataclasses.replace(state, explain_cache=cache)


def _state_increment_needs_info(state: IterationState) -> IterationState:
    """Return a new state with ``needs_info_streak`` advanced by one."""

    return dataclasses.replace(
        state, needs_info_streak=state.needs_info_streak + 1
    )


def _state_reset_needs_info(state: IterationState) -> IterationState:
    """Return a new state with ``needs_info_streak`` cleared."""

    return dataclasses.replace(state, needs_info_streak=0)


def _state_record_test_result(
    state: IterationState, diff_hash: str, status: str
) -> IterationState:
    """Return a new state caching the test outcome for ``diff_hash``.

    Used by the ``code_change_with_test`` flow so a follow-up ``[fix]``
    against an unchanged diff hits the re-test guard
    (:func:`_fix_should_skip_retest`) instead of running the
    ``ExecutionRunWorkflow`` child a second time.
    """

    if not diff_hash:
        return state
    cache = dict(state.test_results_by_diff_hash)
    cache[diff_hash] = status  # type: ignore[assignment]
    return dataclasses.replace(state, test_results_by_diff_hash=cache)


def _dedup_findings(
    previous_hashes: set[str], current_findings: list[dict]
) -> list[dict]:
    """Return ``current_findings`` minus any entry whose ``hash`` was seen.

    Pure helper; placeholder until :func:`temporal_shared.llm_dedup.dedup_findings`
    lands. Each finding is expected to carry a stable
    ``hash`` field — usually a sha256 of the rendered finding body —
    so the set difference reliably suppresses repeat content across
    iterations.

    The placeholder mirrors the contract of the eventual module-level
    helper: the input list order is preserved, only entries whose
    ``hash`` is **not** already in *previous_hashes* survive, and the
    function never mutates its arguments.
    """

    if _LLM_DEDUP_MODULE_AVAILABLE and _dedup_findings_impl is not None:
        return _dedup_findings_impl(  # type: ignore[no-any-return]
            previous_hashes, current_findings
        )
    new_findings: list[dict] = []
    for finding in current_findings:
        finding_hash = finding.get("hash") if isinstance(finding, dict) else None
        if not finding_hash or finding_hash in previous_hashes:
            continue
        new_findings.append(finding)
    return new_findings


# ---------------------------------------------------------------------------
# AgentRunnerWorkflow
# ---------------------------------------------------------------------------


@workflow.defn(name="AgentRunnerWorkflow")
class AgentRunnerWorkflow:
    """LLM + MCP iteration orchestrator.

    The workflow body sets up the iteration state, registers the four signals
    required by the workflow control rules, and waits for either the per-workflow-type
    body to complete or a cancel signal. Concrete per-workflow-type
    bodies (code_change, pr_review, confluence, research, multi_step)
    land in tasks 7-10 and plug into ``_dispatch_workflow_type`` below.
    """

    def __init__(self) -> None:
        # Iteration state — frozen dataclass; we evolve it via
        # ``dataclasses.replace`` so every replay reaches the same
        # value.
        self._iteration_state: IterationState = IterationState()

        # PR-review finding hashes seen in *previous* iterations. The
        # ``pr_review`` body consults this set before posting comments
        # so the same finding is never re-posted in iter-N.
        self._previous_findings: set[str] = set()

        # Confluence section content hashes written in *previous*
        # iterations. The ``confluence_doc_update`` body consults this
        # set before updating a section so identical content is never
        # re-written.
        self._confluence_section_hashes: set[str] = set()

        # Best-effort actions that failed during this run. Reported in
        # the final Jira comment.
        self._output_actions_partial: list[str] = []

        # Edge flag flipped by signal handlers; consumed by the
        # ``run`` body's ``wait_condition`` predicate.
        self._signal_pending: bool = False

        # Set to True by the ``cancel_requested`` signal; the body
        # observes this in its wait predicate and runs the
        # compensation chain.
        self._cancel_requested: bool = False
        self._cancel_actor_id: str = ""
        self._cancel_actor_role: str = _CANCEL_ROLE_END_USER
        self._cancel_reason: str = "user_cancel"

        # Set to True by :meth:`_handle_cancel` *before* invoking the
        # compensation chain activity. Idempotency latch: a second
        # ``cancel_requested`` signal that
        # arrives while the chain is mid-flight observes this flag
        # and short-circuits, so compensation never double-fires.
        # The flag stays set for the lifetime of the workflow so a
        # cancel that arrives *after* the chain completes is also a
        # no-op (terminal state — the workflow is about to close).
        self._compensation_running: bool = False

        # Set to True once the iteration cap (or needs_info cap) is
        # reached; subsequent signals become no-ops.
        self._out_of_scope: bool = False

        # Stable failure category to surface in the workflow output
        # when the run terminates non-successfully.
        self._failure_reason: str | None = None

        # Pending ``[fix]`` / ``[explain]`` invocation parameters —
        # consumed by the body the next time it advances. Populated by
        # the corresponding signal handler when the pre-conditions hold.
        self._pending_fix_diff_hash: str | None = None
        self._pending_explain_diff_hash: str | None = None
        self._pending_explain_text: str | None = None

        # Latest comment received via ``comment_added`` — feeds back
        # into the next iteration.
        self._latest_comment: str = ""

        # Once-per-workflow flag: ``True`` after the iter==3 warning
        # banner has been posted to Jira.
        # Idempotent — the body inspects this flag and a parallel
        # ``_iter_warning_pending`` edge to decide whether to call the
        # ``jira_add_comment`` activity. Once set the flag never flips
        # back, so re-entry into the iter==3 region during a future
        # iteration leaves the banner untouched.
        self._iter_warning_at_three: bool = False

        # Edge flag flipped by signal handlers when ``iter_count``
        # crosses :data:`ITER_WARNING_THRESHOLD` for the first time.
        # Drained by :meth:`_maybe_post_iter_warning_banner` after the
        # ``jira_add_comment`` activity has been invoked.
        self._iter_warning_pending: bool = False

        # Audit actions queued by signal handlers to be drained by the
        # body via :meth:`_drain_pending_audits`. Signal handlers must
        # not call activities directly (Temporal forbids ``await`` in a
        # signal handler), so we accumulate audit action strings here
        # and emit them on the next loop turn. The list is bounded by
        # the number of signals received, which is itself bounded by
        # :data:`MAX_ITER` plus a small constant — Temporal history
        # growth is therefore O(MAX_ITER), well within the hard caps.
        self._pending_audit_actions: list[str] = []

        # Per-workflow LRU for ``code_change_commit_only`` diff
        # summaries. When the shared module ships this attribute will be
        # replaced by a delegate to ``temporal_shared.llm_dedup``.
        # The cache is keyed by commit / diff hash and survives across
        # iterations of the same workflow execution, so a follow-up
        # ``[fix]`` against an unchanged diff hits the cache and skips
        # the LLM round-trip.
        self._diff_summary_cache: dict[str, str] = {}

        # Previous iteration's draft PR id. Updated
        # after each successful ``code_change_with_test`` run so a
        # follow-up iteration can mark the prior PR as superseded
        # via :func:`iter_advance_pr_supersede`. ``None`` until the
        # first PR opens.
        self._previous_pr_id: int | None = None

        # Latest Confluence page id created/updated by this workflow.
        # Populated by the ``confluence_doc_create`` body
        # so the terminal :class:`AgentRunnerWorkflowOutput` can
        # surface it via ``confluence_page_id`` and downstream PO
        # tooling can deep-link to the page. ``None`` when
        # no page operation has happened yet during this run.
        self._latest_confluence_page_id: str | None = None

        # Cumulative output-actions apply log.
        # Each ``_execute_output_actions`` invocation merges its
        # per-call :class:`ApplyResult` into this aggregate so the
        # final Jira comment composed by ``run`` can name every
        # critical action that succeeded and every best-effort
        # action that failed across the lifetime of the workflow.
        # The instance is reset only at workflow start (the constructor
        # creates a fresh :class:`ApplyResult`) so replays observe the
        # same merged state at every step.
        self._output_actions_log: ApplyResult = ApplyResult()

    # -- Queries -----------------------------------------------------------

    @workflow.query
    def is_iter_warning_at_three(self) -> bool:
        """Whether the iter==3 banner has already been posted."""

        return self._iter_warning_at_three

    @workflow.query
    def get_iteration_state(self) -> IterationState:
        """Expose the current :class:`IterationState` for observers."""

        return self._iteration_state

    @workflow.query
    def get_previous_findings(self) -> tuple[str, ...]:
        """Sorted snapshot of PR-review finding hashes seen so far."""

        return tuple(sorted(self._previous_findings))

    @workflow.query
    def get_confluence_section_hashes(self) -> tuple[str, ...]:
        """Sorted snapshot of Confluence section hashes written so far."""

        return tuple(sorted(self._confluence_section_hashes))

    @workflow.query
    def get_output_actions_partial(self) -> tuple[str, ...]:
        """Best-effort actions that failed during this run."""

        return tuple(self._output_actions_partial)

    @workflow.query
    def is_out_of_scope(self) -> bool:
        """Whether the workflow has hit the iter / needs_info cap."""

        return self._out_of_scope

    @workflow.query
    def is_cancel_requested(self) -> bool:
        """Whether a ``cancel_requested`` signal has been received."""

        return self._cancel_requested

    @workflow.query
    def is_compensation_running(self) -> bool:
        """Whether the compensation chain has been dispatched.

        Latched by :meth:`_handle_cancel` immediately before the
        activity call; observable so external probes can confirm the
        idempotency contract (a second cancel during the chain is a
        no-op).
        """

        return self._compensation_running

    # -- Signal handlers ---------------------------------------------------
    #
    # Every handler runs the ``should_advance_iter`` pre-condition before
    # mutating state. When the pre-condition denies, the handler flips
    # ``_out_of_scope`` and leaves the state untouched — the body's wait
    # predicate then drains and the workflow terminates with
    # ``out_of_scope``.
    #
    # Signal handlers MUST NOT perform I/O (Temporal restricts await on
    # activities inside signal handlers). They mutate workflow state and
    # flip the ``_signal_pending`` edge so the run body can pick up the
    # change on its next loop turn.

    @workflow.signal
    def comment_added(self, payload: Any) -> None:
        """Receive a forwarded comment event.

        Inspects the comment body for keyword markers (``[fix]``,
        ``[explain]``, ``[needs_info]``) and dispatches to the
        corresponding internal handler. Plain comments fall through
        to the default "advance iter" path. The dispatch is
        deterministic (a pure function of ``comment_text``) so replays
        reach the same routing decision.

        """

        if self._cancel_requested or self._out_of_scope:
            return

        text, actor, diff_hash = self._coerce_comment_signal(
            payload, "comment_text"
        )

        # ----- Keyword routing ------------------------------------
        # The webhook gateway is supposed to fire ``fix_triggered`` /
        # ``explain_triggered`` directly when it spots the markers,
        # but ``comment_added`` accepts the markers verbatim so direct
        # signals stay correct. The order below matters: ``[fix]``
        # before ``[explain]`` so a "[fix] [explain]" combo prefers
        # the more side-effecting branch (fix re-runs tests).
        if _FIX_KEYWORD_RE.search(text):
            self._apply_fix_signal(text=text, diff_hash=diff_hash)
            del actor
            return
        if _EXPLAIN_KEYWORD_RE.search(text):
            self._apply_explain_signal(text=text, pr_diff_hash=diff_hash)
            del actor
            return
        if _NEEDS_INFO_KEYWORD_RE.search(text):
            self._apply_needs_info_signal(text=text)
            del actor
            return

        # ----- Plain comment path ---------------------------------
        # Receiving a non-``needs_info`` comment resets the streak —
        # the user has supplied additional context, so the loop cap
        # restarts from zero.
        self._iteration_state = _state_reset_needs_info(self._iteration_state)

        # Pre-condition: still allowed to advance?
        decision = _should_advance_iter(self._iteration_state, MAX_ITER)
        if not decision.advance:
            self._out_of_scope = True
            self._failure_reason = decision.reason or "max_iter_reached"
            self._signal_pending = True
            return

        if text:
            self._latest_comment = text
        self._advance_iter_with_banner_check()
        # Note: ``actor`` is not surfaced as workflow state — it only
        # exists so the audit chain can log the originator. We accept
        # the value to keep the wire schema stable but intentionally
        # avoid storing PII inside the workflow state
        # adjacent concerns).
        del actor, diff_hash

    @workflow.signal
    def fix_triggered(self, payload: Any) -> None:
        """Receive a ``[fix]`` keyword event."""

        if self._cancel_requested or self._out_of_scope:
            return

        text, actor, diff_hash = self._coerce_comment_signal(
            payload, "comment_text"
        )
        self._apply_fix_signal(text=text, diff_hash=diff_hash)
        del actor

    @workflow.signal
    def explain_triggered(self, payload: Any) -> None:
        """Receive an ``[explain]`` keyword event."""

        if self._cancel_requested or self._out_of_scope:
            return

        text, actor, diff_hash = self._coerce_comment_signal(
            payload, "comment_text", diff_field="pr_diff_hash"
        )
        self._apply_explain_signal(text=text, pr_diff_hash=diff_hash)
        del actor

    # -- Shared signal-application helpers ---------------------------------
    #
    # These methods carry the actual debounce / dedup / cache logic so
    # ``comment_added`` (keyword-routed) and the dedicated signals share
    # one implementation. They are NOT decorated with ``@workflow.signal``
    # — Temporal still treats them as plain methods, so the per-signal
    # ``@workflow.signal`` boundary stays intact while the body is
    # de-duplicated.

    def _apply_fix_signal(self, *, text: str, diff_hash: str | None) -> None:
        """Apply the ``[fix]`` debounce + dedup logic."""

        if self._cancel_requested or self._out_of_scope:
            return

        # Iter pre-condition first.
        decision = _should_advance_iter(self._iteration_state, MAX_ITER)
        if not decision.advance:
            self._out_of_scope = True
            self._failure_reason = decision.reason or "max_iter_reached"
            self._signal_pending = True
            return

        now = workflow.now()

        # 60s debounce. Drop the signal silently when fired too
        # fast; the gateway also enforces this but defending in depth
        # keeps the workflow correct under direct-signal tests. The
        # body emits a ``fix_debounce_dropped`` audit event lazily on
        # the next loop turn (signal handlers cannot await activities).
        if _is_fix_debounced(self._iteration_state, now):
            self._pending_audit_actions.append(FIX_DEBOUNCE_AUDIT_ACTION)
            return

        # Receiving a ``[fix]`` resets the needs_info streak — the user
        # has supplied additional direction.
        self._iteration_state = _state_reset_needs_info(self._iteration_state)

        # Same diff means re-test is redundant.
        if diff_hash and _fix_should_skip_retest(
            self._iteration_state, diff_hash
        ):
            # Record the trigger time anyway so the next ``[fix]`` is
            # subject to the debounce window starting from this event.
            self._iteration_state = _state_record_fix_trigger(
                self._iteration_state, now
            )
            self._pending_fix_diff_hash = None
            self._pending_audit_actions.append(
                FIX_RETEST_PROTECTED_AUDIT_ACTION
            )
            self._signal_pending = True
            return

        if text:
            self._latest_comment = text
        self._iteration_state = _state_record_fix_trigger(
            self._iteration_state, now
        )
        self._pending_fix_diff_hash = diff_hash
        self._advance_iter_with_banner_check()

    def _apply_explain_signal(
        self, *, text: str, pr_diff_hash: str | None
    ) -> None:
        """Apply the ``[explain]`` cooldown + cache logic."""

        if self._cancel_requested or self._out_of_scope:
            return

        # ``[explain]`` does NOT consume an iteration when the cache is
        # warm (the answer is replayed verbatim). When the cache is
        # cold we still respect the iter cap — emitting a fresh LLM
        # response is a meaningful unit of work.
        now = workflow.now()
        if pr_diff_hash and _explain_should_skip_llm(
            self._iteration_state, pr_diff_hash, now
        ):
            self._pending_explain_diff_hash = pr_diff_hash
            self._pending_explain_text = text
            self._pending_audit_actions.append(EXPLAIN_CACHE_HIT_AUDIT_ACTION)
            self._signal_pending = True
            return

        # ``[explain]`` is a directional comment — reset the
        # ``needs_info`` streak just like the plain comment path does.
        self._iteration_state = _state_reset_needs_info(self._iteration_state)

        decision = _should_advance_iter(self._iteration_state, MAX_ITER)
        if not decision.advance:
            self._out_of_scope = True
            self._failure_reason = decision.reason or "max_iter_reached"
            self._signal_pending = True
            return

        self._pending_explain_diff_hash = pr_diff_hash
        self._pending_explain_text = text
        self._advance_iter_with_banner_check()

    def _apply_needs_info_signal(self, *, text: str) -> None:
        """Apply the ``[needs_info]`` loop-cap logic.

        Increments :attr:`IterationState.needs_info_streak`. When the
        streak reaches :data:`NEEDS_INFO_MAX_STREAK` the workflow
        flips to ``out_of_scope`` so the body can post the "yeni task
        aç" comment and terminate cleanly.
        """

        if self._cancel_requested or self._out_of_scope:
            return

        if text:
            self._latest_comment = text

        self._iteration_state = _state_increment_needs_info(
            self._iteration_state
        )
        if _needs_info_should_terminate(self._iteration_state):
            self._out_of_scope = True
            self._failure_reason = "needs_info_loop_cap"
            self._signal_pending = True
            return

        # ``needs_info`` does NOT advance ``iter_count`` on its own —
        # it just records that the bot is still gathering input. The
        # iteration counter advances when the user supplies a real
        # answer (plain ``comment_added``) or fires ``[fix]`` /
        # ``[explain]``.
        self._signal_pending = True

    def _advance_iter_with_banner_check(self) -> None:
        """Advance ``iter_count`` and arm the iter==3 banner edge.

        Called by every code path that legitimately advances the
        iteration counter. When the resulting count crosses
        :data:`ITER_WARNING_THRESHOLD` for the first time the helper
        flips :attr:`_iter_warning_pending` so the run body can post
        the Jira banner once and only once.
        """

        self._iteration_state = _state_increment_iter(self._iteration_state)
        if (
            not self._iter_warning_at_three
            and self._iteration_state.iter_count >= ITER_WARNING_THRESHOLD
        ):
            self._iter_warning_pending = True
        self._signal_pending = True

    @workflow.signal
    def cancel_requested(self, payload: Any) -> None:
        """Receive an end-user / admin cancel signal.

        Cancel always wins over the iter cap — the body observes
        ``_cancel_requested`` and runs the compensation chain regardless
        of ``_out_of_scope`` state.

        Idempotent: a second cancel signal that arrives
        while compensation is already in flight (or has completed) is
        a silent no-op — both :attr:`_cancel_requested` and
        :attr:`_compensation_running` latch the first request, and
        every subsequent invocation observes the latched state and
        returns without mutating workflow state. This guarantees the
        compensation chain fires exactly once even under signal
        replay or rapid double-tap from the cancel API.
        """

        # Idempotency latch — the *first* cancel wins. We refuse to
        # overwrite ``_cancel_actor_id`` / ``_cancel_actor_role`` /
        # ``_cancel_reason`` once they're set so the audit row + the
        # compensation context reflect the original cancel actor even
        # if the API is hit twice in rapid succession.
        if self._cancel_requested or self._compensation_running:
            return

        actor_id, actor_role, reason = self._coerce_cancel_signal(payload)
        self._cancel_requested = True
        self._cancel_actor_id = actor_id
        self._cancel_actor_role = actor_role
        self._cancel_reason = reason
        self._signal_pending = True

    # -- Run ---------------------------------------------------------------

    @workflow.run
    async def run(
        self, inp: AgentRunnerWorkflowInput
    ) -> AgentRunnerWorkflowOutput:
        # Initial iteration count comes from the input (almost always 1
        # for a fresh dispatch; >=2 reserved for future iter-N re-entry
        # via a parent-side restart). We clamp ``max_iter`` to the
        # constant so a misconfigured input cannot lift the cap.
        max_iter = min(inp.max_iter, MAX_ITER) if inp.max_iter > 0 else MAX_ITER

        # Seed the iteration state from the input's iteration counter.
        self._iteration_state = dataclasses.replace(
            self._iteration_state, iter_count=max(0, inp.iteration - 1)
        )

        # Initial advance pre-condition — refuse to start when the
        # input already exceeds the cap.
        decision = _should_advance_iter(self._iteration_state, max_iter)
        if not decision.advance:
            self._out_of_scope = True
            self._failure_reason = decision.reason or "max_iter_reached"
            return self._build_output(
                status="out_of_scope",
                summary=(
                    "🤖 Maksimum iterasyon sayısına ulaşıldı, lütfen yeni "
                    "bir task açın."
                ),
            )

        # Account the initial run as iteration N. Use the
        # banner-aware helper so a workflow restarted directly at
        # ``inp.iteration >= 3`` still arms the iter==3 banner edge.
        self._advance_iter_with_banner_check()

        # ----- Per-workflow-type body --------------------------------
        # The full body lands in tasks 7-10. For now, dispatch returns
        # a "completed" status when the workflow type is recognised; an
        # unrecognised workflow_type fails the run with a stable reason
        # so callers can rely on the contract from day one.
        try:
            await self._dispatch_workflow_type(inp)
        except _CancelledViaSignal:
            return await self._handle_cancel(inp)
        except _EpicSubtaskFailed:
            # An Epic ``multi_step`` subtask failed; the handler already
            # posted the stop comment and set ``epic_subtask_failed``.
            return self._build_output(
                status="failed",
                summary=(
                    "❌ Epic durduruldu: bir subtask başarısız oldu. "
                    "Ayrıntılar Epic yorumlarında."
                ),
            )
        except _OutOfScope:
            return self._build_output(
                status="out_of_scope",
                summary=(
                    "🤖 Maksimum iterasyon sayısına ulaşıldı, lütfen yeni "
                    "bir task açın."
                ),
            )
        except _OutputActionCriticalFailure as exc:
            # A critical output action refused; trigger the
            # compensation chain (close draft PR, label Confluence,
            # etc.) and terminate with ``failed``. The final Jira
            # comment names the completed critical actions and the
            # failed best-effort actions via
            # :func:`format_final_jira_comment`.
            self._failure_reason = OUTPUT_ACTION_CRITICAL_FAILED_REASON
            self._cancel_reason = "user_cancel"
            return await self._handle_output_action_critical(inp, exc)
        except TokenCapExceededError as exc:
            # Fail-fast token cap: the audit row was already written by
            # :meth:`_execute_llm_activity` before raising, so the
            # workflow simply terminates with a stable failure
            # category. ``maximum_attempts=1`` on the activity retry
            # policy guarantees no second call was made.
            self._failure_reason = TOKEN_CAP_AUDIT_ACTION
            return self._build_output(
                status="failed",
                summary=(
                    "❌ Token cap aşıldı (girdi token sayısı "
                    f"{exc.input_tokens} > {exc.cap}); LLM çağrısı yapılmadı."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - body errors → failed
            self._failure_reason = (
                self._failure_reason or "agent_runner_body_error"
            )
            return self._build_output(
                status="failed",
                summary=f"❌ Agent runner başarısız: {exc}",
            )

        # ----- Cancel handling ---------------------------------------
        if self._cancel_requested:
            return await self._handle_cancel(inp)

        if self._out_of_scope:
            return self._build_output(
                status="out_of_scope",
                summary=(
                    "🤖 Maksimum iterasyon sayısına ulaşıldı, lütfen yeni "
                    "bir task açın."
                ),
            )

        # ----- Success / partial success -----------------------------
        # The final Jira comment names every critical action
        # that succeeded plus every best-effort action that failed.
        # When neither list carries content the formatter returns
        # the empty string and we fall back to the legacy "✅ Tamamlandı"
        # one-liner so the Jira summary is never blank.
        final_summary = format_final_jira_comment(
            self._output_actions_log.successful_critical,
            self._output_actions_log.failed_best_effort,
        )

        if self._output_actions_partial or self._output_actions_log.failed_best_effort:
            summary = final_summary or (
                "✅ Tamamlandı, ancak bazı yan-aksiyonlar başarısız: "
                + ", ".join(self._output_actions_partial)
            )
            return self._build_output(
                status="completed_with_partial_failure",
                summary=summary,
            )

        if final_summary:
            return self._build_output(status="completed", summary=final_summary)
        return self._build_output(status="completed", summary="✅ Tamamlandı.")

    async def _handle_output_action_critical(
        self,
        inp: AgentRunnerWorkflowInput,
        exc: _OutputActionCriticalFailure,
    ) -> AgentRunnerWorkflowOutput:
        """Run compensation after a critical output-action failure.

        Mirrors :meth:`_handle_cancel` (the compensation contract is
        identical) but composes a richer terminal summary that names
        the failed critical actions verbatim so the Jira reviewer
        sees the cause without having to inspect the audit log.

        Note: this path is NOT a user cancel; it does not emit the
        :data:`CANCEL_BY_END_USER_AUDIT_ACTION` /
        :data:`CANCEL_BY_ADMIN_AUDIT_ACTION` audit row. The
        ``compensation_step_failed`` / ``compensation_step_ok`` rows
        emitted by the chain itself remain the authoritative trail
        for this branch.
        """

        # Idempotency latch — same flag the cancel path sets so a
        # cancel signal arriving during this compensation chain does
        # not double-fire the activity.
        self._compensation_running = True

        try:
            await workflow.execute_activity(
                "compensation_chain_run",
                args=[
                    {
                        "workflow_id": workflow.info().workflow_id,
                        "dept_id": inp.department_id,
                        "issue_key": inp.issue_key,
                        "actor_id": "",
                        "actor_role": "system",
                        "reason": "output_action_critical_failed",
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 - compensation best-effort
            workflow.logger.warning(
                "compensation_chain_run failed for %s — continuing",
                workflow.info().workflow_id,
            )

        # Compose the final Jira comment with the expected operator-facing shape:
        # the ``failed_critical`` list lands under the best-effort
        # prefix because the Turkish prose template only carries one
        # "warning" line — see :func:`format_final_jira_comment`.
        failed_lines: list[tuple[str, str]] = list(
            self._output_actions_log.failed_critical
        )
        failed_lines.extend(self._output_actions_log.failed_best_effort)
        summary = format_final_jira_comment(
            self._output_actions_log.successful_critical,
            failed_lines,
        )
        if not summary:
            summary = "❌ Kritik yan-aksiyon başarısız oldu."
        return self._build_output(status="failed", summary=summary)

    # -- Body dispatcher (per-workflow-type bodies) ------------------------

    async def _dispatch_workflow_type(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Drive the per-workflow-type body.

        Discriminates on :attr:`AgentRunnerWorkflowInput.workflow_type`
        and delegates to the matching ``_handle_*`` coroutine. The
        ``code_change_*`` / ``pr_review`` paths land here;
        the ``confluence_doc_*`` paths land here too. The remaining
        types (``research_*``, ``multi_step``, ``noop_test``,
        ``remote_ssh_test_only``) still fall through to the legacy
        signal-wait loop below — tasks 9.3 / 10.3 will replace those
        branches.

        Subclasses / future extensions MUST observe the same invariants:

        * Run :func:`_should_advance_iter` before incrementing
          ``iter_count``.
        * Use ``workflow.execute_activity`` (or
          ``workflow.execute_child_workflow``) for every side effect.
        * Surface best-effort failures via
          :meth:`_record_partial_failure` rather than raising.
        """

        wf_type = inp.workflow_type
        if wf_type == "code_change_with_test":
            await self._handle_code_change_with_test(inp)
            return
        if wf_type == "code_change_commit_only":
            await self._handle_code_change_commit_only(inp)
            return
        if wf_type == "pr_review":
            await self._handle_pr_review(inp)
            return
        if wf_type == "confluence_doc_create":
            await self._handle_confluence_doc_create(inp)
            return
        if wf_type == "confluence_doc_update":
            await self._handle_confluence_doc_update(inp)
            return
        if wf_type == "research_publish_confluence":
            await self._handle_research_publish_confluence(inp)
            return
        if wf_type == "research_summary_jira":
            await self._handle_research_summary_jira(inp)
            return

        if wf_type == "multi_step":
            await self._handle_multi_step(inp)
            return

        # ---- Legacy signal-wait fallback ------------------------

        # Post the iter==3 banner before waiting (handles the case
        # where the workflow was restarted directly at iter >= 3).
        await self._maybe_post_iter_warning_banner(inp)
        await self._drain_pending_audits(inp)

        # Wait for either: a signal (cancel / fix / explain / comment),
        # the iter cap to flip, or the signal-wait timeout. The
        # ``wait_condition`` callable is replay-deterministic — it only
        # reads workflow state.
        try:
            await workflow.wait_condition(
                lambda: (
                    self._cancel_requested
                    or self._out_of_scope
                    or self._signal_pending
                ),
                timeout=SIGNAL_WAIT_TIMEOUT,
            )
        except TimeoutError:
            self._out_of_scope = True
            self._failure_reason = "signal_wait_timeout"
            return

        # Drain the signal-pending edge so a second wait would block on
        # genuinely new input.
        self._signal_pending = False

        # Fire any per-iteration side effects queued up by the signal
        # handlers (banner + audit drains). These must run before the
        # OutOfScope / Cancel branches so an iter==3 / fix-debounce
        # transition leaves a corresponding Jira comment / audit row.
        await self._maybe_post_iter_warning_banner(inp)
        await self._drain_pending_audits(inp)

        if self._cancel_requested:
            raise _CancelledViaSignal
        if self._out_of_scope:
            raise _OutOfScope

    # -- code_change_* handlers --------------------------------------------

    async def _handle_code_change_with_test(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Code-change happy path with test execution.

        Sequence for the code-change flow:

        1. ``set_assignee_to_bot`` claims the Jira issue for the bot.
        2. ``branch_pattern_rules`` deny → ``out_of_scope``.
        3. ``compute_branch_name`` (pure formatter).
        4. ``precommit_scanner`` activity — block → fail with audit
           ``precommit_secret_leak_blocked``.
        5. ``bitbucket_create_commit`` to push the change.
        6. Child :class:`ExecutionRunWorkflow` to run tests.
        7. On test pass: route the PR-create tool via
           :func:`select_pr_create_tool`, call it with ``draft=True``
           (foundation ``pr_draft`` enforcement is automatic), then
           post a Jira comment with the PR link.
        8. On test fail: post a failure summary comment, no PR.

        """

        await self._handle_code_change_common(
            inp, with_test=True, open_pr=True
        )

    async def _handle_code_change_commit_only(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Code-change commit-only path.

        Same shape as :meth:`_handle_code_change_with_test` but with
        the test-run + PR-creation steps elided. The Jira comment
        carries the branch link plus a diff summary fed from the
        ``diff_summary_cache`` (placeholder mirrored here as
        a per-workflow LRU on :attr:`_diff_summary_cache`).
        """

        await self._handle_code_change_common(
            inp, with_test=False, open_pr=False
        )

    async def _handle_code_change_common(
        self,
        inp: AgentRunnerWorkflowInput,
        *,
        with_test: bool,
        open_pr: bool,
    ) -> None:
        """Shared spine for the two ``code_change_*`` flows.

        Carries the steps that are identical between
        ``code_change_with_test`` and ``code_change_commit_only``:
        assignee transfer → branch routing gate → branch name compute
        → precommit scan → commit. The two diverging suffixes
        (``with_test`` adds the test-run + PR-create chain;
        ``commit_only`` posts a branch-link Jira comment) are gated
        behind the *open_pr* / *with_test* flags so the activity
        sequence stays readable in one place — and the unit tests
        can drive both branches against a single mock fixture set.
        """

        target_repo = inp.target_repo or ""
        target_branch = inp.target_branch or ""

        # 1. jira_set_assignee_to_bot.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical step
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # 2. branch_pattern_rules check — denies hotfix commit-only,
        #    release non-pr_review etc. The rule list is pinned to
        #    the foundation defaults; per-dept rule loading lands in
        #    departments.json schema migration.
        if target_branch:
            decision = route_by_branch_pattern(
                target_branch,
                inp.workflow_type,
                DEFAULT_BRANCH_PATTERN_RULES,
            )
            if not decision.allowed:
                self._out_of_scope = True
                self._failure_reason = decision.reason
                await self._emit_audit_action(decision.reason, inp)
                raise _OutOfScope

        # 3. compute_branch_name — pure formatter; ``existing_branches``
        #    is empty for the first iteration of a fresh issue. A
        #    follow-up task (7.6 — iter-N supersede) will populate the
        #    set from a Bitbucket list-branches activity.
        branch_name = compute_branch_name(
            inp.issue_key,
            self._iteration_state.iter_count,
            (),
        )

        # 4. precommit_scanner — gating step. The diff to scan is
        #    produced by the LLM analysis output_actions; for the
        #    skeleton path we use the analysis ``rationale`` as a
        #    proxy so the activity is exercised. A future task (10.1)
        #    threads the actual diff through.
        generation_prompt = self._build_code_generation_prompt(inp)
        estimated_tokens = max(1, len(generation_prompt) // 4)
        workspace_path = "/tmp/workspace"
        try:
            code_output = await self._execute_llm_activity(
                "opencode_generate_code",
                args=[
                    {
                        "issue_key": inp.issue_key,
                        "prompt": generation_prompt,
                        "model": None,
                    },
                    workspace_path,
                ],
                input_tokens=estimated_tokens,
                inp=inp,
            )
        except TokenCapExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "opencode_generate_code_failed"
            workflow.logger.warning(
                "opencode_generate_code failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        commit_files = self._extract_commit_files(code_output)
        if not commit_files:
            self._failure_reason = "code_generation_no_files"
            await self._post_jira_comment_best_effort(
                inp,
                (
                    "Kod degisikligi icin commit edilebilir dosya uretilemedi. "
                    "Lutfen task aciklamasina hedef dosya(lar), beklenen "
                    "degisiklik ve varsa ornek davranisi ekleyip tekrar yorum "
                    "yazin; yorum geldikten sonra workflow yeniden tetiklenir."
                ),
            )
            return

        diff_text = self._extract_code_diff_text(code_output, commit_files)
        try:
            scan_result = await workflow.execute_activity(
                "precommit_scanner",
                args=[diff_text],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "precommit_scan_failed"
            workflow.logger.warning(
                "precommit_scanner failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        decision = self._extract_precommit_decision(scan_result)
        if decision == "block":
            # The activity itself emits the audit row; the workflow
            # surfaces a stable failure reason and aborts.
            self._failure_reason = "precommit_secret_leak_blocked"
            raise ApplicationError(
                "precommit_scanner blocked the commit (secret leak)",
                type="PrecommitSecretLeakBlocked",
                non_retryable=True,
            )

        # 5. bitbucket_create_commit — push the change. The activity
        #    expects a :class:`RepoRef` + file list; for the skeleton
        #    path we forward the LLM-produced output_actions verbatim
        #    via ``args`` so the activity layer can decode them.
        repo_ref = {
            "workspace": "",
            "repo_slug": target_repo,
        }
        source_branch = (
            "develop"
            if not target_branch
            or target_branch == "auto"
            or target_branch.startswith("ai/")
            else target_branch
        )
        try:
            await workflow.execute_activity(
                "bitbucket_create_branch",
                args=[repo_ref, source_branch, branch_name, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "bitbucket_branch_create_failed"
            workflow.logger.warning(
                "bitbucket_create_branch failed for %s on %s: %s",
                inp.issue_key,
                branch_name,
                exc,
            )
            raise

        try:
            commit_info = await workflow.execute_activity(
                "bitbucket_create_commit",
                args=[
                    repo_ref,
                    branch_name,
                    commit_files,
                    format_commit_message(
                        message=inp.analysis.title or "AI-generated change",
                        issue_key=inp.issue_key,
                        iteration=max(1, self._iteration_state.iter_count),
                        bot_email=f"ai-bot@{inp.department_id}.local",
                    ),
                    inp.department_id,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "bitbucket_commit_failed"
            workflow.logger.warning(
                "bitbucket_create_commit failed for %s on %s: %s",
                inp.issue_key,
                branch_name,
                exc,
            )
            raise

        commit_hash = self._extract_commit_hash(commit_info) or branch_name

        # 6 + 7. Test execution + PR creation (only for ``with_test``).
        if with_test:
            test_passed = await self._run_execution_child(inp, commit_hash)

            if not test_passed:
                # Post a failure summary comment; no PR opened.
                await self._post_jira_comment_best_effort(
                    inp,
                    f"❌ Testler başarısız: branch `{branch_name}`. "
                    "Detay için Temporal UI'sini kontrol edin.",
                )
                self._failure_reason = "execution_run_failed"
                return

            # On pass: route the PR-create tool via deployment_router.
            # The deployment value comes from the dept config; the
            # workflow input does not carry it directly today, so
            # default to ``"cloud"`` until the deployment mode is threaded through.
            deployment = "cloud"
            pr_tool = select_pr_create_tool(deployment)
            try:
                pr_info = await workflow.execute_activity(
                    pr_tool,
                    args=[
                        {
                            "workspace": "",
                            "repo_slug": target_repo,
                        },
                        branch_name,
                        "main",  # destination branch — callers can override
                        # the dept-configured default branch.
                        f"[bot] {inp.analysis.title or inp.issue_key}",
                        inp.analysis.rationale or "AI-generated change.",
                        inp.department_id,
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 - critical
                self._failure_reason = "bitbucket_open_pr_failed"
                workflow.logger.warning(
                    "PR-create tool %s failed for %s: %s",
                    pr_tool,
                    inp.issue_key,
                    exc,
                )
                raise

            pr_url = self._extract_pr_url(pr_info)
            new_pr_id = self._extract_pr_id_from_info(pr_info)

            # Mark the previous iteration's PR as
            # superseded *before* posting the Jira comment so a reader
            # who follows the PR link from the comment sees the
            # banner already in place. The activity is idempotent
            # (PK constraint on the ledger + label/banner guards) so
            # a Temporal retry under ``maximum_attempts=3`` is safe.
            if new_pr_id is not None and self._previous_pr_id is not None:
                try:
                    await workflow.execute_activity(
                        "iter_advance_pr_supersede",
                        args=[
                            {
                                "workspace": "",
                                "repo_slug": target_repo,
                            },
                            workflow.info().workflow_id,
                            self._previous_pr_id,
                            new_pr_id,
                            inp.department_id,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RetryPolicy(
                            initial_interval=timedelta(seconds=1),
                            backoff_coefficient=2.0,
                            maximum_interval=timedelta(seconds=10),
                            maximum_attempts=3,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort
                    # Supersede is a courtesy update — failure must
                    # not block the new iteration. The activity itself
                    # records partial progress (label vs description)
                    # so a subsequent ``[fix]`` retry will pick up
                    # whatever was missed.
                    workflow.logger.warning(
                        "iter_advance_pr_supersede failed for old=%d "
                        "new=%d: %s",
                        self._previous_pr_id,
                        new_pr_id,
                        exc,
                    )
                    self._record_partial_failure(
                        "iter_advance_pr_supersede"
                    )

            # Track the new PR id so the next iteration can supersede
            # this one. Only updated when the PR id is recoverable —
            # otherwise we leave the previous value intact so a
            # malformed activity response does not silently break the
            # supersede chain.
            if new_pr_id is not None:
                self._previous_pr_id = new_pr_id

            await self._post_jira_comment_best_effort(
                inp,
                f"✅ Draft PR açıldı: {pr_url or branch_name}",
            )
            await self._maybe_execute_llm_output_actions(inp)
            return

        # commit-only flow: post a branch-link Jira comment with a
        # diff summary served from the per-workflow cache
        # placeholder — see ``_diff_summary_cache``).
        diff_hash = commit_hash
        diff_summary = self._diff_summary_cache.get(diff_hash)
        if diff_summary is None:
            diff_summary = (
                inp.analysis.title or "Değişiklik özeti üretilemedi."
            )
            self._diff_summary_cache[diff_hash] = diff_summary

        await self._post_jira_comment_best_effort(
            inp,
            f"✅ Branch hazır: `{branch_name}` — {diff_summary}",
        )
        await self._maybe_execute_llm_output_actions(inp)

    async def _handle_pr_review(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """LLM-driven PR review with hash dedup.

        Steps:

        1. Fetch the PR diff via :func:`bitbucket_fetch_pr_diff`.
        2. Run the ``pr_review.md`` LLM activity (``llm_review_code``)
           via :meth:`_execute_llm_activity` so the token cap
           applies.
        3. ``dedup_findings(self._previous_findings, current)`` — only
           emit findings whose ``hash`` was not seen in earlier
           iterations.
        4. Post each new finding as a PR comment via
           :func:`bitbucket_add_pr_comment`. Add the new hashes to
           :attr:`_previous_findings` so the next iteration suppresses
           them.

        """

        target_repo = inp.target_repo or ""
        # The workflow input carries the issue_key, not the PR id —
        # the AutomationWorkflow passes the PR id via the analysis
        # ``rationale`` as a fallback today; future callers can surface a
        # dedicated field. Defensive: fall back to 0 so the activity
        # surfaces the misconfiguration instead of crashing the body.
        pr_id = self._extract_pr_id(inp)

        try:
            diff = await workflow.execute_activity(
                "bitbucket_fetch_pr_diff",
                args=[
                    {"workspace": "", "repo_slug": target_repo},
                    pr_id,
                    inp.department_id,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "bitbucket_fetch_pr_diff_failed"
            workflow.logger.warning(
                "bitbucket_fetch_pr_diff failed for PR %s: %s",
                pr_id,
                exc,
            )
            raise

        diff_text = self._extract_diff_text(diff)
        # Token estimate — best effort approximation; the activity
        # itself recomputes the count from the rendered prompt.
        estimated_tokens = max(1, len(diff_text) // 4)

        issue_data = {
            "issue_key": inp.issue_key,
            "summary": inp.analysis.title or inp.issue_key,
            "description": inp.analysis.rationale or "",
            "issue_type": "Task",
            "project_key": inp.issue_key.split("-")[0] if "-" in inp.issue_key else "",
        }

        try:
            review = await self._execute_llm_activity(
                "llm_review_code",
                args=[diff, issue_data],
                input_tokens=estimated_tokens,
                inp=inp,
            )
        except TokenCapExceededError:
            # Re-raise — the workflow body's outer handler converts
            # this into a stable terminal status.
            raise
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "llm_review_code_failed"
            workflow.logger.warning(
                "llm_review_code failed for PR %s: %s",
                pr_id,
                exc,
            )
            raise

        current_findings = self._extract_findings(review)
        new_findings = _dedup_findings(
            self._previous_findings, current_findings
        )

        for finding in new_findings:
            body = (
                finding.get("body")
                or finding.get("message")
                or "(boş bulgu)"
            )
            try:
                await workflow.execute_activity(
                    "bitbucket_add_pr_comment",
                    args=[
                        {"workspace": "", "repo_slug": target_repo},
                        pr_id,
                        body,
                        inp.department_id,
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 - best effort
                workflow.logger.warning(
                    "bitbucket_add_pr_comment failed for PR %s: %s",
                    pr_id,
                    exc,
                )
                self._record_partial_failure("bitbucket_add_pr_comment")
                continue

            finding_hash = finding.get("hash")
            if finding_hash:
                self._previous_findings.add(finding_hash)

        # ``[explain]`` enrichment — when a signal handler stashed an
        # explain request, render a max-200-word answer via the LLM
        # token-capped helper, cache it on IterationState, and post
        # the answer to the PR.
        await self._maybe_handle_explain(inp)

    # -- confluence_doc_* handlers -----------------------------------------

    async def _handle_confluence_doc_create(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Create a Confluence page with provenance footer.

        Sequence for the Confluence document creation flow:

        1. ``set_assignee_to_bot`` claims the
           Jira issue.
        2. Resolve the Jira issue link via ``jira_build_issue_link``
           so :func:`compute_provenance_footer` can embed it
           verbatim in the page body. The activity is best-effort: a
           failure degrades into a no-op footer rather than aborting
           the run.
        3. Compose the page title via :func:`format_page_title`
           (``{topic} - {YYYY-MM-DD}``). ``current_date`` is sourced
           from ``workflow.now().date()`` so the call stays
           replay-deterministic. The topic is taken from
           ``analysis.title`` so the LLM-translated text is preserved.
        4. Generate the body via the token-capped LLM activity
           ``llm_generate_doc`` (token-cap enforcement applies). The
           returned text is appended with the provenance footer
           before the page is created.
        5. Call ``confluence_create_page`` — the foundation
           :func:`mcp_client.tool_filter.filter_tools` ensures
           ``confluence_delete_page`` is never offered to the LLM
           ``confluence_create_page`` itself is allowed.
        6. Post a Jira completion comment with the page link
           (best-effort).

        """

        # 1. set_assignee_to_bot — critical step.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical step
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # 2. Resolve Jira issue link for the provenance footer.
        jira_issue_link = await self._resolve_jira_issue_link(inp)

        # 3. Compose the page title via the pure formatter.
        topic = (inp.analysis.title or inp.issue_key).strip() or inp.issue_key
        target_lang = inp.default_language if inp.default_language in (
            "tr",
            "en",
        ) else "tr"
        current_date = workflow.now().date()
        try:
            page_title = format_page_title(
                topic, target_lang, current_date  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001 - formatter rejects bad topic
            self._failure_reason = "confluence_title_invalid"
            workflow.logger.warning(
                "format_page_title failed for %s: %s", inp.issue_key, exc
            )
            raise

        # 4. LLM body — token-capped.
        rationale = inp.analysis.rationale or ""
        estimated_tokens = max(1, len(rationale) // 4 + len(topic) // 4)
        try:
            body_response = await self._execute_llm_activity(
                "llm_generate_doc",
                args=[
                    {
                        "topic": topic,
                        "target_lang": target_lang,
                        "context": rationale,
                        "issue_key": inp.issue_key,
                    }
                ],
                input_tokens=estimated_tokens,
                inp=inp,
            )
        except TokenCapExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "llm_generate_doc_failed"
            workflow.logger.warning(
                "llm_generate_doc failed for %s: %s", inp.issue_key, exc
            )
            raise

        body_text = self._extract_doc_body(body_response)

        # 5. Append the provenance footer and call
        # ``confluence_create_page``. The footer is appended only
        # when we have a usable Jira issue link; otherwise we
        # gracefully fall back to a body without the footer rather
        # than fail the page creation. The footer attribution is
        # required so operators can detect a
        # missing footer via the ``confluence_create_page`` audit
        # trail in such degraded cases.
        page_body = body_text
        if jira_issue_link:
            try:
                page_body = body_text + "\n\n" + compute_provenance_footer(
                    jira_issue_link
                )
            except Exception as exc:  # noqa: BLE001 - degraded footer path
                workflow.logger.warning(
                    "compute_provenance_footer failed for %s: %s — "
                    "creating page without footer",
                    inp.issue_key,
                    exc,
                )

        target_space = inp.analysis.target_space or ""
        try:
            page_info = await workflow.execute_activity(
                "confluence_create_page",
                args=[
                    target_space,
                    page_title,
                    page_body,
                    inp.department_id,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "confluence_create_page_failed"
            workflow.logger.warning(
                "confluence_create_page failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        page_url = self._extract_confluence_page_url(page_info)
        page_id = self._extract_confluence_page_id(page_info)
        if page_id:
            # Stash the new page id on the iteration so a follow-up
            # update knows where to write. Stored as a tuple-ish via
            # the workflow's confluence section hash set is not the
            # right place; we record it on the body's local state via
            # the standard partial-failure / pr_id patterns. The
            # output dataclass surfaces it through ``confluence_page_id``
            # at terminal time.
            self._latest_confluence_page_id = page_id

        # 6. Post the Jira completion comment (best-effort).
        link_text = page_url or page_title
        await self._post_jira_comment_best_effort(
            inp,
            f"📝 Confluence sayfası oluşturuldu: `{link_text}`",
        )

        # 7. Apply LLM-emitted output_actions.
        await self._maybe_execute_llm_output_actions(inp)

    async def _handle_confluence_doc_update(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Update Confluence sections with dedup + overwrite protection.

        Sequence for the Confluence section update flow:

        1. ``set_assignee_to_bot`` claims the Jira issue for the bot.
        2. Read the page metadata via ``confluence_get_page`` to
           resolve last-editor, last-edit timestamp, and current
           sections. The activity also supplies the page title so the
           ``_AI_PROBE_*`` filter can fire deterministically.
        3. Filter out pages whose title matches
           :func:`is_probe_page` — write-probe artifacts must never
           be overwritten.
        4. Overwrite-protection check via :func:`should_skip_overwrite`
           — when a non-bot user edited the page within the last 5
           minutes, skip the update and emit the
           ``confluence_overwrite_protected`` audit.
        5. For every target section: compute the content hash, run
           :func:`should_skip_section_update` against the workflow's
           in-memory hash set, and either skip (audit
           ``confluence_section_dedup_skip``) or update via
           ``confluence_update_page``. The section content is
           augmented with the provenance footer.
        6. Post a Jira summary comment listing updated and skipped
           sections (best-effort).

        """

        # 1. set_assignee_to_bot.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        page_id = inp.analysis.target_page_id or ""
        if not page_id:
            self._failure_reason = "confluence_page_id_missing"
            raise ApplicationError(
                "confluence_doc_update requires analysis.target_page_id",
                type="ConfluencePageIdMissing",
                non_retryable=True,
            )

        # 2. Read the page metadata.
        try:
            page_meta = await workflow.execute_activity(
                "confluence_get_page",
                args=[page_id, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "confluence_get_page_failed"
            workflow.logger.warning(
                "confluence_get_page failed for %s: %s", page_id, exc
            )
            raise

        page_title = self._extract_confluence_field(page_meta, "title", "")
        last_editor = self._extract_confluence_field(
            page_meta, "last_editor_account_id", None
        )
        last_edit_at = self._extract_confluence_datetime(
            page_meta, "last_edit_at"
        )

        # 3. Probe page filter — never write to ``_AI_PROBE_*`` pages.
        if is_probe_page(page_title):
            await self._emit_audit_action(
                CONFLUENCE_PROBE_PAGE_SKIPPED_AUDIT_ACTION, inp
            )
            await self._post_jira_comment_best_effort(
                inp,
                "🤖 Confluence güncellemesi atlandı: hedef sayfa "
                "bir doğrulama (probe) artefaktı.",
            )
            return

        # 4. Overwrite-protection check.
        bot_ids = self._resolve_bot_account_ids(inp)
        now = workflow.now()
        overwrite_decision = should_skip_overwrite(
            last_editor,
            last_edit_at,
            now,
            bot_ids,
        )
        if overwrite_decision.skip:
            await self._emit_audit_action(
                overwrite_decision.audit_event
                or AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
                inp,
            )
            await self._post_jira_comment_best_effort(
                inp,
                "⏸️ Confluence sayfası son 5 dakika içinde başka bir "
                "kullanıcı tarafından düzenlendi; güncelleme atlandı.",
            )
            return

        # 5. Per-section update with dedup.
        sections = self._extract_confluence_sections(page_meta, inp)
        if not sections:
            # Nothing to update — surface a clear partial-failure
            # signal but do not crash the run.
            workflow.logger.info(
                "confluence_doc_update: no sections to update for %s",
                page_id,
            )
            await self._post_jira_comment_best_effort(
                inp,
                "ℹ️ Confluence güncellemesi atlandı: güncellenecek "
                "bölüm bulunamadı.",
            )
            return

        # Resolve the Jira issue link once — every section's body
        # gets the same provenance footer.
        jira_issue_link = await self._resolve_jira_issue_link(inp)

        workflow_id = workflow.info().workflow_id
        updated_sections: list[str] = []
        skipped_sections: list[str] = []

        for section in sections:
            section_path = section.get("section_path") or section.get(
                "title"
            ) or ""
            content = section.get("content") or section.get("body") or ""
            content_hash = section.get("content_hash") or self._hash_section(
                section_path, content
            )

            # Replay-safe dedup: the ``_confluence_section_hashes``
            # set lives on the workflow instance and is updated only
            # after a successful write below.
            hash_table = {
                (workflow_id, page_id, section_path, h)
                for h in self._confluence_section_hashes
            }
            decision = should_skip_section_update(
                workflow_id,
                page_id,
                section_path,
                content_hash,
                hash_table,
            )
            if decision.skip:
                await self._emit_audit_action(
                    decision.audit_event
                    or AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
                    inp,
                )
                skipped_sections.append(section_path)
                continue

            # Append the provenance footer to the section
            # body before writing. Failure to render the footer is
            # non-fatal — the update still goes ahead.
            section_body = content
            if jira_issue_link:
                try:
                    section_body = content + "\n\n" + compute_provenance_footer(
                        jira_issue_link
                    )
                except Exception as exc:  # noqa: BLE001 - degraded
                    workflow.logger.warning(
                        "compute_provenance_footer failed for %s/%s: %s — "
                        "writing without footer",
                        page_id,
                        section_path,
                        exc,
                    )

            try:
                await workflow.execute_activity(
                    "confluence_update_page",
                    args=[
                        page_id,
                        section_path,
                        section_body,
                        inp.department_id,
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 - best effort
                workflow.logger.warning(
                    "confluence_update_page failed for %s/%s: %s",
                    page_id,
                    section_path,
                    exc,
                )
                self._record_partial_failure(
                    f"confluence_update_page:{section_path}"
                )
                continue

            self._confluence_section_hashes.add(content_hash)
            updated_sections.append(section_path)

        # 6. Jira summary comment (best-effort).
        summary_lines = ["📝 Confluence güncellemesi tamamlandı:"]
        if updated_sections:
            summary_lines.append(
                "  • Güncellenen bölümler: " + ", ".join(updated_sections)
            )
        if skipped_sections:
            summary_lines.append(
                "  • Atlanan bölümler (içerik aynı): "
                + ", ".join(skipped_sections)
            )
        await self._post_jira_comment_best_effort(
            inp, "\n".join(summary_lines)
        )

        # 7. Apply LLM-emitted output_actions.
        await self._maybe_execute_llm_output_actions(inp)

    # -- research_* handlers ------------------------------------------------

    async def _handle_research_publish_confluence(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Run the research → Confluence publish flow.

        Sequence for the research-to-Confluence publish flow:

        1. ``set_assignee_to_bot`` claims the
           Jira issue so the bot's authorship surfaces immediately.
        2. Resolve the Jira issue link via ``jira_build_issue_link``
           so the provenance footer can deep-link back to the
           original task.
        3. ``firecrawl_search`` activity with ``analysis.title`` (or
           ``rationale`` fallback) as the query. The activity returns
           a list of candidate URLs to scrape.
        4. For every URL: call ``firecrawl_scrape`` and triage the
           outcome:

           * ``EgressBlocked`` (or any dict with ``kind ==
             "egress_blocked"``) → graceful 403 handling:
             post a Jira comment naming the blocked domain, record a
             best-effort partial-failure marker, and **continue with
             the remaining URLs**. The workflow MUST NOT raise.
           * Successful scrape → harvest the content + bookkeeping
             (title, url, accessed_at) into the running ``content``
             buffer + ``sources`` list for the formatter.

        5. Render the Confluence body via
           :func:`format_research_publish_confluence_body`, append
           the provenance footer, and call the
           ``confluence_create_page`` activity.
        6. Post a Jira completion comment carrying the new page URL.

        """

        # 1. set_assignee_to_bot — critical step.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical step
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # 2. Resolve Jira issue link for the provenance footer.
        jira_issue_link = await self._resolve_jira_issue_link(inp)

        # 3. firecrawl_search to enumerate candidate URLs.
        query = self._extract_research_query(inp)
        try:
            search_result = await workflow.execute_activity(
                "firecrawl_search",
                args=[query, inp.department_id],
                start_to_close_timeout=_LLM_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "firecrawl_search_failed"
            workflow.logger.warning(
                "firecrawl_search failed for %s (query=%r): %s",
                inp.issue_key,
                query,
                exc,
            )
            raise

        candidate_urls = self._extract_firecrawl_urls(search_result)

        # 4. Scrape each URL; collect successful payloads + sources,
        # gracefully degrade on egress denial.
        scraped_content: list[str] = []
        sources: list[dict[str, str]] = []
        for url in candidate_urls:
            outcome = await self._firecrawl_scrape_with_grace(
                url=url, inp=inp
            )
            if outcome is None:
                continue  # blocked URL — graceful path already handled.
            scraped_content.append(outcome["content"])
            sources.append(outcome["source"])

        if not sources:
            # Nothing useful was collected; surface the situation as a
            # best-effort Jira comment + partial-failure marker. The
            # workflow does NOT fail — the operator can supply a
            # different topic or extend the allowlist.
            self._record_partial_failure("research_no_sources")
            await self._post_jira_comment_best_effort(
                inp,
                "🤖 Araştırma için kullanılabilir bir kaynak bulunamadı; "
                "konuyu daraltarak yeniden deneyin.",
            )
            return

        # 5. Render the Confluence body + provenance footer + create.
        joined_content = "\n\n".join(scraped_content).strip()
        body_text = format_research_publish_confluence_body(
            joined_content, sources
        )
        page_body = body_text
        if jira_issue_link:
            try:
                page_body = body_text + "\n\n" + compute_provenance_footer(
                    jira_issue_link
                )
            except Exception as exc:  # noqa: BLE001 - degraded footer path
                workflow.logger.warning(
                    "compute_provenance_footer failed for %s: %s — "
                    "creating page without footer",
                    inp.issue_key,
                    exc,
                )

        # Compose the page title via the same formatter the
        # ``confluence_doc_create`` flow uses so the two paths stay
        # consistent for end users.
        topic = (inp.analysis.title or inp.issue_key).strip() or inp.issue_key
        target_lang = inp.default_language if inp.default_language in (
            "tr",
            "en",
        ) else "tr"
        current_date = workflow.now().date()
        try:
            page_title = format_page_title(
                topic, target_lang, current_date  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001 - formatter rejects bad topic
            self._failure_reason = "confluence_title_invalid"
            workflow.logger.warning(
                "format_page_title failed for %s: %s", inp.issue_key, exc
            )
            raise

        target_space = inp.analysis.target_space or ""
        try:
            page_info = await workflow.execute_activity(
                "confluence_create_page",
                args=[
                    target_space,
                    page_title,
                    page_body,
                    inp.department_id,
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "confluence_create_page_failed"
            workflow.logger.warning(
                "confluence_create_page failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        page_url = self._extract_confluence_page_url(page_info)
        page_id = self._extract_confluence_page_id(page_info)
        if page_id:
            self._latest_confluence_page_id = page_id

        # 6. Jira completion comment (best-effort).
        link_text = page_url or page_title
        await self._post_jira_comment_best_effort(
            inp,
            f"📝 Araştırma yayınlandı: `{link_text}`",
        )

        # 7. Apply LLM-emitted output_actions.
        await self._maybe_execute_llm_output_actions(inp)

    async def _handle_research_summary_jira(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Run the research → Jira summary flow.

        Sequence for the research-to-Jira summary flow:

        1. ``set_assignee_to_bot`` claims the Jira issue for the bot.
        2. ``firecrawl_search`` — enumerate candidate URLs from the
           Jira topic.
        3. For every URL run ``firecrawl_scrape`` with the same
           graceful 403 fallback as the Confluence flow; collect the
           content + sources.
        4. Render the comment via
           :func:`format_research_summary_jira_comment`. When the
           formatter signals that the rendered content overflowed
           the size budget (``minio_uri`` is non-``None``), call the
           ``minio_put_research_summary`` activity to offload the
           full body and substitute the returned URI into the
           comment text. When ``minio_uri`` is ``None`` the comment
           is short enough to land in Jira verbatim.
        5. Post the Jira comment (the comment IS the workflow's
           primary side effect — failure flips ``failure_reason``
           rather than degrading silently).

        """

        # 1. set_assignee_to_bot — critical step.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical step
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # 2. firecrawl_search — same shape as the Confluence flow.
        query = self._extract_research_query(inp)
        try:
            search_result = await workflow.execute_activity(
                "firecrawl_search",
                args=[query, inp.department_id],
                start_to_close_timeout=_LLM_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "firecrawl_search_failed"
            workflow.logger.warning(
                "firecrawl_search failed for %s (query=%r): %s",
                inp.issue_key,
                query,
                exc,
            )
            raise

        candidate_urls = self._extract_firecrawl_urls(search_result)

        # 3. Scrape with graceful degradation.
        scraped_content: list[str] = []
        sources: list[dict[str, str]] = []
        for url in candidate_urls:
            outcome = await self._firecrawl_scrape_with_grace(
                url=url, inp=inp
            )
            if outcome is None:
                continue
            scraped_content.append(outcome["content"])
            sources.append(outcome["source"])

        if not sources:
            self._record_partial_failure("research_no_sources")
            await self._post_jira_comment_best_effort(
                inp,
                "🤖 Araştırma için kullanılabilir bir kaynak bulunamadı; "
                "konuyu daraltarak yeniden deneyin.",
            )
            return

        # 4. Render the comment via the pure formatter.
        summary_text = "\n\n".join(scraped_content).strip()
        comment_text, minio_uri = format_research_summary_jira_comment(
            summary_text, sources, max_words=500, max_sources=5
        )

        # 5. Long-content path: offload to MinIO and substitute the
        # real URI into the comment. The formatter returns a sentinel
        # ``minio://research-summary-pending`` placeholder so we can
        # detect and replace it deterministically.
        if minio_uri is not None:
            try:
                stored_uri = await workflow.execute_activity(
                    "minio_put_research_summary",
                    args=[
                        {
                            "workflow_id": workflow.info().workflow_id,
                            "issue_key": inp.issue_key,
                            "department_id": inp.department_id,
                            "content": summary_text,
                            "sources": sources,
                        }
                    ],
                    start_to_close_timeout=_SHORT_TIMEOUT,
                    retry_policy=_DEFAULT_RETRY,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort offload
                workflow.logger.warning(
                    "minio_put_research_summary failed for %s: %s — "
                    "posting comment without MinIO link",
                    inp.issue_key,
                    exc,
                )
                self._record_partial_failure("research_minio_offload")
                stored_uri = ""

            stored_uri_str = (
                str(stored_uri) if isinstance(stored_uri, str) else ""
            )
            if stored_uri_str:
                # Append the real URI to the comment so reviewers can
                # follow the link. We keep the placeholder strategy
                # explicit (rather than mutating the formatter output)
                # so the formatter stays a pure function.
                comment_text = (
                    f"{comment_text}\n\n🔗 Tam içerik: {stored_uri_str}"
                )

        # Post the comment as the primary output (NOT best-effort —
        # the comment IS the workflow's deliverable for this type).
        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[inp.issue_key, comment_text, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical for this flow
            self._failure_reason = "jira_comment_failed"
            workflow.logger.warning(
                "jira_add_comment failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # Apply LLM-emitted output_actions.
        await self._maybe_execute_llm_output_actions(inp)

    # -- multi_step (Epic fan-out) handler ---------------------------------

    async def _handle_multi_step(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Fan an Epic out to one child automation per subtask.

        Sequence for the Epic / multi_step flow:

        1. ``set_assignee_to_bot`` claims the parent Epic for the bot.
        2. ``jira_list_epic_children`` enumerates the Epic's child
           issues (JQL ``parent = <epic>``). An empty list posts a
           guidance comment and ends — the analyzer normally gates
           this case into ``needs_info``, but the workflow defends in
           depth.
        3. Each child runs as its own ``AutomationWorkflow`` child,
           executed sequentially so progress is observable and a
           failure stops the remaining subtasks (the same contract as
           the standalone Epic orchestrator). Progress and the final
           tally are posted back to the parent Epic as Jira comments.

        Children are awaited in order; the first failure posts a
        failure comment, marks the rest skipped, and flips
        ``failure_reason`` so the run terminates as ``failed``.
        """

        # 1. set_assignee_to_bot — claim the Epic for the bot.
        try:
            await workflow.execute_activity(
                "set_assignee_to_bot",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical step
            self._failure_reason = "jira_assignee_failed"
            workflow.logger.warning(
                "set_assignee_to_bot failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        # 2. Enumerate the Epic's children.
        try:
            children = await workflow.execute_activity(
                "jira_list_epic_children",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - critical
            self._failure_reason = "epic_children_lookup_failed"
            workflow.logger.warning(
                "jira_list_epic_children failed for %s: %s",
                inp.issue_key,
                exc,
            )
            raise

        child_items = self._normalise_epic_children(children)
        total = len(child_items)
        if total == 0:
            self._failure_reason = "epic_no_subtasks"
            await self._post_jira_comment_best_effort(
                inp,
                "🤖 Bu Epic için işlenecek subtask bulunamadı. "
                "Epic'e subtask ekleyip yorum yazarak yeniden tetikleyin.",
            )
            return

        completed = 0
        # Disambiguate child ids by the parent workflow run so a
        # re-triggered Epic actually re-runs its subtasks (Temporal
        # dedupes by workflow id; reusing a prior id would silently
        # replay the earlier — possibly denied — execution).
        run_suffix = workflow.info().run_id[:8]
        for index, child in enumerate(child_items):
            child_key = child["key"]
            child_workflow_id = (
                f"multi-step-{inp.issue_key}-{child_key}-{index}-{run_suffix}"
            )
            child_input = self._build_subtask_child_input(inp, child_key)
            try:
                child_output = await workflow.execute_child_workflow(
                    "AutomationWorkflow",
                    args=[child_input],
                    id=child_workflow_id,
                    task_queue="automation-tq",
                    execution_timeout=_EPIC_SUBTASK_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001 - stop on failure
                self._failure_reason = "epic_subtask_failed"
                workflow.logger.warning(
                    "multi_step subtask %s failed for Epic %s: %s",
                    child_key,
                    inp.issue_key,
                    exc,
                )
                skipped = total - (index + 1)
                await self._post_jira_comment_best_effort(
                    inp,
                    f"❌ Subtask {child_key} başarısız oldu — Epic durduruldu "
                    f"({completed}/{total} tamamlandı, {skipped} atlandı).",
                )
                # Stop the fan-out: surface a failed terminal status via
                # the run body's generic handler (no compensation chain —
                # the Epic itself opened no PR / draft to roll back).
                raise _EpicSubtaskFailed(child_key) from exc

            # The gateway returns ``decision="denied"`` / ``"failed"`` as a
            # normal completion (not an exception). Treat anything other
            # than a dispatched / completed outcome as a subtask failure
            # so the Epic does not falsely report success.
            decision = self._extract_child_decision(child_output)
            if decision in {"denied", "failed", "out_of_scope"}:
                self._failure_reason = "epic_subtask_failed"
                skipped = total - (index + 1)
                await self._post_jira_comment_best_effort(
                    inp,
                    f"❌ Subtask {child_key} işlenemedi (sonuç: {decision}) — "
                    f"Epic durduruldu ({completed}/{total} tamamlandı, "
                    f"{skipped} atlandı).",
                )
                raise _EpicSubtaskFailed(child_key)

            completed += 1
            await self._post_jira_comment_best_effort(
                inp,
                f"🤖 {completed}/{total} subtask tamamlandı "
                f"(`{child_key}`).",
            )

        await self._post_jira_comment_best_effort(
            inp,
            f"✅ Epic tamamlandı — {completed}/{total} subtask işlendi.",
        )
        await self._maybe_execute_llm_output_actions(inp)

    @staticmethod
    def _normalise_epic_children(children: Any) -> list[dict[str, str]]:
        """Coerce the ``jira_list_epic_children`` result into dicts.

        Accepts both the activity's ``EpicChild`` dataclass instances
        and the plain-dict fallback the Temporal data converter may
        produce, returning a list of ``{"key", "summary"}`` dicts with
        non-empty keys preserved in order.
        """
        items: list[dict[str, str]] = []
        if not isinstance(children, (list, tuple)):
            return items
        for child in children:
            if isinstance(child, dict):
                key = str(child.get("key") or "").strip()
                summary = str(child.get("summary") or "")
            else:
                key = str(getattr(child, "key", "") or "").strip()
                summary = str(getattr(child, "summary", "") or "")
            if key:
                items.append({"key": key, "summary": summary})
        return items

    @staticmethod
    def _extract_child_decision(child_output: Any) -> str:
        """Read the ``decision`` from a child AutomationWorkflow result.

        The gateway returns an ``AutomationWorkflowOutput`` (or a plain
        dict under some data-converter paths). A missing decision is
        treated as ``"dispatched"`` so a malformed-but-non-raising
        result does not falsely fail the Epic.
        """
        if isinstance(child_output, dict):
            value = child_output.get("decision")
        else:
            value = getattr(child_output, "decision", None)
        return str(value or "dispatched").strip().lower()

    def _build_subtask_child_input(
        self, inp: AgentRunnerWorkflowInput, child_key: str
    ) -> Any:
        """Build the ``AutomationWorkflowInput`` for one Epic subtask.

        Each subtask re-enters the gateway as a fresh first-iteration
        run so the analyzer picks the right workflow type for that
        child. Department-scoped routing context is inherited from the
        parent Epic input.
        """
        from temporal_shared.messages import (  # noqa: PLC0415
            AutomationWorkflowInput,
        )

        return AutomationWorkflowInput(
            issue_key=child_key,
            department_id=inp.department_id,
            available_capabilities=tuple(inp.available_capabilities or ()),
            available_repos=tuple(inp.available_repos or ()),
            available_spaces=tuple(inp.available_spaces or ()),
            default_language=inp.default_language,
            trigger_event="jira:issue_assigned",
            iteration=1,
            trace_id=inp.trace_id,
        )

    # -- research helpers --------------------------------------------------

    @staticmethod
    def _extract_research_query(inp: AgentRunnerWorkflowInput) -> str:
        """Build the firecrawl query string from the LLM analysis.

        Prefers ``analysis.title`` because it is the LLM's distilled
        statement of the task; falls back to ``rationale`` (truncated)
        and finally to the issue key so the query is never empty —
        firecrawl rejects empty input outright.
        """

        title = (inp.analysis.title or "").strip()
        if title:
            return title
        rationale = (inp.analysis.rationale or "").strip()
        if rationale:
            # Cap the fallback at a defensible length — firecrawl
            # tokenises the query upstream so a thousand-word
            # rationale would just waste budget.
            return rationale[:500]
        return inp.issue_key

    @staticmethod
    def _extract_firecrawl_urls(search_result: Any) -> list[str]:
        """Pull a list of unique URLs from a ``firecrawl_search`` result.

        Accepts either the typed :class:`FirecrawlSuccess` body
        (a ``list[dict]`` with ``url`` keys), a bare list, a single
        dict carrying ``urls`` / ``results``, or a raw list of strings.
        Duplicates are dropped while preserving first-seen order so
        the workflow scrapes each domain at most once per run.
        """

        seen: set[str] = set()
        ordered: list[str] = []

        def _add(candidate: Any) -> None:
            if isinstance(candidate, str):
                value = candidate.strip()
            elif isinstance(candidate, dict):
                raw = candidate.get("url") or candidate.get("href")
                value = str(raw).strip() if raw else ""
            else:
                value = str(getattr(candidate, "url", "")).strip()
            if not value or value in seen:
                return
            seen.add(value)
            ordered.append(value)

        # Drill through the common shapes returned by the activity.
        body: Any = search_result
        if hasattr(search_result, "body"):
            body = getattr(search_result, "body")
        if isinstance(body, dict):
            for key in ("results", "urls", "items"):
                value = body.get(key)
                if isinstance(value, list):
                    body = value
                    break
        if isinstance(body, list):
            for entry in body:
                _add(entry)
        elif isinstance(body, str):
            _add(body)

        return ordered

    async def _firecrawl_scrape_with_grace(
        self, *, url: str, inp: AgentRunnerWorkflowInput
    ) -> dict[str, Any] | None:
        """Run ``firecrawl_scrape`` for *url* with graceful 403 handling.

        Returns a ``{"content": str, "source": dict}`` mapping on
        success and ``None`` for blocked / failed URLs (the caller
        treats ``None`` as "skip this URL").  Blocked URLs trigger
        the standard Jira-comment + partial-failure flow.
        """

        try:
            outcome = await workflow.execute_activity(
                "firecrawl_scrape",
                args=[url, inp.department_id],
                start_to_close_timeout=_LLM_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - per-URL best effort
            workflow.logger.warning(
                "firecrawl_scrape failed for %s/%s: %s — skipping URL",
                inp.issue_key,
                url,
                exc,
            )
            self._record_partial_failure(f"firecrawl_scrape:{url}")
            return None

        kind = self._extract_firecrawl_kind(outcome)
        if kind == "egress_blocked":
            await self._post_jira_comment_best_effort(
                inp,
                f"🤖 {url} domain'i araştırma için izinli değil; "
                "admin'den eklenmesini isteyin.",
            )
            self._record_partial_failure(f"firecrawl_blocked:{url}")
            return None

        # Treat anything that's not an explicit success as a soft skip
        # so a malformed activity response cannot crash the run.
        if kind != "success":
            workflow.logger.info(
                "firecrawl_scrape returned non-success kind=%r for %s",
                kind,
                url,
            )
            return None

        content = self._extract_firecrawl_content(outcome)
        title = self._extract_firecrawl_title(outcome) or url
        accessed = self._extract_firecrawl_accessed_at(
            outcome, workflow.now()
        )
        return {
            "content": content,
            "source": {
                "title": title,
                "url": url,
                "accessed_at": accessed,
            },
        }

    @staticmethod
    def _extract_firecrawl_kind(outcome: Any) -> str:
        """Discriminate the firecrawl outcome union.

        The wrapper returns either a tagged dict (``{"kind": ...}``)
        or one of the typed dataclasses (:class:`FirecrawlSuccess`,
        :class:`EgressBlocked`, :class:`PayloadOverflow`); both
        carry a ``kind`` literal so the helper can collapse on it.
        """

        if isinstance(outcome, dict):
            return str(outcome.get("kind") or "success")
        kind = getattr(outcome, "kind", None)
        return str(kind) if kind else "success"

    @staticmethod
    def _extract_firecrawl_content(outcome: Any) -> str:
        """Pull the textual body from a successful scrape."""

        body: Any = outcome
        if isinstance(outcome, dict):
            body = outcome.get("body", outcome.get("content"))
        else:
            body = getattr(outcome, "body", None)
        if isinstance(body, dict):
            for key in ("markdown", "content", "text", "html"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
            return ""
        if isinstance(body, str):
            return body
        return ""

    @staticmethod
    def _extract_firecrawl_title(outcome: Any) -> str:
        """Pull a human-readable title from a successful scrape."""

        body: Any = outcome
        if isinstance(outcome, dict):
            body = outcome.get("body", outcome.get("content"))
        else:
            body = getattr(outcome, "body", None)
        if isinstance(body, dict):
            for key in ("title", "page_title", "name"):
                value = body.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""

    @staticmethod
    def _extract_firecrawl_accessed_at(outcome: Any, now: datetime) -> str:
        """Best-effort access date for a scrape — defaults to ``now``.

        Pure / replay-deterministic — when the scrape payload doesn't
        carry an ``accessed_at`` we use ``workflow.now().date()`` so
        the rendered Confluence sources block always shows a date.
        """

        body: Any = outcome
        if isinstance(outcome, dict):
            body = outcome.get("body", outcome.get("content"))
        else:
            body = getattr(outcome, "body", None)
        if isinstance(body, dict):
            value = body.get("accessed_at") or body.get("date")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return now.date().isoformat()

    # -- confluence helpers ------------------------------------------------

    async def _resolve_jira_issue_link(
        self, inp: AgentRunnerWorkflowInput
    ) -> str | None:
        """Resolve the canonical Jira issue link for *inp.issue_key*.

        Delegates to the ``jira_build_issue_link`` activity which
        composes ``{site_url}/browse/{issue_key}`` from the dept's
        Vault-backed credential. The activity is best-effort: a
        failure (or an activity that is not yet hosted on this worker)
        returns ``None`` so the caller falls back to a degraded
        provenance footer rather than failing the workflow run.
        """

        try:
            link = await workflow.execute_activity(
                "jira_build_issue_link",
                args=[inp.issue_key, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - best effort
            workflow.logger.warning(
                "jira_build_issue_link failed for %s: %s — "
                "provenance footer will be degraded",
                inp.issue_key,
                exc,
            )
            return None
        if isinstance(link, str) and link.startswith("https://"):
            return link
        if isinstance(link, dict):
            value = link.get("url") or link.get("link")
            if isinstance(value, str) and value.startswith("https://"):
                return value
        return None

    @staticmethod
    def _resolve_bot_account_ids(
        inp: AgentRunnerWorkflowInput,
    ) -> frozenset[str]:
        """Best-effort bot ``account_id`` set for overwrite protection.

        Today the workflow input does not surface the dept's bot
        account ids directly — the value is loaded by the
        AutomationWorkflow when computing the loop guard and is not
        forwarded to the agent runner. As a defensive default we
        return an empty set, which means
        :func:`should_skip_overwrite` treats every recent edit as a
        non-bot edit (the safer fail-closed behaviour).
        Future work will thread the bot id list
        through the input envelope so the comparison is exact.
        """

        # Reserved hook for future expansion; today the input does
        # not carry a bot id list. Returning a frozenset keeps the
        # ``should_skip_overwrite`` contract intact.
        del inp
        return frozenset()

    @staticmethod
    def _hash_section(section_path: str, content: str) -> str:
        """Replay-safe sha256 hex digest for a section body.

        Hashing is a pure deterministic function of its inputs, so
        the resulting digest is identical across replays. We include
        ``section_path`` in the digest input so two sections with
        identical bodies but different titles are not collapsed into
        a single dedup entry.
        """

        import hashlib

        h = hashlib.sha256()
        h.update(section_path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def _extract_confluence_field(
        meta: Any, field: str, default: Any = None
    ) -> Any:
        """Read a field from a confluence_get_page result (dict or obj)."""

        if isinstance(meta, dict):
            return meta.get(field, default)
        return getattr(meta, field, default)

    @staticmethod
    def _extract_confluence_datetime(
        meta: Any, field: str
    ) -> datetime | None:
        """Read a tz-aware datetime field from a confluence_get_page result.

        Accepts either a real :class:`datetime` (preferred — typed
        activity returns) or an ISO-8601 string (dict-shaped returns).
        Returns ``None`` for missing / malformed values rather than
        raising, so the overwrite-protection branch falls through to
        the proceed path on a degraded payload.
        """

        value: Any
        if isinstance(meta, dict):
            value = meta.get(field)
        else:
            value = getattr(meta, field, None)
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else None
        if isinstance(value, str):
            try:
                # ``datetime.fromisoformat`` is a deterministic, pure
                # parser — safe to use inside the workflow body.
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else None
        return None

    @staticmethod
    def _extract_confluence_sections(
        meta: Any, inp: AgentRunnerWorkflowInput
    ) -> list[dict[str, str]]:
        """Return the list of section payloads to update.

        Accepts either a ``confluence_get_page`` activity output that
        already enumerates sections, or — when the activity returns a
        bare body — falls back to a single-section update keyed on
        ``analysis.title``. The shape returned by each entry is a
        mapping with ``section_path`` and ``content`` keys; the
        ``content_hash`` key is optional (we recompute when absent).
        """

        sections: list[dict[str, str]] = []
        raw_sections: Any
        if isinstance(meta, dict):
            raw_sections = meta.get("sections")
        else:
            raw_sections = getattr(meta, "sections", None)

        if isinstance(raw_sections, list) and raw_sections:
            for item in raw_sections:
                if isinstance(item, dict):
                    sections.append(
                        {
                            "section_path": str(
                                item.get("section_path")
                                or item.get("title")
                                or ""
                            ),
                            "content": str(
                                item.get("content")
                                or item.get("body")
                                or ""
                            ),
                            "content_hash": str(item.get("content_hash") or ""),
                        }
                    )
            return [s for s in sections if s["section_path"]]

        # Degraded fallback: single-section update derived from the
        # analysis. Used by the e2e stub that returns a flat body.
        rationale = inp.analysis.rationale or ""
        title = inp.analysis.title or "Section"
        if rationale:
            return [
                {
                    "section_path": title,
                    "content": rationale,
                    "content_hash": "",
                }
            ]
        return []

    @staticmethod
    def _extract_doc_body(response: Any) -> str:
        """Best-effort body extraction from an ``llm_generate_doc`` result."""

        if isinstance(response, dict):
            for key in ("body", "content", "text", "answer"):
                value = response.get(key)
                if isinstance(value, str) and value:
                    return value
            return ""
        for attr in ("body", "content", "text", "answer"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value:
                return value
        return str(response) if response is not None else ""

    @staticmethod
    def _extract_confluence_page_url(page_info: Any) -> str | None:
        if isinstance(page_info, dict):
            value = page_info.get("url") or page_info.get("link")
            return str(value) if isinstance(value, str) else None
        value = getattr(page_info, "url", None) or getattr(
            page_info, "link", None
        )
        return str(value) if isinstance(value, str) else None

    @staticmethod
    def _extract_confluence_page_id(page_info: Any) -> str | None:
        if isinstance(page_info, dict):
            value = page_info.get("page_id") or page_info.get("id")
            return str(value) if value is not None else None
        for attr in ("page_id", "id"):
            value = getattr(page_info, attr, None)
            if value is not None:
                return str(value)
        return None

    # -- code_change helpers ----------------------------------------------

    async def _run_execution_child(
        self, inp: AgentRunnerWorkflowInput, commit_hash: str
    ) -> bool:
        """Start an :class:`ExecutionRunWorkflow` child and await its result.

        Returns ``True`` iff the child reported ``status == "passed"``.
        Any other status (``"failed"``, ``"timeout"``) is treated as a
        test failure and the result is cached against ``commit_hash``
        in :attr:`IterationState.test_results_by_diff_hash` so a
        follow-up ``[fix]`` against the same diff hits the re-test
        guard.
        """

        command = getattr(inp.analysis, "execution_command", None) or ""
        child_input = ExecutionRunWorkflowInput(
            parent_workflow_id=workflow.info().workflow_id,
            runner_id=None,
            command=command,
            workdir=None,
            environment=(),
            artifact_minio_prefix=None,
            start_to_close_timeout=None,
            heartbeat_timeout=None,
            department_id=inp.department_id,
            workflow_type=inp.workflow_type,
            trace_id=inp.trace_id,
            needs_docker=bool(getattr(inp.analysis, "needs_docker", False)),
        )

        try:
            output: ExecutionRunWorkflowOutput = (
                await workflow.execute_child_workflow(
                    "ExecutionRunWorkflow",
                    args=[child_input],
                    id=(
                        f"execution-{inp.issue_key}-iter"
                        f"{self._iteration_state.iter_count}"
                    ),
                    task_queue="execution-runner-tq",
                )
            )
        except Exception as exc:  # noqa: BLE001 - test child failure
            workflow.logger.warning(
                "ExecutionRunWorkflow child failed for %s: %s",
                inp.issue_key,
                exc,
            )
            # Cache the failure so a follow-up ``[fix]`` against the
            # same diff hits the re-test guard.
            self._iteration_state = _state_record_test_result(
                self._iteration_state, commit_hash, "failed"
            )
            return False

        # The data converter usually round-trips back into the
        # :class:`ExecutionRunWorkflowOutput` dataclass, but a
        # plain-dict fallback can occur under some SDK / converter
        # combinations (e.g. a child stub returning the bare JSON
        # shape in tests). Accept both shapes so a transient
        # deserialisation hiccup does not silently poison
        # ``test_results_by_diff_hash``.
        if isinstance(output, dict):
            status = output.get("status", "failed")
        else:
            status = getattr(output, "status", "failed")
        self._iteration_state = _state_record_test_result(
            self._iteration_state, commit_hash, str(status)
        )
        return status == "passed"

    async def _maybe_handle_explain(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Render and post a queued ``[explain]`` answer.

        Consumes :attr:`_pending_explain_diff_hash` /
        :attr:`_pending_explain_text` populated by the corresponding
        signal handler. When the answer is already cached
        (``_explain_should_skip_llm`` returned True at signal time)
        the cached entry is replayed verbatim. Otherwise the LLM is
        invoked with a hard 200-word ceiling and the answer is
        cached for the 5-minute TTL.
        """

        if not self._pending_explain_diff_hash:
            return

        diff_hash = self._pending_explain_diff_hash
        prompt_text = self._pending_explain_text or ""

        # Drain the pending fields up-front so a re-entry without a
        # fresh signal does not double-post.
        self._pending_explain_diff_hash = None
        self._pending_explain_text = None

        cached = self._iteration_state.explain_cache.get(diff_hash)
        if cached is not None:
            answer = cached.answer
        else:
            try:
                response = await self._execute_llm_activity(
                    "llm_review_code",
                    args=[
                        {
                            "prompt_template": "explain.md",
                            "diff_hash": diff_hash,
                            "prompt": prompt_text,
                            "max_words": 200,
                        }
                    ],
                    # Conservative pre-flight estimate; the activity
                    # itself enforces the actual token cap.
                    input_tokens=max(1, len(prompt_text) // 4),
                    inp=inp,
                )
            except TokenCapExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - best effort
                workflow.logger.warning(
                    "[explain] LLM call failed for %s: %s",
                    diff_hash,
                    exc,
                )
                self._record_partial_failure("explain_llm")
                return

            answer = self._extract_explain_answer(response)
            self._iteration_state = _state_record_explain_answer(
                self._iteration_state, diff_hash, answer, workflow.now()
            )

        # Post the answer to Jira (PR comment for pr_review flow lands
        # behind a future task that surfaces the PR id explicitly).
        await self._post_jira_comment_best_effort(
            inp, f"🤖 [explain]\n\n{answer}"
        )

    async def _maybe_execute_llm_output_actions(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Run ``_execute_output_actions`` against ``inp.analysis.output_actions``.

        Thin wrapper invoked by every per-workflow-type body (code_change,
        confluence, research, ...) after its primary side effect
        succeeds.  When ``analysis.output_actions`` is empty the call
        is a cheap no-op.  When it carries one or more actions the
        wrapper dispatches via :meth:`_execute_output_actions`; a
        critical failure propagates :class:`_OutputActionCriticalFailure`
        up to ``run`` which translates it into the cancel /
        compensation path.

        """

        actions = inp.analysis.output_actions
        if not actions:
            return
        await self._execute_output_actions(
            actions, workflow.info().workflow_id, inp
        )

    async def _post_jira_comment_best_effort(
        self, inp: AgentRunnerWorkflowInput, body: str
    ) -> None:
        """Post a Jira comment, recording a partial failure on error."""

        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[inp.issue_key, body, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - best effort
            workflow.logger.warning(
                "jira_add_comment failed for %s: %s",
                inp.issue_key,
                exc,
            )
            self._record_partial_failure("jira_comment")

    # -- output_actions partition + apply ----------------------------------

    async def _execute_output_actions(
        self,
        actions: tuple[OutputAction, ...],
        workflow_id: str,
        inp: AgentRunnerWorkflowInput,
    ) -> ApplyResult:
        """Apply a tuple of LLM-emitted ``OutputAction`` values.

        Output action pipeline:

        1. Apply the size-cap helper
           :func:`temporal_shared.output_size_cap.redirect_oversized_payload`
           to every action so payloads above
           :data:`temporal_shared.output_size_cap.MAX_OUTPUT_BYTES`
           are offloaded to MinIO and replaced with a
           ``summary``/``minio_uri``/``size_bytes`` triple.
        2. :func:`temporal_shared.output_actions.partition` splits the
           cap-corrected list into ``(critical, best_effort)`` based on
           the kind classification table — not the carried severity —
           so a malformed ``severity`` field cannot bypass the policy.
        3. Critical actions are applied **first** (and serially): any
           critical failure short-circuits the rest of the critical
           list, raises :class:`_OutputActionCriticalFailure`, and the
           ``run`` body translates that into a compensation chain
           run + ``failed`` workflow status.
        4. Best-effort actions are applied **after** all critical
           actions succeed.  A best-effort failure appends the action
           kind + reason to :attr:`ApplyResult.failed_best_effort`
           and to :attr:`_output_actions_partial` so the final Jira
           comment can list every degraded action.

        The activity dispatch table is :data:`_OUTPUT_ACTION_DISPATCH`.
        Each activity is invoked with two positional arguments:

            (1) the action ``payload`` rendered as a ``dict`` (the
                wire-friendly shape for activities that already accept
                JSON-encoded inputs); and
            (2) the ``inp.department_id`` for the credential lookup.

        Activities that need additional context (``issue_key``,
        ``workflow_id``, etc.) read it from the payload — the LLM
        analysis is responsible for populating those keys when it
        emits the action.

        Parameters
        ----------
        actions:
            Tuple of LLM-emitted :class:`OutputAction`.  May be empty —
            the function is a no-op for empty tuples.
        workflow_id:
            Temporal workflow id, formatted via
            :mod:`temporal_shared.identifiers`.  Forwarded to
            :func:`redirect_oversized_payload` so MinIO offloads land
            under ``ai-runs/{workflow_id}/output-{idx}.json``.
        inp:
            The workflow input — used for ``department_id`` (credential
            lookup) and audit context.

        Returns
        -------
        ApplyResult
            Per-action success / failure record.  When every critical
            action succeeded :attr:`ApplyResult.failed_critical` is
            empty.  Best-effort failures are reported through the
            return value but do not raise.

        Raises
        ------
        _OutputActionCriticalFailure
            When at least one critical action fails.  Carries the
            partially-populated :class:`ApplyResult` so the caller's
            exception handler can render the final Jira comment.
        """

        result = ApplyResult()
        if not actions:
            return result

        # ----- 1. Cap correction ----------------------------------
        cap_corrected: list[OutputAction] = []
        for idx, action in enumerate(actions):
            corrected = await redirect_oversized_payload(
                action,
                workflow_id,
                idx,
                self._minio_offload,
            )
            cap_corrected.append(corrected)

        # ----- 2. Partition ---------------------------------------
        try:
            critical, best_effort = partition_output_actions(cap_corrected)
        except (TypeError, ValueError) as exc:
            # An LLM emitting an unclassified kind is a programming
            # bug; log + audit and treat the run as a critical
            # failure so compensation runs.
            workflow.logger.warning(
                "partition() rejected output_actions for %s: %s",
                workflow_id,
                exc,
            )
            self._failure_reason = OUTPUT_ACTION_CRITICAL_FAILED_REASON
            result.failed_critical.append(
                ("partition", f"{type(exc).__name__}: {exc}")
            )
            await self._merge_output_actions_into_log(result, inp)
            raise _OutputActionCriticalFailure(result) from exc

        # ----- 3. Critical first ----------------------------------
        for action in critical:
            success, reason = await self._apply_single_output_action(
                action, inp
            )
            if success:
                result.successful_critical.append(action.kind)
            else:
                result.failed_critical.append((action.kind, reason))
                # Critical failure short-circuits the rest of the list:
                # the workflow MUST NOT keep applying side effects after
                # the first critical refusal.
                self._failure_reason = OUTPUT_ACTION_CRITICAL_FAILED_REASON
                await self._merge_output_actions_into_log(result, inp)
                raise _OutputActionCriticalFailure(result)

        # ----- 4. Best-effort last --------------------------------
        for action in best_effort:
            success, reason = await self._apply_single_output_action(
                action, inp
            )
            if success:
                result.successful_best_effort.append(action.kind)
            else:
                result.failed_best_effort.append((action.kind, reason))
                # Mirror the failure into the workflow-level
                # ``_output_actions_partial`` list so the existing
                # final-summary path (and its Jira comment) sees the
                # degraded action verbatim.
                self._record_partial_failure(action.kind)

        await self._merge_output_actions_into_log(result, inp)
        return result

    async def _apply_single_output_action(
        self,
        action: OutputAction,
        inp: AgentRunnerWorkflowInput,
    ) -> tuple[bool, str]:
        """Dispatch *action* to the matching activity.

        Returns ``(True, "")`` on success and ``(False, reason)`` on
        failure.  The reason string is the exception's ``str(exc)``
        truncated to a single line so it can land verbatim in the
        final Jira comment without breaking the expected layout.
        """

        activity_name = _OUTPUT_ACTION_DISPATCH.get(action.kind)
        if activity_name is None:
            # Unknown kind — should be unreachable because partition()
            # rejects unclassified kinds, but defensive handling lets
            # tests mock the dispatch table without crashing.
            return False, f"no_dispatch_for_kind:{action.kind}"

        payload_dict = dict(action.payload)
        activity_args: list[Any]
        if action.kind == "jira_comment":
            body = (
                payload_dict.get("body")
                or payload_dict.get("comment")
                or payload_dict.get("text")
                or payload_dict.get("content")
                or payload_dict.get("summary")
                or ""
            )
            if payload_dict.get("minio_uri") and payload_dict.get("summary"):
                body = f"{body}\n\nArtifact: {payload_dict['minio_uri']}"
            activity_args = [
                payload_dict.get("issue_key") or inp.issue_key,
                str(body),
                inp.department_id,
            ]
        elif action.kind == "jira_attachment":
            activity_args = [
                payload_dict.get("issue_key") or inp.issue_key,
                payload_dict.get("bucket") or "ai-runs",
                payload_dict.get("key") or payload_dict.get("minio_key") or "",
                payload_dict.get("file_name") or payload_dict.get("filename") or "result.md",
                inp.department_id,
            ]
        elif action.kind == "confluence_create_page":
            activity_args = [
                payload_dict.get("space_key") or payload_dict.get("space") or "",
                payload_dict.get("title") or inp.analysis.title or inp.issue_key,
                payload_dict.get("content")
                or payload_dict.get("body")
                or payload_dict.get("text")
                or "",
                inp.department_id,
                payload_dict.get("parent_id"),
            ]
        elif action.kind == "confluence_update_page":
            activity_args = [
                payload_dict.get("page_id") or payload_dict.get("id") or "",
                payload_dict.get("title") or inp.analysis.title or inp.issue_key,
                payload_dict.get("content")
                or payload_dict.get("body")
                or payload_dict.get("text")
                or "",
                inp.department_id,
            ]
        elif action.kind == "bitbucket_create_pr":
            repo = {
                "workspace": payload_dict.get("workspace")
                or payload_dict.get("project_key")
                or "",
                "repo_slug": payload_dict.get("repo_slug")
                or payload_dict.get("repository")
                or inp.target_repo
                or "",
            }
            activity_args = [
                repo,
                payload_dict.get("source_branch")
                or payload_dict.get("from_branch")
                or "",
                payload_dict.get("target_branch")
                or payload_dict.get("to_branch")
                or inp.target_branch
                or "main",
                payload_dict.get("title") or inp.analysis.title or inp.issue_key,
                payload_dict.get("description") or payload_dict.get("body") or "",
                inp.department_id,
            ]
        else:
            activity_args = [payload_dict, inp.department_id]
        try:
            await workflow.execute_activity(
                activity_name,
                args=activity_args,
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 - per-action triage
            reason_raw = str(exc) or type(exc).__name__
            # Single-line truncation keeps the Jira comment intact.
            reason = reason_raw.splitlines()[0][:200]
            workflow.logger.warning(
                "output_action %s (%s) failed: %s",
                action.kind,
                activity_name,
                reason,
            )
            return False, reason
        return True, ""

    async def _merge_output_actions_into_log(
        self,
        result: ApplyResult,
        inp: AgentRunnerWorkflowInput,
    ) -> None:
        """Merge *result* into the running :attr:`_output_actions_log`.

        Pure list extension — no clocks, no randomness — so the
        merged log replays deterministically.  *inp* is currently
        unused but retained in the signature so a future audit hook
        audit on partial failure can be threaded through
        without changing call sites.
        """

        del inp  # reserved for future audit emission
        self._output_actions_log.successful_critical.extend(
            result.successful_critical
        )
        self._output_actions_log.failed_critical.extend(
            result.failed_critical
        )
        self._output_actions_log.successful_best_effort.extend(
            result.successful_best_effort
        )
        self._output_actions_log.failed_best_effort.extend(
            result.failed_best_effort
        )

    async def _minio_offload(self, *, key: str, body: bytes) -> str:
        """Async callback used by :func:`redirect_oversized_payload`.

        Delegates to the ``minio_put_output_action`` activity and
        returns the canonical S3-style URI emitted by the activity.
        Shape mirrors :class:`temporal_shared.output_size_cap.MinioCallback`.
        Failure of the activity propagates to the caller — the
        size-cap branch is part of the critical pipeline, so a
        MinIO outage during the offload is treated as a critical
        failure rather than a degraded best-effort action.
        """

        uri = await workflow.execute_activity(
            "minio_put_output_action",
            args=[key, body],
            start_to_close_timeout=_SHORT_TIMEOUT,
            retry_policy=_DEFAULT_RETRY,
        )
        # ``minio_put_output_action`` is documented to return the
        # canonical ``s3://{bucket}/{key}`` URI as a string. Coerce
        # defensively so a malformed activity response surfaces a
        # ``TypeError`` at the cap-helper boundary instead of leaking
        # a non-string into the rewritten payload.
        if not isinstance(uri, str) or not uri:
            raise TypeError(
                "minio_put_output_action must return a non-empty string"
            )
        return uri

    # -- Activity-result extractors ----------------------------------------
    #
    # The activities return either their declared frozen dataclasses
    # (``ScanResult``, ``CommitInfo``, ``PRInfo``, ``PRDiff``) or a
    # raw dict when Temporal's data converter cannot resolve the
    # dataclass at the workflow boundary (e.g. tests that mock the
    # activity return value with a plain dict). The helpers below
    # accept both shapes so unit tests can drive the workflow without
    # importing the activity dataclasses.

    @staticmethod
    def _build_code_generation_prompt(inp: AgentRunnerWorkflowInput) -> str:
        """Build the deterministic prompt for code-change generation."""

        parts = [
            f"Issue: {inp.issue_key}",
            f"Workflow: {inp.workflow_type}",
            f"Repository: {inp.target_repo or inp.analysis.target_repo or ''}",
            f"Branch: {inp.target_branch or inp.analysis.target_branch or ''}",
            f"Title: {inp.analysis.title or ''}",
            "Task details:",
            inp.analysis.rationale or "",
        ]
        command = getattr(inp.analysis, "execution_command", None)
        if command:
            parts.extend(["Test command:", str(command)])
        parts.append(
            "Return JSON with files: [{path, content, action}], where action "
            "is create, update, or delete. Do not omit file contents for "
            "create/update."
        )
        return "\n".join(parts)

    @staticmethod
    def _normalise_commit_action(value: Any) -> str:
        action = str(value or "update").strip().lower()
        if action in {"created", "add", "added", "new"}:
            return "create"
        if action in {"modified", "modify", "changed", "change"}:
            return "update"
        if action in {"deleted", "remove", "removed"}:
            return "delete"
        if action in {"create", "update", "delete"}:
            return action
        return "update"

    @classmethod
    def _extract_commit_files(cls, code_output: Any) -> list[dict[str, str]]:
        """Normalise OpenCode output for Bitbucket commit."""

        raw_files: Any
        if isinstance(code_output, dict):
            raw_files = code_output.get(
                "files", code_output.get("files_changed", [])
            )
        else:
            raw_files = getattr(
                code_output,
                "files",
                getattr(code_output, "files_changed", []),
            )

        if not isinstance(raw_files, list):
            return []

        files: list[dict[str, str]] = []
        for raw in raw_files:
            if not isinstance(raw, dict):
                continue
            path = str(
                raw.get("path")
                or raw.get("file")
                or raw.get("filename")
                or ""
            ).strip().replace("\\", "/")
            if not path or path.startswith("/") or ".." in path.split("/"):
                continue
            action = cls._normalise_commit_action(raw.get("action"))
            content_value = raw.get("content", raw.get("code", ""))
            content = "" if content_value is None else str(content_value)
            if action != "delete" and not content:
                continue
            files.append(
                {
                    "path": path,
                    "content": content,
                    "action": action,
                }
            )
        return files

    @staticmethod
    def _extract_code_diff_text(
        code_output: Any,
        files: list[dict[str, str]],
    ) -> str:
        if isinstance(code_output, dict):
            for key in ("diff_content", "diff", "patch"):
                value = code_output.get(key)
                if value:
                    return str(value)
            explanation = str(code_output.get("explanation", ""))
        else:
            for attr in ("diff_content", "diff", "patch"):
                value = getattr(code_output, attr, None)
                if value:
                    return str(value)
            explanation = str(getattr(code_output, "explanation", ""))
        summary = "\n".join(
            f"{f['action']} {f['path']}\n{f['content']}" for f in files
        )
        return "\n".join(part for part in (explanation, summary) if part)

    @staticmethod
    def _extract_precommit_decision(scan_result: Any) -> str:
        if isinstance(scan_result, dict):
            return str(scan_result.get("decision", "pass"))
        decision = getattr(scan_result, "decision", "pass")
        return str(decision)

    @staticmethod
    def _extract_commit_hash(commit_info: Any) -> str | None:
        if isinstance(commit_info, dict):
            value = commit_info.get("commit_hash")
            return str(value) if value is not None else None
        value = getattr(commit_info, "commit_hash", None)
        return str(value) if value is not None else None

    @staticmethod
    def _extract_pr_url(pr_info: Any) -> str | None:
        if isinstance(pr_info, dict):
            value = pr_info.get("url")
            return str(value) if value is not None else None
        value = getattr(pr_info, "url", None)
        return str(value) if value is not None else None

    @staticmethod
    def _extract_pr_id_from_info(pr_info: Any) -> int | None:
        """Recover the integer PR id from a PR-create activity result.

        Mirrors :meth:`_extract_pr_url` but pulls ``id`` /
        ``pr_id`` keys (the dict variant) or attributes (the
        :class:`PRInfo` dataclass variant emitted by
        :mod:`activities.bitbucket`). Returns ``None`` when no
        recoverable integer is available; callers fall
        through gracefully so a malformed PR-create response cannot
        silently break the supersede chain.
        """

        if isinstance(pr_info, dict):
            for key in ("pr_id", "id"):
                value = pr_info.get(key)
                if isinstance(value, bool):
                    # ``bool`` is a subclass of ``int``; reject so
                    # ``draft=True`` does not get mistaken for an id.
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.isdigit():
                    return int(value)
            return None
        for attr in ("pr_id", "id"):
            value = getattr(pr_info, attr, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
        return None

    @staticmethod
    def _extract_diff_text(diff: Any) -> str:
        if isinstance(diff, dict):
            return str(diff.get("diff_content", ""))
        return str(getattr(diff, "diff_content", ""))

    @staticmethod
    def _extract_findings(review: Any) -> list[dict]:
        if isinstance(review, dict):
            findings = review.get("findings")
            if isinstance(findings, list):
                return [f for f in findings if isinstance(f, dict)]
            return []
        findings = getattr(review, "findings", None)
        if isinstance(findings, list):
            return [f for f in findings if isinstance(f, dict)]
        return []

    @staticmethod
    def _extract_pr_id(inp: AgentRunnerWorkflowInput) -> int:
        """Best-effort PR id extraction for the ``pr_review`` flow.

        The PR id is sourced from the analysis ``rationale`` (the
        gateway stashes it there). The rationale is free-form prose
        that may mention the same number several times, so the first
        contiguous run of digits is taken as the id rather than
        concatenating every digit in the text. Defaults to 0 so the
        downstream activity emits a clear error when the upstream
        payload is malformed.
        """

        rationale = inp.analysis.rationale or ""
        match = re.search(r"\d+", rationale)
        if match:
            try:
                return int(match.group()[:9])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def _extract_explain_answer(response: Any) -> str:
        if isinstance(response, dict):
            return str(
                response.get("answer")
                or response.get("text")
                or response.get("content")
                or ""
            )
        for attr in ("answer", "text", "content"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value:
                return value
        return ""

    # -- Cancel handling ---------------------------------------------------

    async def _handle_cancel(
        self, inp: AgentRunnerWorkflowInput
    ) -> AgentRunnerWorkflowOutput:
        """Run the deterministic compensation chain.

        Idempotent: :attr:`_compensation_running` is set to
        ``True`` *before* the activity dispatch so a cancel signal
        that arrives during the chain (replay, rapid double-tap, etc.)
        observes the latched state via :meth:`cancel_requested` and
        returns without re-firing. Combined with the activity-level
        idempotency contract (every compensation step is a no-op when
        the side effect is already cleaned up — see
        ``temporal_shared.compensation``) this gives an
        exactly-once compensation guarantee at the workflow layer.

        The audit row uses :data:`CANCEL_BY_END_USER_AUDIT_ACTION` or
        :data:`CANCEL_BY_ADMIN_AUDIT_ACTION` according to
        :attr:`_cancel_actor_role`. The role mapping happens
        through :func:`_audit_action_for_cancel_role`, which falls
        back to ``end_user`` for unknown / blank values so a
        misconfigured caller still produces a usable audit trail.
        """

        # Idempotency latch — set *before* the activity call so a
        # cancel signal arriving during the chain is a no-op.
        self._compensation_running = True

        # Run the compensation activities. The activity itself enforces
        # its own retry policy and idempotence (see
        # ``temporal_shared.compensation``). When the activity is not
        # yet wired up we fall through gracefully
        # so the cancel still terminates the workflow.
        #
        # ``start_to_close_timeout=_SHORT_TIMEOUT`` (= 2 minutes / 120s)
        # covers the full chain (six activities × 30s budgets each)
        # for the compensation contract; individual steps are retried inside
        # the chain activity according to its own ``maximumAttempts=3``
        # rule.
        try:
            await workflow.execute_activity(
                "compensation_chain_run",
                args=[
                    {
                        "workflow_id": workflow.info().workflow_id,
                        "dept_id": inp.department_id,
                        "issue_key": inp.issue_key,
                        "actor_id": self._cancel_actor_id,
                        "actor_role": self._cancel_actor_role,
                        "reason": self._cancel_reason,
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 - compensation best-effort
            workflow.logger.warning(
                "compensation_chain_run failed for %s — continuing",
                workflow.info().workflow_id,
            )

        # Emit the role-mapped audit row. Best-effort — the
        # workflow still terminates with ``cancelled`` even if the
        # audit activity is unavailable.
        await self._emit_audit_action(
            _audit_action_for_cancel_role(self._cancel_actor_role), inp
        )

        return self._build_output(
            status="cancelled",
            summary="🤖 Bu task iptal edildi.",
        )

    # -- iter==3 banner + audit drains -------------------------------------

    async def _maybe_post_iter_warning_banner(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Post the iter==3 banner once per workflow.

        Idempotent: the banner activity is invoked at most one time
        across the lifetime of an :class:`AgentRunnerWorkflow`
        execution. The flag :attr:`_iter_warning_at_three` is set
        **before** the activity returns so a transient activity
        failure cannot cause the banner to be posted twice on the
        next loop turn — at-most-once is preferred over
        at-least-once for user-facing comments (the banner is a soft
        warning, not a contractual guarantee).

        Sets the banner state field to
        ``iter_warning_at_three=True``.
        """

        if self._iter_warning_at_three:
            self._iter_warning_pending = False
            return
        if not self._iter_warning_pending:
            return
        if (
            self._iteration_state.iter_count < ITER_WARNING_THRESHOLD
        ):  # defensive — flag should not have been armed below threshold
            self._iter_warning_pending = False
            return

        # Flip the flag *first* so a second drain in the same loop
        # iteration is a no-op (the activity itself is best-effort —
        # the contract is "fire once and remember", not "fire until
        # success").
        self._iter_warning_at_three = True
        self._iter_warning_pending = False

        try:
            await workflow.execute_activity(
                "jira_add_comment",
                args=[inp.issue_key, ITER_WARNING_BANNER_TEXT, inp.department_id],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 - banner is best-effort
            workflow.logger.warning(
                "iter_warning_at_three banner activity failed for %s — "
                "continuing without re-fire",
                workflow.info().workflow_id,
            )

        # Audit the banner emission — best-effort, never raises.
        await self._emit_audit_action(ITER_WARNING_AUDIT_ACTION, inp)

    async def _drain_pending_audits(
        self, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Emit any audit actions queued by the signal handlers.

        Signal handlers cannot ``await`` activities, so they queue an
        action name in :attr:`_pending_audit_actions` and rely on the
        body to emit them on the next loop turn. This keeps the audit
        trail aligned with the "audit on every keyword decision" behavior
        without violating
        Temporal's signal-handler I/O restriction.
        """

        if not self._pending_audit_actions:
            return

        # Snapshot + clear so a fresh signal during the drain doesn't
        # cause double emission. We rebuild the list after the drain
        # completes — emit failures are swallowed (best-effort).
        pending = list(self._pending_audit_actions)
        self._pending_audit_actions = []
        for action in pending:
            await self._emit_audit_action(action, inp)

    async def _emit_audit_action(
        self, action: str, inp: AgentRunnerWorkflowInput
    ) -> None:
        """Best-effort emission of an audit row via ``audit_emit``.

        The audit activity is registered separately; when the worker is
        not yet hosting it the call
        falls through silently with a workflow-logger warning so the
        primary side effect of the parent operation is unaffected.
        """

        try:
            await workflow.execute_activity(
                "audit_emit",
                args=[
                    {
                        "action": action,
                        "workflow_id": workflow.info().workflow_id,
                        "dept_id": inp.department_id,
                        "issue_key": inp.issue_key,
                        "iter_count": self._iteration_state.iter_count,
                    }
                ],
                start_to_close_timeout=_SHORT_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception:  # noqa: BLE001 - audit is best-effort
            workflow.logger.warning(
                "audit_emit(%s) failed for %s — continuing",
                action,
                workflow.info().workflow_id,
            )

    # -- LLM activity execution with token cap -----------------------------

    async def _execute_llm_activity(
        self,
        activity_name: str,
        *,
        args: list[Any],
        input_tokens: int,
        inp: AgentRunnerWorkflowInput,
        start_to_close_timeout: timedelta = _LLM_TIMEOUT,
        token_cap: int = MAX_ACTIVITY_TOKEN_CAP,
    ) -> Any:
        """Invoke an LLM activity with the fail-fast token cap.

        Pre-flight check: when ``input_tokens`` exceeds
        :data:`MAX_ACTIVITY_TOKEN_CAP` the workflow refuses to call
        the activity, emits a :data:`TOKEN_CAP_AUDIT_ACTION` audit
        row, and raises :class:`TokenCapExceededError` (which is
        non-retryable so Temporal's retry machinery does not attempt
        a second call).

        The activity itself is invoked with :data:`LLM_RETRY_POLICY`
        (``maximum_attempts=1``) so an in-activity overflow (i.e. one
        the workflow could not pre-flight because the token count was
        only known after the prompt was rendered) also fails fast.

        Enforces the activity-level token cap.

        Parameters
        ----------
        activity_name:
            Registered activity name (e.g. ``"llm_analyze_task"``).
        args:
            Positional arguments forwarded to the activity.
        input_tokens:
            Caller-computed token count for the activity input. The
            workflow is responsible for accurate counting; the cap is
            enforced verbatim against this value.
        inp:
            The workflow input — used for audit context.
        start_to_close_timeout:
            Per-attempt timeout; defaults to :data:`_LLM_TIMEOUT`.
        token_cap:
            Override the cap for tests; defaults to
            :data:`MAX_ACTIVITY_TOKEN_CAP`.
        """

        if input_tokens > token_cap:
            # Audit the refusal *before* raising so the row exists
            # even if the workflow terminates immediately.
            await self._emit_audit_action(TOKEN_CAP_AUDIT_ACTION, inp)
            raise TokenCapExceededError(
                activity_name=activity_name,
                input_tokens=input_tokens,
                cap=token_cap,
            )

        return await workflow.execute_activity(
            activity_name,
            args=args,
            start_to_close_timeout=start_to_close_timeout,
            retry_policy=LLM_RETRY_POLICY,
        )

    # -- Output construction ----------------------------------------------

    def _build_output(
        self, *, status: AgentRunnerStatus, summary: str
    ) -> AgentRunnerWorkflowOutput:
        """Assemble the terminal :class:`AgentRunnerWorkflowOutput`."""

        return AgentRunnerWorkflowOutput(
            status=status,
            iter_count=self._iteration_state.iter_count,
            summary=summary,
            partial_failure_actions=tuple(self._output_actions_partial),
            failure_reason=self._failure_reason,
            confluence_page_id=self._latest_confluence_page_id,
        )

    # -- Internal helpers --------------------------------------------------

    def _record_partial_failure(self, action_kind: str) -> None:
        """Mark a best-effort action as failed for the final summary."""

        self._output_actions_partial.append(action_kind)

    @staticmethod
    def _coerce_comment_signal(
        payload: Any,
        text_field: str,
        *,
        diff_field: str = "diff_hash",
    ) -> tuple[str, str | None, str | None]:
        """Normalise a comment-style signal payload to ``(text, actor, hash)``.

        Temporal converts JSON payloads to dicts when the wire schema
        doesn't match a registered dataclass. Accepting both the typed
        and the raw form keeps the workflow interoperable with handlers
        that use either.
        """

        if isinstance(payload, (CommentAddedSignal, FixTriggeredSignal)):
            return (
                getattr(payload, text_field, "") or "",
                payload.actor_account_id,
                payload.diff_hash,
            )
        if isinstance(payload, ExplainTriggeredSignal):
            return (
                getattr(payload, text_field, "") or "",
                payload.actor_account_id,
                payload.pr_diff_hash,
            )
        if isinstance(payload, dict):
            text_value = payload.get(text_field, "")
            text = str(text_value) if text_value is not None else ""
            actor_value = payload.get("actor_account_id")
            actor = str(actor_value) if isinstance(actor_value, str) else None
            diff_value = payload.get(diff_field)
            if diff_value is None and diff_field != "diff_hash":
                diff_value = payload.get("diff_hash")
            diff = (
                str(diff_value) if isinstance(diff_value, str) else None
            )
            return text, actor, diff
        # Fallback: stringify the entire payload.
        return str(payload) if payload is not None else "", None, None

    @staticmethod
    def _coerce_cancel_signal(payload: Any) -> tuple[str, str, str]:
        """Normalise a cancel signal payload to ``(actor_id, actor_role, reason)``.

        Accepts both the typed :class:`CancelRequestedSignal` and the
        raw dict produced by the Temporal data converter when the wire
        schema doesn't match a registered dataclass. The
        ``actor_role`` field is normalised through the closed
        vocabulary in :data:`_CANCEL_RECOGNISED_ROLES` — unknown,
        empty, or ``None`` values default to
        :data:`_CANCEL_ROLE_END_USER`.
        """

        if isinstance(payload, CancelRequestedSignal):
            actor_id = payload.actor_id
            role = payload.actor_role
            reason = payload.reason
        elif isinstance(payload, dict):
            actor_value = payload.get("actor_id", "")
            role_value = payload.get("actor_role", _CANCEL_ROLE_END_USER)
            reason_value = payload.get("reason", "user_cancel")
            actor_id = (
                str(actor_value) if actor_value is not None else ""
            )
            role = (
                str(role_value)
                if role_value is not None
                else _CANCEL_ROLE_END_USER
            )
            reason = (
                str(reason_value)
                if reason_value is not None
                else "user_cancel"
            )
        else:
            # Fallback — preserve the operator's intent without crashing.
            return "", _CANCEL_ROLE_END_USER, "user_cancel"

        # Closed-vocabulary check. Unknown roles default to
        # ``end_user`` so a misconfigured caller still produces a
        # well-formed audit row.
        if role not in _CANCEL_RECOGNISED_ROLES:
            role = _CANCEL_ROLE_END_USER
        return actor_id, role, reason


# ---------------------------------------------------------------------------
# Internal sentinel exceptions
#
# These are caught by ``run`` to translate body-side termination signals
# into the appropriate :class:`AgentRunnerWorkflowOutput`. They are NOT
# part of the public API — exporting them would tempt callers to raise
# them from outside the workflow body, which would bypass the cancel /
# out-of-scope state machine.
# ---------------------------------------------------------------------------


class _CancelledViaSignal(Exception):
    """Raised internally when the body observes ``_cancel_requested``."""


class _OutOfScope(Exception):
    """Raised internally when the body observes the iter / needs_info cap."""


class _EpicSubtaskFailed(Exception):
    """Raised internally when an Epic ``multi_step`` subtask child fails.

    Caught by the ``run`` body's generic handler, which returns a
    ``failed`` terminal status carrying the ``epic_subtask_failed``
    reason already set on the workflow. No compensation chain runs —
    the Epic parent itself opened no draft PR / page to roll back; the
    failed subtask child handles its own compensation.
    """

    def __init__(self, child_key: str) -> None:
        super().__init__(f"epic subtask failed: {child_key}")
        self.child_key = child_key


class _OutputActionCriticalFailure(Exception):
    """Raised by :meth:`AgentRunnerWorkflow._execute_output_actions` when at
    least one critical output action fails.

    The ``run`` body catches this sentinel exactly the same way it
    catches ``cancel_requested`` — it triggers the
    ``compensation_chain_run`` activity before terminating with
    ``failed`` and the stable failure reason
    :data:`OUTPUT_ACTION_CRITICAL_FAILED_REASON`.

    Carries the :class:`ApplyResult` produced by the failed run so
    the cancel branch can render the final Jira comment with the
    completed-critical / failed list intact.
    """

    def __init__(self, apply_result: ApplyResult) -> None:
        super().__init__("critical output action failed")
        self.apply_result = apply_result


__all__ = [
    "AgentRunnerWorkflow",
    "CancelRequestedSignal",
    "CommentAddedSignal",
    "ExplainTriggeredSignal",
    "FixTriggeredSignal",
    "TokenCapExceededError",
    "MAX_ITER",
    "MAX_ACTIVITY_TOKEN_CAP",
    "FIX_DEBOUNCE_WINDOW",
    "EXPLAIN_CACHE_TTL",
    "NEEDS_INFO_MAX_STREAK",
    "ITER_WARNING_THRESHOLD",
    "ITER_WARNING_BANNER_TEXT",
    "ITER_WARNING_AUDIT_ACTION",
    "TOKEN_CAP_AUDIT_ACTION",
    "TOKEN_CAP_ERROR_TYPE",
    "FIX_DEBOUNCE_AUDIT_ACTION",
    "FIX_RETEST_PROTECTED_AUDIT_ACTION",
    "EXPLAIN_CACHE_HIT_AUDIT_ACTION",
    "CONFLUENCE_PROBE_PAGE_SKIPPED_AUDIT_ACTION",
    "LLM_RETRY_POLICY",
    "SIGNAL_WAIT_TIMEOUT",
]
