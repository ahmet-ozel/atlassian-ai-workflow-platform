"""Unit tests for ``AgentRunnerWorkflow`` signal handlers and helpers.

Validates the signal-handler wiring:

    1. ``comment_added`` keyword routing - ``[fix]`` /  ``[explain]`` /
       ``[needs_info]`` markers dispatch to the matching internal
       handler; plain comments fall through to the iter-advance path.
    2. ``[fix]`` debounce + diff-hash dedup branches each queue the
       correct audit action (``fix_debounce_dropped`` /
       ``fix_re_test_protected``) without re-advancing ``iter_count``.
    3. ``[explain]`` cache hit serves the cached answer with no iter
       advance and queues ``explain_cache_hit`` audit.
    4. ``[needs_info]`` streak → ``out_of_scope`` once the cap is hit.
    5. iter==3 banner - ``_iter_warning_pending`` arms exactly once the
       first time ``iter_count`` crosses :data:`ITER_WARNING_THRESHOLD`;
       a fourth advance does not re-arm.
    6. :class:`TokenCapExceededError` is non-retryable with the stable
       type discriminator and the workflow-level
       :meth:`AgentRunnerWorkflow._execute_llm_activity` helper raises
       it pre-flight (no activity call) when ``input_tokens`` exceeds
       :data:`MAX_ACTIVITY_TOKEN_CAP`.

The tests exercise the signal handlers as plain Python methods -
:meth:`AgentRunnerWorkflow.comment_added` and friends are bound
methods that mutate workflow state but do not themselves ``await``
activities, so they remain testable outside a Temporal worker once
``workflow.now`` is monkey-patched into a deterministic clock.

"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio import workflow as _temporal_workflow
from temporalio.exceptions import ApplicationError

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping - the canonical workflow ships under
# ``src/agent_runner/`` (mirrors ``hatchling`` ``packages = ["src",
# "src/agent_runner"]``) and pulls ``temporal_shared.*`` from the
# foundation lib package.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_SRC_DIR, _TEMPORAL_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

# noqa: E402 below - import after sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    EXPLAIN_CACHE_HIT_AUDIT_ACTION,
    EXPLAIN_CACHE_TTL,
    FIX_DEBOUNCE_AUDIT_ACTION,
    FIX_DEBOUNCE_WINDOW,
    FIX_RETEST_PROTECTED_AUDIT_ACTION,
    ITER_WARNING_THRESHOLD,
    LLM_RETRY_POLICY,
    MAX_ACTIVITY_TOKEN_CAP,
    MAX_ITER,
    NEEDS_INFO_MAX_STREAK,
    TOKEN_CAP_AUDIT_ACTION,
    TOKEN_CAP_ERROR_TYPE,
    AgentRunnerWorkflow,
    CommentAddedSignal,
    ExplainTriggeredSignal,
    FixTriggeredSignal,
    TokenCapExceededError,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    ExplainCacheEntry,
    IterationState,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    """Anchor clock used by the deterministic ``workflow.now`` stub."""

    return datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def patched_workflow_now(fixed_now: datetime, monkeypatch: pytest.MonkeyPatch):
    """Replace ``workflow.now`` with a mutable clock for signal-handler tests.

    The handlers call ``workflow.now()`` to check the ``[fix]`` debounce
    window and the ``[explain]`` cache TTL. We expose a small helper
    so each test can advance the clock without restarting the runtime.
    """

    state = {"now": fixed_now}

    def _now() -> datetime:
        return state["now"]

    monkeypatch.setattr(_temporal_workflow, "now", _now)

    class _Clock:
        @property
        def value(self) -> datetime:
            return state["now"]

        def advance(self, delta: timedelta) -> None:
            state["now"] = state["now"] + delta

        def set(self, when: datetime) -> None:
            state["now"] = when

    return _Clock()


@pytest.fixture
def workflow_input() -> AgentRunnerWorkflowInput:
    """Minimal :class:`AgentRunnerWorkflowInput` for body-side tests."""

    analysis = LlmAnalysisResult(
        workflow_type="code_change_with_test",
        confidence="high",
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4211",
        title="Fix payment timeout",
        rationale="LLM-resolved",
        token_usage=120,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-4211",
        issue_key="PAY-4211",
        department_id="payments",
        workflow_type="code_change_with_test",
        analysis=analysis,
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4211",
        iteration=1,
        max_iter=MAX_ITER,
        default_language="tr",
    )


@pytest.fixture
def make_wf():
    """Factory returning a fresh :class:`AgentRunnerWorkflow` instance."""

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        # Mirror what ``run`` would set up on its first turn so the
        # signal handlers operate on a non-zero iter_count.
        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


# ---------------------------------------------------------------------------
# 1. ``comment_added`` keyword routing
# ---------------------------------------------------------------------------


class TestCommentAddedKeywordRouting:
    """``comment_added`` routes ``[fix]`` / ``[explain]`` / ``[needs_info]``
    markers to the matching internal handler.
    """

    def test_plain_comment_advances_iter(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        before = wf._iteration_state.iter_count

        wf.comment_added(
            CommentAddedSignal(
                comment_text="Lütfen testleri tekrar gözden geçirin.",
                actor_account_id="user-1",
            )
        )

        assert wf._iteration_state.iter_count == before + 1
        assert wf._signal_pending is True
        assert wf._latest_comment == (
            "Lütfen testleri tekrar gözden geçirin."
        )
        assert wf._pending_audit_actions == []

    def test_fix_keyword_routes_to_fix_handler(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        before = wf._iteration_state.iter_count

        wf.comment_added(
            CommentAddedSignal(
                comment_text="[fix] please re-run the failing test",
                actor_account_id="reviewer-1",
                diff_hash="abc123",
            )
        )

        # Fix routing advanced iter and recorded the trigger time.
        assert wf._iteration_state.iter_count == before + 1
        assert wf._iteration_state.last_fix_trigger_at == (
            patched_workflow_now.value
        )
        assert wf._pending_fix_diff_hash == "abc123"

    def test_explain_keyword_routes_to_explain_handler(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        before = wf._iteration_state.iter_count

        wf.comment_added(
            CommentAddedSignal(
                comment_text="[explain] what changed?",
                actor_account_id="reviewer-1",
                diff_hash="diff-9",
            )
        )

        # Cold cache → iter advances and the explain payload is staged
        # for the body's next loop turn.
        assert wf._iteration_state.iter_count == before + 1
        assert wf._pending_explain_diff_hash == "diff-9"
        assert (
            wf._pending_explain_text == "[explain] what changed?"
        )

    def test_needs_info_keyword_does_not_advance_iter(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        before = wf._iteration_state.iter_count

        wf.comment_added(
            CommentAddedSignal(
                comment_text="[needs_info] hangi servisten bahsediyorsun?",
                actor_account_id="reporter-1",
            )
        )

        # ``[needs_info]`` increments the streak but never advances the
        # iter counter.
        assert wf._iteration_state.iter_count == before
        assert wf._iteration_state.needs_info_streak == 1
        assert wf._out_of_scope is False

    def test_keyword_matching_is_case_insensitive(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()

        wf.comment_added(
            CommentAddedSignal(
                comment_text="[FIX] retry",
                actor_account_id="reviewer-1",
                diff_hash="X",
            )
        )

        assert wf._iteration_state.last_fix_trigger_at is not None

    def test_word_fix_without_brackets_falls_through(
        self, make_wf, patched_workflow_now
    ) -> None:
        """A bare ``fix`` mention must not trip the keyword router."""

        wf = make_wf()
        before_fix_at = wf._iteration_state.last_fix_trigger_at

        wf.comment_added(
            CommentAddedSignal(
                comment_text="we will fix this later",
                actor_account_id="user-1",
            )
        )

        # No fix trigger time recorded → routed through the plain path.
        assert wf._iteration_state.last_fix_trigger_at == before_fix_at


# ---------------------------------------------------------------------------
# 2. ``[fix]`` debounce + diff-hash dedup
# ---------------------------------------------------------------------------


class TestFixDebounceAndDedup:
    """``[fix]`` keyword: 60s debounce + cached-test re-test protection
    behavior.
    """

    def test_consecutive_fix_within_window_is_debounced(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()

        # First ``[fix]`` records the trigger time + advances iter.
        wf.fix_triggered(
            FixTriggeredSignal(comment_text="[fix]", diff_hash="H1")
        )
        first_iter = wf._iteration_state.iter_count
        assert (
            wf._iteration_state.last_fix_trigger_at
            == patched_workflow_now.value
        )

        # Second ``[fix]`` 30s later (well within the 60s window) is
        # silently dropped - no iter advance, audit row queued.
        patched_workflow_now.advance(timedelta(seconds=30))
        wf.fix_triggered(
            FixTriggeredSignal(comment_text="[fix]", diff_hash="H2")
        )

        assert wf._iteration_state.iter_count == first_iter
        assert FIX_DEBOUNCE_AUDIT_ACTION in wf._pending_audit_actions

    def test_same_diff_hash_skips_retest(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        # Seed a cached test result so the re-test guard fires.
        wf._iteration_state = replace(
            wf._iteration_state,
            test_results_by_diff_hash={"H1": "passed"},
        )

        wf.fix_triggered(
            FixTriggeredSignal(comment_text="[fix]", diff_hash="H1")
        )

        # Iter is unchanged - re-test was protected.
        assert wf._iteration_state.iter_count == 1
        assert (
            FIX_RETEST_PROTECTED_AUDIT_ACTION in wf._pending_audit_actions
        )
        # Trigger time was still recorded so the next ``[fix]`` is
        # subject to the debounce window.
        assert (
            wf._iteration_state.last_fix_trigger_at
            == patched_workflow_now.value
        )

    def test_fresh_diff_after_debounce_advances_iter(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf.fix_triggered(
            FixTriggeredSignal(comment_text="[fix]", diff_hash="H1")
        )
        first_iter = wf._iteration_state.iter_count

        # Step beyond the 60s debounce window with a brand-new diff.
        patched_workflow_now.advance(FIX_DEBOUNCE_WINDOW + timedelta(seconds=1))
        wf.fix_triggered(
            FixTriggeredSignal(comment_text="[fix]", diff_hash="H2")
        )

        assert wf._iteration_state.iter_count == first_iter + 1
        assert wf._pending_fix_diff_hash == "H2"


# ---------------------------------------------------------------------------
# 3. ``[explain]`` cooldown + cache
# ---------------------------------------------------------------------------


class TestExplainCacheHit:
    """``[explain]`` keyword: 5-minute LRU cache.
    """

    def test_cache_hit_skips_iter_and_queues_audit(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        # Pre-warm the cache so the next ``[explain]`` hits.
        wf._iteration_state = replace(
            wf._iteration_state,
            explain_cache={
                "diff-9": ExplainCacheEntry(
                    answer="cached answer",
                    issued_at=patched_workflow_now.value,
                )
            },
        )
        before = wf._iteration_state.iter_count

        wf.explain_triggered(
            ExplainTriggeredSignal(
                comment_text="[explain] please",
                pr_diff_hash="diff-9",
            )
        )

        assert wf._iteration_state.iter_count == before
        assert (
            EXPLAIN_CACHE_HIT_AUDIT_ACTION in wf._pending_audit_actions
        )
        assert wf._pending_explain_diff_hash == "diff-9"
        assert wf._pending_explain_text == "[explain] please"

    def test_cache_miss_advances_iter(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        before = wf._iteration_state.iter_count

        wf.explain_triggered(
            ExplainTriggeredSignal(
                comment_text="[explain]", pr_diff_hash="diff-9"
            )
        )

        assert wf._iteration_state.iter_count == before + 1
        assert (
            EXPLAIN_CACHE_HIT_AUDIT_ACTION not in wf._pending_audit_actions
        )

    def test_expired_cache_entry_is_treated_as_miss(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf._iteration_state = replace(
            wf._iteration_state,
            explain_cache={
                "diff-9": ExplainCacheEntry(
                    answer="stale",
                    issued_at=patched_workflow_now.value,
                )
            },
        )
        # Step past the 5-minute TTL → cache miss.
        patched_workflow_now.advance(EXPLAIN_CACHE_TTL + timedelta(seconds=1))

        before = wf._iteration_state.iter_count
        wf.explain_triggered(
            ExplainTriggeredSignal(
                comment_text="[explain]", pr_diff_hash="diff-9"
            )
        )

        assert wf._iteration_state.iter_count == before + 1
        assert (
            EXPLAIN_CACHE_HIT_AUDIT_ACTION not in wf._pending_audit_actions
        )


# ---------------------------------------------------------------------------
# 4. ``[needs_info]`` streak → out_of_scope
# ---------------------------------------------------------------------------


class TestNeedsInfoStreak:
    """``[needs_info]`` cap."""

    def test_streak_terminates_after_max(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()

        for _ in range(NEEDS_INFO_MAX_STREAK):
            wf.comment_added(
                CommentAddedSignal(
                    comment_text="[needs_info] tell me more",
                    actor_account_id="user-1",
                )
            )

        assert wf._iteration_state.needs_info_streak == NEEDS_INFO_MAX_STREAK
        assert wf._out_of_scope is True
        assert wf._failure_reason == "needs_info_loop_cap"

    def test_plain_comment_resets_streak(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf.comment_added(
            CommentAddedSignal(comment_text="[needs_info] hint?")
        )
        wf.comment_added(
            CommentAddedSignal(comment_text="[needs_info] hint?")
        )
        assert wf._iteration_state.needs_info_streak == 2

        # A plain reply provides direction → streak resets to 0.
        wf.comment_added(
            CommentAddedSignal(
                comment_text="here's the answer", actor_account_id="user-1"
            )
        )
        assert wf._iteration_state.needs_info_streak == 0
        assert wf._out_of_scope is False


# ---------------------------------------------------------------------------
# 5. iter==3 banner - fires exactly once
# ---------------------------------------------------------------------------


class TestIterWarningBanner:
    """iter==3 banner edge fires exactly once."""

    def test_banner_armed_on_first_crossing(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        # iter=1 already; advance to 2 then 3.
        wf.comment_added(CommentAddedSignal(comment_text="more"))
        assert wf._iteration_state.iter_count == 2
        assert wf._iter_warning_pending is False

        wf.comment_added(CommentAddedSignal(comment_text="more again"))
        assert wf._iteration_state.iter_count == ITER_WARNING_THRESHOLD
        assert wf._iter_warning_pending is True
        assert wf._iter_warning_at_three is False  # not yet posted

    def test_banner_not_re_armed_after_post(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        # Walk to iter=3.
        wf.comment_added(CommentAddedSignal(comment_text="x"))
        wf.comment_added(CommentAddedSignal(comment_text="y"))
        assert wf._iter_warning_pending is True

        # Simulate the body draining the edge + posting the banner.
        wf._iter_warning_pending = False
        wf._iter_warning_at_three = True

        # A fourth advance must not re-arm the edge.
        wf.comment_added(CommentAddedSignal(comment_text="z"))
        assert wf._iteration_state.iter_count == 4
        assert wf._iter_warning_pending is False
        assert wf._iter_warning_at_three is True

    def test_banner_not_armed_below_threshold(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf.comment_added(CommentAddedSignal(comment_text="x"))  # iter=2
        assert wf._iter_warning_pending is False
        assert wf._iter_warning_at_three is False


class TestMaybePostIterWarningBanner:
    """The body-side banner posting helper is idempotent."""

    def test_posts_banner_once_then_flag_blocks_subsequent(
        self,
        make_wf,
        patched_workflow_now,
        workflow_input: AgentRunnerWorkflowInput,
    ) -> None:
        wf = make_wf()
        wf._iteration_state = replace(
            wf._iteration_state, iter_count=ITER_WARNING_THRESHOLD
        )
        wf._iter_warning_pending = True

        execute_activity = AsyncMock(return_value=None)
        with patch.object(
            _temporal_workflow,
            "execute_activity",
            execute_activity,
        ), patch.object(
            _temporal_workflow,
            "info",
            lambda: type("WfInfo", (), {"workflow_id": "wf-1"})(),
        ):
            asyncio.run(wf._maybe_post_iter_warning_banner(workflow_input))

        # First pass: banner posted, audit emitted.
        assert wf._iter_warning_at_three is True
        assert wf._iter_warning_pending is False
        # Activity calls: ``jira_add_comment`` + ``audit_emit``.
        called_names = [c.args[0] for c in execute_activity.call_args_list]
        assert "jira_add_comment" in called_names
        assert "audit_emit" in called_names

        # Second pass with the flag set must not re-fire.
        execute_activity.reset_mock()
        wf._iter_warning_pending = True  # an erroneous re-arm
        with patch.object(
            _temporal_workflow,
            "execute_activity",
            execute_activity,
        ), patch.object(
            _temporal_workflow,
            "info",
            lambda: type("WfInfo", (), {"workflow_id": "wf-1"})(),
        ):
            asyncio.run(wf._maybe_post_iter_warning_banner(workflow_input))

        assert execute_activity.call_count == 0
        assert wf._iter_warning_pending is False  # cleared defensively


# ---------------------------------------------------------------------------
# 6. Token cap (T13) - non-retryable + workflow helper pre-flight
# ---------------------------------------------------------------------------


class TestTokenCapExceededError:
    """:class:`TokenCapExceededError` is non-retryable with the stable
    type discriminator.
    """

    def test_is_application_error_subclass(self) -> None:
        err = TokenCapExceededError(
            activity_name="llm_analyze_task",
            input_tokens=9000,
        )
        assert isinstance(err, ApplicationError)

    def test_non_retryable_flag_set(self) -> None:
        err = TokenCapExceededError(
            activity_name="llm_analyze_task",
            input_tokens=MAX_ACTIVITY_TOKEN_CAP + 1,
        )
        assert err.non_retryable is True
        assert err.type == TOKEN_CAP_ERROR_TYPE

    def test_carries_diagnostic_attributes(self) -> None:
        err = TokenCapExceededError(
            activity_name="llm_review_code",
            input_tokens=12_345,
            cap=8000,
        )
        assert err.activity_name == "llm_review_code"
        assert err.input_tokens == 12_345
        assert err.cap == 8000

    def test_llm_retry_policy_fail_fast(self) -> None:
        """LLM activities use ``maximum_attempts=1`` for fail-fast behavior."""

        assert LLM_RETRY_POLICY.maximum_attempts == 1


class TestExecuteLlmActivityTokenCap:
    """:meth:`AgentRunnerWorkflow._execute_llm_activity` enforces token-cap
    pre-flight."""

    def test_over_cap_raises_without_calling_activity(
        self,
        make_wf,
        patched_workflow_now,
        workflow_input: AgentRunnerWorkflowInput,
    ) -> None:
        wf = make_wf()
        execute_activity = AsyncMock(return_value="never reached")

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow,
                "execute_activity",
                execute_activity,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "wf-1"})(),
            ):
                return await wf._execute_llm_activity(
                    "llm_analyze_task",
                    args=["payload"],
                    input_tokens=MAX_ACTIVITY_TOKEN_CAP + 1,
                    inp=workflow_input,
                )

        with pytest.raises(TokenCapExceededError) as excinfo:
            asyncio.run(_drive())

        assert excinfo.value.activity_name == "llm_analyze_task"
        assert excinfo.value.input_tokens == MAX_ACTIVITY_TOKEN_CAP + 1
        assert excinfo.value.non_retryable is True
        # The activity itself was NEVER invoked - cap is pre-flight.
        called_names = [c.args[0] for c in execute_activity.call_args_list]
        assert "llm_analyze_task" not in called_names
        # The audit row was emitted before raising so the trail
        # records the refusal.
        assert "audit_emit" in called_names

    def test_under_cap_calls_activity_with_single_attempt_retry(
        self,
        make_wf,
        patched_workflow_now,
        workflow_input: AgentRunnerWorkflowInput,
    ) -> None:
        wf = make_wf()
        execute_activity = AsyncMock(return_value="ok")

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow,
                "execute_activity",
                execute_activity,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "wf-1"})(),
            ):
                return await wf._execute_llm_activity(
                    "llm_analyze_task",
                    args=["payload"],
                    input_tokens=4_000,
                    inp=workflow_input,
                )

        result = asyncio.run(_drive())
        assert result == "ok"

        # Exactly one activity call, with the fail-fast retry policy.
        call = execute_activity.call_args
        assert call.args[0] == "llm_analyze_task"
        assert call.kwargs["retry_policy"] is LLM_RETRY_POLICY
        assert call.kwargs["retry_policy"].maximum_attempts == 1

    def test_pre_flight_audit_action_is_token_cap_exceeded(
        self,
        make_wf,
        patched_workflow_now,
        workflow_input: AgentRunnerWorkflowInput,
    ) -> None:
        wf = make_wf()
        execute_activity = AsyncMock()

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow,
                "execute_activity",
                execute_activity,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "wf-1"})(),
            ):
                with pytest.raises(TokenCapExceededError):
                    await wf._execute_llm_activity(
                        "llm_analyze_task",
                        args=[],
                        input_tokens=MAX_ACTIVITY_TOKEN_CAP + 100,
                        inp=workflow_input,
                    )

        asyncio.run(_drive())

        audit_calls = [
            c
            for c in execute_activity.call_args_list
            if c.args[0] == "audit_emit"
        ]
        assert len(audit_calls) == 1
        emitted_payload = audit_calls[0].kwargs["args"][0]
        assert emitted_payload["action"] == TOKEN_CAP_AUDIT_ACTION
