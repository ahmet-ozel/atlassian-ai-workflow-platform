"""Unit tests for :mod:`src.sandbox` (task 6.2 — Requirement 2.4).

The tests focus on the deterministic invariants of the
:class:`PromptSandbox`:

* ``cost_tag`` is always ``"sandbox"`` on both the LLM call and the
  recorded cost entry.
* The :class:`SandboxResult` is a faithful projection of the LLM
  response and carries the invocation timestamp from the injected
  clock.
* The cost write happens **after** the LLM call (so a cost-write
  failure never masks a successful response).
* The class never retries on LLM failure — sandbox tests should
  surface errors immediately to the developer.
* :class:`SyntheticLlmInvoker` and :class:`NullCostTracker` satisfy the
  declared protocols and integrate cleanly with the sandbox.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# Bootstrap sys.path so the tests can be run via ``pytest`` directly
# from the service root without requiring ``pip install -e``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


from src.sandbox import (  # noqa: E402
    COST_TAG_SANDBOX,
    CostEntryLike,
    CostTrackerLike,
    LlmInvocationResult,
    LlmInvokerLike,
    NullCostTracker,
    PromptSandbox,
    SandboxResult,
    SyntheticLlmInvoker,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingLlm:
    """Capture every ``invoke`` call; return a scripted result."""

    def __init__(self, result: LlmInvocationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._result = result or LlmInvocationResult(
            response_text="ok",
            token_in=12,
            token_out=34,
            cost_usd=Decimal("0.001234"),
            model="test-model",
            provider="test-provider",
        )
        self._raise: Exception | None = None

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def invoke(
        self,
        *,
        system: str,
        user: str,
        cost_tag: str,
    ) -> LlmInvocationResult:
        self.calls.append(
            {"system": system, "user": user, "cost_tag": cost_tag}
        )
        if self._raise is not None:
            raise self._raise
        return self._result


class _RecordingCostTracker:
    """Capture every ``record`` call; optionally raise."""

    def __init__(self) -> None:
        self.records: list[CostEntryLike] = []
        self._raise: Exception | None = None

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def record(self, entry: CostEntryLike) -> None:
        self.records.append(entry)
        if self._raise is not None:
            raise self._raise


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_cost_tag_sandbox_is_pinned(self) -> None:
        # The literal must match the CHECK constraint on
        # ``shared.cost_tracking.cost_tag`` and the exclusion clause
        # in BudgetCapPolicy. Drift here breaks Requirement 5.5.
        assert COST_TAG_SANDBOX == "sandbox"

    def test_protocols_are_runtime_checkable(self) -> None:
        # ``isinstance(obj, Protocol)`` must work so dependency
        # injection can introspect collaborator shapes.
        assert isinstance(_RecordingLlm(), LlmInvokerLike)
        assert isinstance(_RecordingCostTracker(), CostTrackerLike)
        assert isinstance(SyntheticLlmInvoker(), LlmInvokerLike)
        assert isinstance(NullCostTracker(), CostTrackerLike)


# ---------------------------------------------------------------------------
# PromptSandbox.run — happy path
# ---------------------------------------------------------------------------


class TestRunHappyPath:
    @pytest.mark.asyncio
    async def test_returns_sandbox_result_with_cost_tag(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()

        fixed_time = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
        ids: list[str] = []

        def _id() -> str:
            ids.append(f"sandbox-test-{len(ids)}")
            return ids[-1]

        sandbox = PromptSandbox(
            llm=llm,
            cost_tracker=tracker,
            activity_id_factory=_id,
            clock=lambda: fixed_time,
        )

        result = await sandbox.run(
            "system body {department_id}",
            "Sample user input",
            dept_id="payment",
            user_id="alice",
        )

        # The SandboxResult is a faithful projection of the LLM
        # response with the sandbox-stamped invoked_at + cost_tag.
        assert isinstance(result, SandboxResult)
        assert result.response_text == "ok"
        assert result.token_in == 12
        assert result.token_out == 34
        assert result.cost_usd == Decimal("0.001234")
        assert result.invoked_at == fixed_time
        assert result.model == "test-model"
        assert result.provider == "test-provider"
        assert result.cost_tag == "sandbox"

    @pytest.mark.asyncio
    async def test_llm_called_with_cost_tag_sandbox(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)

        await sandbox.run("system body", "user input")

        assert len(llm.calls) == 1
        assert llm.calls[0]["system"] == "system body"
        assert llm.calls[0]["user"] == "user input"
        # The cost_tag forwarded to the LLM is the only signal a
        # production provider gets that this is a sandbox call.
        assert llm.calls[0]["cost_tag"] == "sandbox"

    @pytest.mark.asyncio
    async def test_cost_recorded_with_sandbox_tag(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(
            llm=llm,
            cost_tracker=tracker,
            activity_id_factory=lambda: "fixed-activity-id",
        )

        await sandbox.run(
            "body", "user", dept_id="marketing", user_id="bob"
        )

        assert len(tracker.records) == 1
        entry = tracker.records[0]
        assert entry.activity_id == "fixed-activity-id"
        # The sandbox-tag is the budget-isolation contract: every
        # row must carry it so BudgetCapPolicy excludes the spend.
        assert entry.cost_tag == "sandbox"
        assert entry.dept_id == "marketing"
        assert entry.user_id == "bob"
        assert entry.workflow_id is None
        assert entry.token_in == 12
        assert entry.token_out == 34
        assert entry.cost_usd == Decimal("0.001234")
        assert entry.model == "test-model"
        assert entry.provider == "test-provider"

    @pytest.mark.asyncio
    async def test_dept_id_and_user_id_are_optional(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)

        # Cross-dept prompts (eg. assistant_chat.md) are global.
        await sandbox.run("body", "user")

        assert tracker.records[0].dept_id is None
        assert tracker.records[0].user_id is None
        assert tracker.records[0].cost_tag == "sandbox"


# ---------------------------------------------------------------------------
# PromptSandbox.run — error / fail-soft behaviour
# ---------------------------------------------------------------------------


class TestRunErrorHandling:
    @pytest.mark.asyncio
    async def test_llm_failure_propagates_without_recording(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)
        llm.set_error(RuntimeError("LLM down"))

        with pytest.raises(RuntimeError, match="LLM down"):
            await sandbox.run("body", "user")

        # Cost is NOT recorded when the LLM fails — there is no
        # actual spend to track.
        assert tracker.records == []

    @pytest.mark.asyncio
    async def test_cost_record_failure_does_not_mask_response(self) -> None:
        llm = _RecordingLlm()
        tracker = _RecordingCostTracker()
        tracker.set_error(RuntimeError("Postgres down"))
        sandbox = PromptSandbox(llm=llm, cost_tracker=tracker)

        # Even when cost recording fails the developer still sees the
        # LLM response — the sandbox is interactive, the cost write
        # is best-effort fail-soft.
        result = await sandbox.run("body", "user")

        assert result.response_text == "ok"
        assert result.cost_tag == "sandbox"
        # The tracker did receive the call (we just made it raise).
        assert len(tracker.records) == 1


# ---------------------------------------------------------------------------
# PromptSandbox — call ordering invariants
# ---------------------------------------------------------------------------


class TestCallOrdering:
    @pytest.mark.asyncio
    async def test_llm_called_before_cost_record(self) -> None:
        events: list[str] = []

        class _OrderedLlm:
            async def invoke(
                self, *, system: str, user: str, cost_tag: str
            ) -> LlmInvocationResult:
                events.append("llm")
                return LlmInvocationResult(
                    response_text="r",
                    token_in=1,
                    token_out=1,
                    cost_usd=Decimal("0.0"),
                )

        class _OrderedTracker:
            async def record(self, entry: CostEntryLike) -> None:
                events.append("cost")

        sandbox = PromptSandbox(
            llm=_OrderedLlm(), cost_tracker=_OrderedTracker()
        )
        await sandbox.run("body", "user")

        # Cost must follow the LLM round-trip; recording before the
        # provider returns would mean we record speculative cost
        # that may never have happened.
        assert events == ["llm", "cost"]


# ---------------------------------------------------------------------------
# SyntheticLlmInvoker
# ---------------------------------------------------------------------------


class TestSyntheticLlmInvoker:
    @pytest.mark.asyncio
    async def test_returns_deterministic_preview(self) -> None:
        invoker = SyntheticLlmInvoker()

        result = await invoker.invoke(
            system="system",
            user="hello world",
            cost_tag="sandbox",
        )

        assert result.response_text.startswith("[synthetic] ")
        assert "hello world" in result.response_text
        assert result.token_in > 0
        assert result.token_out > 0
        assert result.cost_usd == Decimal("0.000000")

    @pytest.mark.asyncio
    async def test_cost_per_1k_tokens_applied(self) -> None:
        invoker = SyntheticLlmInvoker(cost_per_1k_tokens=Decimal("1.0"))

        result = await invoker.invoke(
            system="a b c d e",
            user="f g h",
            cost_tag="sandbox",
        )

        # token_in = 5 + 3 = 8, token_out = roughly the preview text; cost
        # = 1.0 * (10 / 1000) = 0.010000. We assert the value is
        # positive and rounded to six decimals rather than the exact
        # number, because the response token count depends on the
        # synthetic preview formatting.
        assert result.cost_usd > Decimal("0")
        assert result.cost_usd == result.cost_usd.quantize(
            Decimal("0.000001")
        )


# ---------------------------------------------------------------------------
# NullCostTracker — scaffold default
# ---------------------------------------------------------------------------


class TestNullCostTracker:
    @pytest.mark.asyncio
    async def test_records_appended_to_inspectable_list(self) -> None:
        tracker = NullCostTracker()
        entry = CostEntryLike(
            activity_id="a",
            dept_id="d",
            user_id="u",
            workflow_id=None,
            model="m",
            provider="p",
            token_in=1,
            token_out=2,
            cost_usd=Decimal("0.5"),
            cost_tag="sandbox",
        )

        await tracker.record(entry)

        assert tracker.records == [entry]


# ---------------------------------------------------------------------------
# SandboxResult — frozen dataclass invariants
# ---------------------------------------------------------------------------


class TestSandboxResult:
    def test_is_frozen(self) -> None:
        result = SandboxResult(
            response_text="x",
            token_in=1,
            token_out=2,
            cost_usd=Decimal("0.1"),
            invoked_at=datetime.now(tz=timezone.utc),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            result.response_text = "y"  # type: ignore[misc]

    def test_default_cost_tag_is_sandbox(self) -> None:
        result = SandboxResult(
            response_text="x",
            token_in=1,
            token_out=2,
            cost_usd=Decimal("0.1"),
            invoked_at=datetime.now(tz=timezone.utc),
        )
        # Default protects against accidental construction without
        # the tag — every SandboxResult on the wire must carry the
        # isolation contract.
        assert result.cost_tag == "sandbox"
