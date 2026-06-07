"""Unit tests for ``AgentRunnerWorkflow`` cancel signal handler.

Covers the cancel + compensation contract delivered by
The cancel signal path covers:

    1. ``cancel_requested`` signal triggers the
       ``compensation_chain_run`` activity exactly once.
    2. ``MAX_ITER`` exhaustion does NOT trigger the compensation
       chain (natural termination - only the "yeni task aç" comment
       is posted via the existing helper).
    3. ``out_of_scope`` natural termination (``needs_info_streak`` cap)
       does NOT trigger the compensation chain.
    4. A second ``cancel_requested`` signal that arrives while the
       chain is already running is a silent no-op (no double-fire).
    5. Audit role mapping - ``end_user``
       ``workflow_cancelled_by_end_user``, ``admin`` / ``dept_admin``
        ``workflow_cancelled_by_admin``; unknown roles default to
       end_user.

The tests drive the body methods directly without spinning up a
Temporal worker. Activity calls are intercepted by patching
``temporalio.workflow.execute_activity`` so we can assert on the
exact dispatch sequence.

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

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors ``test_agent_runner_signal_handlers.py``.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (_SRC_DIR, _TEMPORAL_SHARED_SRC, _MCP_CLIENT_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

# noqa: E402 below - import after sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    CANCEL_BY_ADMIN_AUDIT_ACTION,
    CANCEL_BY_END_USER_AUDIT_ACTION,
    MAX_ITER,
    NEEDS_INFO_MAX_STREAK,
    AgentRunnerWorkflow,
    CancelRequestedSignal,
    CommentAddedSignal,
    _audit_action_for_cancel_role,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    """Deterministic anchor for ``workflow.now`` stubs."""

    return datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def patched_workflow_now(fixed_now: datetime, monkeypatch: pytest.MonkeyPatch):
    """Replace ``workflow.now`` with a deterministic clock."""

    state = {"now": fixed_now}
    monkeypatch.setattr(_temporal_workflow, "now", lambda: state["now"])
    return state


def _make_input(
    *,
    workflow_type: str = "code_change_with_test",
    iteration: int = 1,
    max_iter: int = MAX_ITER,
) -> AgentRunnerWorkflowInput:
    """Build a minimal :class:`AgentRunnerWorkflowInput` fixture."""

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4211",
        title="Fix payment retry",
        rationale="LLM-resolved",
        token_usage=120,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-4211",
        issue_key="PAY-4211",
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4211",
        iteration=iteration,
        max_iter=max_iter,
        default_language="tr",
    )


@pytest.fixture
def make_wf():
    """Factory returning a fresh :class:`AgentRunnerWorkflow`.

    Seeds ``iter_count=1`` so signal-handler tests operate on a
    non-zero iteration counter (the body's first call to
    :func:`_should_advance_iter` is implicitly performed by ``run``).
    """

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _activity_dispatcher(routes: dict[str, Any]) -> AsyncMock:
    """Return an ``AsyncMock`` that resolves ``execute_activity`` calls."""

    async def _fake_execute_activity(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if name in routes:
            value = routes[name]
            if callable(value):
                return value(*args, **kwargs)
            return value
        return None

    return AsyncMock(side_effect=_fake_execute_activity)


def _patch_workflow_runtime(activity_mock: AsyncMock):
    """Patch ``workflow.execute_activity`` and ``workflow.info``."""

    info_stub = type(
        "WfInfo", (), {"workflow_id": "automation-jira-PAY-4211"}
    )()
    return [
        patch.object(_temporal_workflow, "execute_activity", activity_mock),
        patch.object(_temporal_workflow, "info", lambda: info_stub),
    ]


# ---------------------------------------------------------------------------
# 1. Pure helper - role  audit action mapping
# ---------------------------------------------------------------------------


class TestAuditActionForCancelRole:
    """``_audit_action_for_cancel_role`` is a pure mapping."""

    def test_end_user_role_maps_to_end_user_audit(self) -> None:
        assert (
            _audit_action_for_cancel_role("end_user")
            == CANCEL_BY_END_USER_AUDIT_ACTION
        )

    def test_admin_role_maps_to_admin_audit(self) -> None:
        assert (
            _audit_action_for_cancel_role("admin")
            == CANCEL_BY_ADMIN_AUDIT_ACTION
        )

    def test_dept_admin_role_maps_to_admin_audit(self) -> None:
        assert (
            _audit_action_for_cancel_role("dept_admin")
            == CANCEL_BY_ADMIN_AUDIT_ACTION
        )

    @pytest.mark.parametrize(
        "role", ["", "viewer", "lead", "system", "superuser", None]
    )
    def test_unknown_or_blank_role_defaults_to_end_user(
        self, role: str | None
    ) -> None:
        assert (
            _audit_action_for_cancel_role(role)
            == CANCEL_BY_END_USER_AUDIT_ACTION
        )


# ---------------------------------------------------------------------------
# 2. Cancel signal latches state and is idempotent
# ---------------------------------------------------------------------------


class TestCancelSignalLatch:
    """``cancel_requested`` latches the first request."""

    def test_first_cancel_records_actor_role_and_reason(
        self, make_wf
    ) -> None:
        wf = make_wf()

        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user pressed cancel",
            )
        )

        assert wf._cancel_requested is True
        assert wf._cancel_actor_id == "alice"
        assert wf._cancel_actor_role == "end_user"
        assert wf._cancel_reason == "user pressed cancel"
        assert wf._signal_pending is True
        # Compensation hasn't been dispatched yet - only the body
        # observing ``_cancel_requested`` runs the chain.
        assert wf._compensation_running is False

    def test_second_cancel_signal_is_noop(self, make_wf) -> None:
        wf = make_wf()

        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="first",
            )
        )
        # Drain the signal_pending flag so the no-op assertion below
        # is meaningful - a second signal must not flip it back on.
        wf._signal_pending = False

        # Second cancel attempt with different actor.
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="bob",
                actor_role="admin",
                reason="second",
            )
        )

        # First cancel wins: actor_id / actor_role / reason unchanged.
        assert wf._cancel_actor_id == "alice"
        assert wf._cancel_actor_role == "end_user"
        assert wf._cancel_reason == "first"
        # The handler must not flip the signal pending edge a second
        # time - the body has nothing new to drain.
        assert wf._signal_pending is False

    def test_cancel_during_compensation_is_noop(self, make_wf) -> None:
        """A cancel arriving *while* compensation runs is a no-op."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="first",
            )
        )
        # Simulate the body having entered ``_handle_cancel``.
        wf._compensation_running = True
        wf._signal_pending = False

        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="bob",
                actor_role="admin",
                reason="duplicate",
            )
        )

        assert wf._cancel_actor_id == "alice"
        assert wf._cancel_actor_role == "end_user"
        assert wf._signal_pending is False

    def test_dict_payload_is_normalised(self, make_wf) -> None:
        """Raw dict payloads (Temporal data converter fallback) are accepted."""

        wf = make_wf()
        wf.cancel_requested(
            {
                "actor_id": "carol",
                "actor_role": "dept_admin",
                "reason": "admin override",
            }
        )

        assert wf._cancel_actor_id == "carol"
        assert wf._cancel_actor_role == "dept_admin"
        assert wf._cancel_reason == "admin override"

    def test_unknown_role_defaults_to_end_user(self, make_wf) -> None:
        """Unknown ``actor_role`` falls back to ``end_user``."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="superuser",
                reason="test",
            )
        )

        assert wf._cancel_actor_role == "end_user"


# ---------------------------------------------------------------------------
# 3. ``_handle_cancel`` runs compensation chain + audits with role mapping
# ---------------------------------------------------------------------------


class TestHandleCancelCompensation:
    """``_handle_cancel`` invokes ``compensation_chain_run`` exactly once."""

    def test_cancel_triggers_compensation_chain_run(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        inp = _make_input()
        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "automation-jira-PAY-4211"},
                )(),
            ):
                return await wf._handle_cancel(inp)

        output = asyncio.run(_drive())

        # Compensation chain was dispatched exactly once.
        comp_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        ]
        assert len(comp_calls) == 1

        # Idempotency latch is set.
        assert wf._compensation_running is True

        # Workflow output reports ``cancelled``.
        assert output.status == "cancelled"
        assert "iptal edildi" in output.summary

    def test_cancel_passes_actor_role_to_chain_args(
        self, make_wf, patched_workflow_now
    ) -> None:
        """The compensation chain receives the actor_role for downstream use."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="dave",
                actor_role="dept_admin",
                reason="admin override",
            )
        )

        inp = _make_input()
        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                await wf._handle_cancel(inp)

        asyncio.run(_drive())

        comp_call = next(
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        )
        # The activity is invoked with ``args=[{...}]``.
        chain_args = comp_call.kwargs["args"][0]
        assert chain_args["actor_id"] == "dave"
        assert chain_args["actor_role"] == "dept_admin"
        assert chain_args["reason"] == "admin override"
        assert chain_args["dept_id"] == "payments"
        assert chain_args["issue_key"] == "PAY-4211"

    def test_cancel_uses_short_timeout_for_chain(
        self, make_wf, patched_workflow_now
    ) -> None:
        """The chain dispatch uses a 2-minute / 120s start_to_close timeout."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        inp = _make_input()
        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                await wf._handle_cancel(inp)

        asyncio.run(_drive())

        comp_call = next(
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        )
        timeout = comp_call.kwargs.get("start_to_close_timeout")
        # 2 minutes / 120s - covers the full six-step chain.
        assert timeout == timedelta(minutes=2)

    def test_end_user_cancel_emits_end_user_audit(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        inp = _make_input()
        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                await wf._handle_cancel(inp)

        asyncio.run(_drive())

        audit_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "audit_emit"
        ]
        assert len(audit_calls) == 1
        action = audit_calls[0].kwargs["args"][0]["action"]
        assert action == CANCEL_BY_END_USER_AUDIT_ACTION

    @pytest.mark.parametrize("role", ["admin", "dept_admin"])
    def test_admin_cancel_emits_admin_audit(
        self, make_wf, patched_workflow_now, role: str
    ) -> None:
        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="carol",
                actor_role=role,
                reason="admin_cancel",
            )
        )

        inp = _make_input()
        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                await wf._handle_cancel(inp)

        asyncio.run(_drive())

        audit_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "audit_emit"
        ]
        assert len(audit_calls) == 1
        action = audit_calls[0].kwargs["args"][0]["action"]
        assert action == CANCEL_BY_ADMIN_AUDIT_ACTION

    def test_compensation_chain_failure_still_terminates_cancelled(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Best-effort: a chain activity failure does not prevent termination."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        inp = _make_input()

        async def _raise_chain_failure(*args, **kwargs):
            name = args[0] if args else kwargs.get("activity")
            if name == "compensation_chain_run":
                raise RuntimeError("chain unreachable")
            return None

        activity_mock = AsyncMock(side_effect=_raise_chain_failure)

        # ``workflow.logger`` is sandbox-aware and requires a workflow
        # event loop, which the unit test does not have. Patch it
        # with a plain logger so the best-effort warning emits without
        # raising ``_NotInWorkflowEventLoopError``.
        import logging

        plain_logger = logging.getLogger("test.agent_runner_cancel")

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ), patch.object(
                _temporal_workflow, "logger", plain_logger
            ):
                return await wf._handle_cancel(inp)

        output = asyncio.run(_drive())
        # Workflow still terminates with ``cancelled`` even though the
        # chain raised - the spec contract is "compensation is
        # best-effort, the workflow status still flips to cancelled".
        assert output.status == "cancelled"


# ---------------------------------------------------------------------------
# 4. MAX_ITER does NOT trigger compensation
# ---------------------------------------------------------------------------


class TestMaxIterNoCompensation:
    """MAX_ITER natural termination must not run compensation."""

    def test_max_iter_signal_marks_out_of_scope_without_cancel(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Iter-cap exhaustion flips ``_out_of_scope`` but not ``_cancel_requested``."""

        wf = make_wf()
        # Drive the iter counter to MAX_ITER so the next advance trips
        # the cap.
        wf._iteration_state = replace(
            wf._iteration_state, iter_count=MAX_ITER
        )

        wf.comment_added(
            CommentAddedSignal(
                comment_text="another comment after the cap",
                actor_account_id="user-1",
            )
        )

        assert wf._out_of_scope is True
        # Cancel state untouched - natural termination is NOT cancel.
        assert wf._cancel_requested is False
        assert wf._compensation_running is False

    def test_max_iter_run_does_not_dispatch_compensation_chain(
        self, make_wf, patched_workflow_now
    ) -> None:
        """``run`` with iter already at cap returns ``out_of_scope`` and skips compensation."""

        wf = AgentRunnerWorkflow()
        # Pre-seed the iter counter past the cap so the initial
        # ``_should_advance_iter`` check refuses to start.
        wf._iteration_state = replace(
            wf._iteration_state, iter_count=MAX_ITER
        )

        # Drive ``run`` with an input whose iteration is already past
        # the cap. The body should short-circuit to ``out_of_scope``
        # without ever calling ``compensation_chain_run``.
        inp = _make_input(iteration=MAX_ITER + 1)

        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                return await wf.run(inp)

        output = asyncio.run(_drive())

        assert output.status == "out_of_scope"
        # Crucial invariant - compensation MUST NOT have been called.
        comp_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        ]
        assert comp_calls == []
        # Nor any of the cancel-audit actions.
        audit_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args
            and c.args[0] == "audit_emit"
            and c.kwargs["args"][0]["action"]
            in {
                CANCEL_BY_END_USER_AUDIT_ACTION,
                CANCEL_BY_ADMIN_AUDIT_ACTION,
            }
        ]
        assert audit_calls == []


