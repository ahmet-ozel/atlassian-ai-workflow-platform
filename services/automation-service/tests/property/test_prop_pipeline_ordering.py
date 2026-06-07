"""Property test: Webhook pipeline ordering guarantee.

Stages always execute in dedup  loop_guard  dispatcher order.

For ANY webhook payload (generated via Hypothesis strategies), the pipeline
stages ALWAYS execute in strict order:
    Event_Dedup  Loop_Guard  Webhook_Dispatcher

No stage can be skipped or reordered. This is verified by injecting fake
stages that record their invocation order and asserting the invariant
holds across all generated inputs.

The ``WebhookPipeline`` class under test lives in
``services/automation-service/src/webhooks/pipeline.py``. It accepts a
list of ``PipelineStage`` protocol-compatible objects and calls their
``check()`` method sequentially.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors sibling property tests
# ---------------------------------------------------------------------------

_AUTOMATION_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from webhooks.pipeline import (  # noqa: E402
    PipelineStage,
    StageAction,
    StageResult,
    WebhookPayload,
    WebhookPipeline,
)

# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=200,
    deadline=5000,
    suppress_health_check=[HealthCheck.too_slow],
)

# ---------------------------------------------------------------------------
# Fake pipeline stages that record execution order
# ---------------------------------------------------------------------------


class OrderRecordingStage:
    """A fake pipeline stage that records when it was called.

    Each instance appends its name to a shared execution log list
    when ``check()`` is invoked, allowing the test to verify ordering.

    Parameters
    ----------
    stage_name:
        The name of this stage (e.g. "event_dedup").
    execution_log:
        A shared list that all stages append to.
    action:
        The StageAction to return (default PASS to continue pipeline).
    """

    def __init__(
        self,
        stage_name: str,
        execution_log: list[str],
        action: StageAction = StageAction.PASS,
    ) -> None:
        self._name = stage_name
        self._execution_log = execution_log
        self._action = action

    @property
    def name(self) -> str:
        return self._name

    async def check(self, payload: WebhookPayload) -> StageResult:
        self._execution_log.append(self._name)
        return StageResult(action=self._action)


# ---------------------------------------------------------------------------
# Hypothesis strategies for WebhookPayload generation
# ---------------------------------------------------------------------------

#: Jira-style event types
_event_types: st.SearchStrategy[str] = st.sampled_from([
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_assigned",
    "jira:comment_created",
    "pullrequest:reviewer_added",
    "pullrequest:comment_created",
])

#: Jira-style issue keys (e.g. PROJ-123)
_issue_keys: st.SearchStrategy[str] = st.from_regex(
    r"[A-Z]{2,6}-[1-9][0-9]{0,4}", fullmatch=True
)

#: Atlassian-style account IDs
_account_ids: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=40,
)

#: Optional account IDs (may be None)
_optional_account_ids: st.SearchStrategy[str | None] = st.one_of(
    st.none(), _account_ids
)

#: Optional comment bodies
_optional_comments: st.SearchStrategy[str | None] = st.one_of(
    st.none(),
    st.text(min_size=1, max_size=200),
)

#: Optional trace IDs
_optional_trace_ids: st.SearchStrategy[str | None] = st.one_of(
    st.none(),
    st.uuids().map(str),
)

#: Raw payload dicts (simplified but representative)
_raw_payloads: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries({
    "webhookEvent": _event_types,
    "timestamp": st.integers(min_value=1_600_000_000_000, max_value=2_000_000_000_000),
    "issue": st.fixed_dictionaries({
        "id": st.integers(min_value=1, max_value=999999).map(str),
        "key": _issue_keys,
    }),
})

#: HTTP headers (simplified)
_headers: st.SearchStrategy[dict[str, str]] = st.fixed_dictionaries({}, optional={
    "x-atlassian-webhook-identifier": st.uuids().map(str),
    "x-trace-id": st.uuids().map(str),
    "content-type": st.just("application/json"),
})


@st.composite
def webhook_payloads(draw: st.DrawFn) -> WebhookPayload:
    """Generate random but valid WebhookPayload instances."""
    event_type = draw(_event_types)
    issue_key = draw(_issue_keys)
    actor_account_id = draw(_optional_account_ids)
    assignee_account_id = draw(_optional_account_ids)
    comment_body = draw(_optional_comments)
    trace_id = draw(_optional_trace_ids)
    raw_payload = draw(_raw_payloads)
    headers = draw(_headers)

    return WebhookPayload(
        event_id=draw(st.one_of(st.none(), st.uuids().map(str))),
        event_type=event_type,
        issue_key=issue_key,
        actor_account_id=actor_account_id,
        assignee_account_id=assignee_account_id,
        comment_body=comment_body,
        trace_id=trace_id,
        raw_payload=raw_payload,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Pipeline ordering guarantee
# ---------------------------------------------------------------------------


class TestPipelineOrderingGuarantee:
    """For any webhook payload, stages execute in strict order:
    Event_Dedup  Loop_Guard  Webhook_Dispatcher.

    No stage can be skipped or reordered.
    """

    @_PROFILE
    @given(payload=webhook_payloads())
    def test_all_stages_execute_in_order_when_all_pass(
        self, payload: WebhookPayload
    ) -> None:
        """When all stages return PASS, all three stages execute in the
        exact order: event_dedup  loop_guard  dispatcher.
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.PASS)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.PASS)
        dispatcher = OrderRecordingStage("dispatcher", execution_log, StageAction.PASS)

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        asyncio.run(pipeline.process(payload))

        assert execution_log == ["event_dedup", "loop_guard", "dispatcher"], (
            f"Expected strict ordering [event_dedup, loop_guard, dispatcher], "
            f"got {execution_log}"
        )

    @_PROFILE
    @given(payload=webhook_payloads())
    def test_dedup_drop_stops_pipeline_after_first_stage(
        self, payload: WebhookPayload
    ) -> None:
        """When dedup drops the event, only dedup executes. Loop_guard
        and dispatcher are never reached - but the ordering of what
        DID execute is still correct (dedup is first).
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.DROP)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.PASS)
        dispatcher = OrderRecordingStage("dispatcher", execution_log, StageAction.PASS)

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        result = asyncio.run(pipeline.process(payload))

        # Only dedup executed
        assert execution_log == ["event_dedup"], (
            f"Expected only [event_dedup] when dedup drops, got {execution_log}"
        )
        # Pipeline reports drop at dedup
        assert result.dropped_at == "event_dedup"
        assert result.final_action == StageAction.DROP

    @_PROFILE
    @given(payload=webhook_payloads())
    def test_loop_guard_drop_stops_after_second_stage(
        self, payload: WebhookPayload
    ) -> None:
        """When loop_guard drops the event, dedup and loop_guard execute
        in order. Dispatcher is never reached.
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.PASS)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.DROP)
        dispatcher = OrderRecordingStage("dispatcher", execution_log, StageAction.PASS)

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        result = asyncio.run(pipeline.process(payload))

        # Dedup then loop_guard executed in order
        assert execution_log == ["event_dedup", "loop_guard"], (
            f"Expected [event_dedup, loop_guard] when loop_guard drops, "
            f"got {execution_log}"
        )
        # Pipeline reports drop at loop_guard
        assert result.dropped_at == "loop_guard"
        assert result.final_action == StageAction.DROP

    @_PROFILE
    @given(payload=webhook_payloads())
    def test_dispatcher_terminal_action_completes_full_pipeline(
        self, payload: WebhookPayload
    ) -> None:
        """When dispatcher returns a terminal action (WORKFLOW_STARTED),
        all three stages execute in strict order before the pipeline
        terminates.
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.PASS)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.PASS)
        dispatcher = OrderRecordingStage(
            "dispatcher", execution_log, StageAction.WORKFLOW_STARTED
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        result = asyncio.run(pipeline.process(payload))

        # All three stages executed in strict order
        assert execution_log == ["event_dedup", "loop_guard", "dispatcher"], (
            f"Expected full ordering, got {execution_log}"
        )
        assert result.final_action == StageAction.WORKFLOW_STARTED

    @_PROFILE
    @given(payload=webhook_payloads())
    def test_no_stage_is_skipped_regardless_of_payload(
        self, payload: WebhookPayload
    ) -> None:
        """The pipeline never skips a stage based on payload content.
        When all stages pass, all three are always invoked regardless
        of what the payload contains.
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.PASS)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.PASS)
        dispatcher = OrderRecordingStage("dispatcher", execution_log, StageAction.PASS)

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        asyncio.run(pipeline.process(payload))

        # Verify no stage was skipped
        assert len(execution_log) == 3, (
            f"Expected 3 stages to execute, got {len(execution_log)}: {execution_log}"
        )
        # Verify strict ordering
        assert execution_log[0] == "event_dedup"
        assert execution_log[1] == "loop_guard"
        assert execution_log[2] == "dispatcher"

    @_PROFILE
    @given(
        payload=webhook_payloads(),
        terminal_action=st.sampled_from([
            StageAction.SIGNALED,
            StageAction.WORKFLOW_STARTED,
            StageAction.ITERATION_STARTED,
        ]),
    )
    def test_ordering_preserved_for_all_terminal_actions(
        self, payload: WebhookPayload, terminal_action: StageAction
    ) -> None:
        """Regardless of which terminal action the dispatcher returns,
        the execution order is always dedup  loop_guard  dispatcher.
        """
        execution_log: list[str] = []

        dedup = OrderRecordingStage("event_dedup", execution_log, StageAction.PASS)
        loop_guard = OrderRecordingStage("loop_guard", execution_log, StageAction.PASS)
        dispatcher = OrderRecordingStage("dispatcher", execution_log, terminal_action)

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        result = asyncio.run(pipeline.process(payload))

        assert execution_log == ["event_dedup", "loop_guard", "dispatcher"]
        assert result.final_action == terminal_action