# ---------------------------------------------------------------------------
# 5. ``out_of_scope`` (needs_info streak) does NOT trigger compensation
# ---------------------------------------------------------------------------


class TestOutOfScopeNoCompensation:
    """``needs_info`` streak cap natural termination skips compensation."""

    def test_needs_info_streak_marks_out_of_scope_without_cancel(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()

        # Fire ``[needs_info]`` until the streak cap is reached.
        for _ in range(NEEDS_INFO_MAX_STREAK):
            wf.comment_added(
                CommentAddedSignal(
                    comment_text="[needs_info] hangi servis?",
                    actor_account_id="user-1",
                )
            )

        assert wf._out_of_scope is True
        assert wf._failure_reason == "needs_info_loop_cap"
        # Cancel state untouched.
        assert wf._cancel_requested is False
        assert wf._compensation_running is False


# ---------------------------------------------------------------------------
# 6. Idempotency at the ``_handle_cancel`` layer
# ---------------------------------------------------------------------------


class TestCancelIdempotencyAtBody:
    """A second cancel during chain execution must not double-fire."""

    def test_chain_runs_once_even_with_second_cancel_during_dispatch(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Simulate a cancel signal arriving while ``_handle_cancel`` is running."""

        wf = make_wf()
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        inp = _make_input()

        # The chain "activity" injects a second cancel signal mid-flight
        # to simulate a rapid double-tap from the cancel API. The
        # signal handler MUST observe ``_compensation_running`` and
        # short-circuit.
        async def _chain_activity_with_double_cancel(*args, **kwargs):
            name = args[0] if args else kwargs.get("activity")
            if name == "compensation_chain_run":
                # A second cancel arrives while we're running.
                wf.cancel_requested(
                    CancelRequestedSignal(
                        actor_id="bob",
                        actor_role="admin",
                        reason="duplicate",
                    )
                )
                # First-cancel state must still be intact.
                assert wf._cancel_actor_id == "alice"
                assert wf._cancel_actor_role == "end_user"
            return None

        activity_mock = AsyncMock(side_effect=_chain_activity_with_double_cancel)

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ):
                return await wf._handle_cancel(inp)

        output = asyncio.run(_drive())
        assert output.status == "cancelled"

        # Compensation chain still invoked exactly once despite the
        # second cancel signal mid-flight.
        comp_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        ]
        assert len(comp_calls) == 1


# ---------------------------------------------------------------------------
# 7. Smoke test on the full ``run`` path
# ---------------------------------------------------------------------------


class TestCancelDuringRun:
    """End-to-end ``run`` with a pre-seeded cancel signal."""

    def test_run_with_pre_seeded_cancel_dispatches_compensation(
        self, patched_workflow_now
    ) -> None:
        """A cancel signal received before ``run`` triggers the chain."""

        wf = AgentRunnerWorkflow()
        # Pre-seed the cancel state as if a signal had been delivered
        # before the body started executing.
        wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="alice",
                actor_role="end_user",
                reason="user_cancel",
            )
        )

        # Use ``noop_test`` so the body falls through to the legacy
        # signal-wait loop - which will see ``_cancel_requested`` and
        # raise ``_CancelledViaSignal`` immediately, routing into
        # ``_handle_cancel``.
        inp = _make_input(workflow_type="noop_test")

        activity_mock = _activity_dispatcher(
            {"compensation_chain_run": None, "audit_emit": None}
        )

        async def _drive() -> Any:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo", (), {"workflow_id": "x"}
                )(),
            ), patch.object(
                _temporal_workflow,
                "wait_condition",
                AsyncMock(return_value=None),
            ):
                return await wf.run(inp)

        output = asyncio.run(_drive())

        assert output.status == "cancelled"
        comp_calls = [
            c
            for c in activity_mock.call_args_list
            if c.args and c.args[0] == "compensation_chain_run"
        ]
        assert len(comp_calls) == 1
        # End-user role audit emitted.
        end_user_audits = [
            c
            for c in activity_mock.call_args_list
            if c.args
            and c.args[0] == "audit_emit"
            and c.kwargs["args"][0]["action"]
            == CANCEL_BY_END_USER_AUDIT_ACTION
        ]
        assert len(end_user_audits) == 1
