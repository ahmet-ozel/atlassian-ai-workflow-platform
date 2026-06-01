"""Unit tests for the per-issue iteration storm-guard cap.

The activity-side and workflow-side behaviour are exercised
independently:

* :func:`prepare_iteration` returns ``authorized=False`` with
  ``reason="max_iteration_exceeded"`` and a populated
  ``current_count`` once the latest stored ``iteration_number`` has
  reached :data:`MAX_ITERATIONS_PER_ISSUE`. The 9 → 10 boundary
  (last-allowed → first-rejected) is locked down explicitly so a
  future refactor that flips the comparison from ``>=`` to ``>``
  surfaces as a regression.

* :class:`IterationWorkflow` does **not** start the child
  :class:`AutomationWorkflow` when the activity rejects with
  ``max_iteration_exceeded``, posts a Turkish-prose Jira comment via
  the ``jira_add_comment`` activity, and writes an audit row with
  ``action="iteration_max_exceeded"``. The workflow returns its
  ``unauthorized`` envelope rather than raising — a stray
  ``[iterate]`` storm must never crash the worker.

Strategy
--------

The activity layer is exercised with the same in-memory
:class:`IterationStore` fake the existing
``test_iteration_manager.py`` suite uses, registered through
:func:`set_iteration_store`. The workflow layer is exercised through
the Temporal test environment (``temporalio.testing.WorkflowEnvironment``)
with three hand-rolled activity stubs: ``prepare_iteration`` returns a
canned ``max_iteration_exceeded`` :class:`IterationContext`,
``jira_add_comment`` and ``audit_write`` record their invocations on
shared lists.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — keep parity with sibling unit tests
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# pylint: disable=wrong-import-position
from automation_worker.activities import iteration_manager as im  # noqa: E402
from automation_worker.activities.iteration_manager import (  # noqa: E402
    DEFAULT_WORKSPACE_BASE_PATH,
    MAX_ITERATIONS_PER_ISSUE,
    IterationContext,
    IterationRecord,
    PrepareIterationInput,
    prepare_iteration,
    set_iteration_store,
)


# ---------------------------------------------------------------------------
# In-memory fake IterationStore (mirrors test_iteration_manager.py)
# ---------------------------------------------------------------------------


@dataclass
class _InMemoryStore:
    rows: list[IterationRecord] = field(default_factory=list)

    async def latest_iteration(
        self, issue_key: str
    ) -> IterationRecord | None:
        candidates = [r for r in self.rows if r.issue_key == issue_key]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.iteration_number)

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
        self.rows.append(
            IterationRecord(
                issue_key=issue_key,
                iteration_number=iteration_number,
                workflow_id=workflow_id,
                previous_branch=previous_branch,
                previous_pr_id=previous_pr_id,
                workspace_path=workspace_path,
                status=status,
                created_at=datetime.now(timezone.utc),
            )
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_module_state() -> None:
    snapshot = (
        im._db_pool,  # noqa: SLF001
        im._iteration_store,  # noqa: SLF001
        im._workspace_base_path,  # noqa: SLF001
    )
    im._db_pool = None  # noqa: SLF001
    im._iteration_store = None  # noqa: SLF001
    im._workspace_base_path = DEFAULT_WORKSPACE_BASE_PATH  # noqa: SLF001
    yield
    (
        im._db_pool,  # noqa: SLF001
        im._iteration_store,  # noqa: SLF001
        im._workspace_base_path,  # noqa: SLF001
    ) = snapshot


def _make_input(
    *,
    issue_key: str = "PAY-4211",
    author: str = "user-alice",
) -> PrepareIterationInput:
    return PrepareIterationInput(
        issue_key=issue_key,
        comment_body="[iterate]",
        comment_author_account_id=author,
        issue_reporter_account_id="user-bob",
        dept_id="platform",
        dept_config={"approvers": [author]},
        trace_id="",
    )


def _make_record(*, issue_key: str, iteration_number: int) -> IterationRecord:
    return IterationRecord(
        issue_key=issue_key,
        iteration_number=iteration_number,
        workflow_id=f"iteration-{issue_key}-{iteration_number}-aaaa",
        previous_branch=None,
        previous_pr_id=None,
        workspace_path=f"/var/ai-runner/{issue_key}/iter-{iteration_number}",
        status="completed",
        created_at=datetime.now(timezone.utc),
    )


# ===========================================================================
# 1. Activity layer — storm-guard boundary
# ===========================================================================


class TestActivityStormGuard:
    """``prepare_iteration`` enforces the per-issue cap at the right
    boundary.

    The cap is *inclusive* on the current count: ``current_count == 10``
    means the next ``[iterate]`` is rejected. The boundary at
    ``current_count == 9`` is the last allowed slot — it produces
    ``next_iteration_n = 10`` with ``authorized=True``.
    """

    def test_constant_value(self) -> None:
        # Pin the storm-guard cap in the test suite so a future
        # refactor that bumps it must update the tests deliberately.
        assert MAX_ITERATIONS_PER_ISSUE == 10

    def test_count_9_authorizes_iter_10(self) -> None:
        """The last-allowed slot: count=9 → next=10 → authorized."""
        store = _InMemoryStore(
            rows=[_make_record(issue_key="PAY-4211", iteration_number=9)]
        )
        set_iteration_store(store)

        result = asyncio.run(prepare_iteration(_make_input()))

        assert result.authorized is True
        assert result.reason == ""
        assert result.iteration_number == 10
        # The persisted row matches the authorized iteration number.
        assert any(r.iteration_number == 10 for r in store.rows)

    def test_count_10_rejects_with_max_exceeded(self) -> None:
        """Cap is inclusive: count=10 → reject + ``current_count=10``."""
        store = _InMemoryStore(
            rows=[
                _make_record(issue_key="PAY-4211", iteration_number=10)
            ]
        )
        set_iteration_store(store)

        result = asyncio.run(prepare_iteration(_make_input()))

        assert result.authorized is False
        assert result.reason == "max_iteration_exceeded"
        assert result.current_count == 10
        # No new row inserted — the storm-guard runs before the
        # workspace path / insert step.
        assert len(store.rows) == 1

    def test_count_15_rejects_with_max_exceeded(self) -> None:
        """Above the cap: count=15 → reject + ``current_count=15``.

        Defends against the cap being implemented as ``== cap``
        rather than ``>= cap`` — without the explicit ``>=`` guard
        an issue with 15 prior iterations would slip through.
        """
        store = _InMemoryStore(
            rows=[
                _make_record(issue_key="PAY-4211", iteration_number=15)
            ]
        )
        set_iteration_store(store)

        result = asyncio.run(prepare_iteration(_make_input()))

        assert result.authorized is False
        assert result.reason == "max_iteration_exceeded"
        assert result.current_count == 15
        assert len(store.rows) == 1

    def test_no_rows_authorizes_iter_1(self) -> None:
        """The empty-history path is unaffected by the storm-guard."""
        store = _InMemoryStore()
        set_iteration_store(store)

        result = asyncio.run(prepare_iteration(_make_input()))

        assert result.authorized is True
        assert result.iteration_number == 1
        # Authorized result does not populate ``current_count``.
        assert result.current_count is None


# ===========================================================================
# 2. Workflow layer — Jira comment + audit row + no child dispatch
# ===========================================================================
#
# Driven through the Temporal test environment so the workflow body
# runs end-to-end against canned activity stubs. The stubs record
# every invocation on shared lists which the assertions then inspect.


class _Recorder:
    """Captures activity invocations for the workflow-level test."""

    def __init__(self) -> None:
        self.jira_calls: list[tuple[str, str, str]] = []
        self.audit_calls: list[dict[str, Any]] = []
        self.child_started: bool = False


def _build_canned_context(
    *, issue_key: str, current_count: int
) -> IterationContext:
    return IterationContext(
        authorized=False,
        reason="max_iteration_exceeded",
        issue_key=issue_key,
        iteration_number=0,
        workflow_id="",
        workspace_path="",
        previous_branch=None,
        previous_pr_id=None,
        extra_instructions=None,
        dept_id="platform",
        trace_id="trace-test",
        current_count=current_count,
    )


@pytest.mark.asyncio
async def test_workflow_max_iteration_exceeded_handling() -> None:
    """End-to-end: activity rejects → no child + Jira comment + audit row.

    Uses :class:`temporalio.testing.WorkflowEnvironment` so the
    workflow body runs against a real Temporal server (the test
    environment ships an embedded Temporalite). The
    ``prepare_iteration`` activity is stubbed to return a canned
    ``max_iteration_exceeded`` context; ``jira_add_comment`` and
    ``audit_write`` are stubbed to record their invocations. The
    child :class:`AutomationWorkflow` is *not* registered — if the
    workflow attempts to start it the test fails with a clear
    ``unable to find workflow`` error.
    """

    pytest.importorskip("temporalio.testing")
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from automation_worker.workflows.iteration_workflow import (
        IterationWorkflow,
        IterationWorkflowInput,
    )

    recorder = _Recorder()

    @activity.defn(name="prepare_iteration")
    async def fake_prepare_iteration(
        _: PrepareIterationInput,
    ) -> IterationContext:
        return _build_canned_context(
            issue_key="PAY-4211", current_count=10
        )

    @activity.defn(name="jira_add_comment")
    async def fake_jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        recorder.jira_calls.append((issue_key, body, dept_id))

    @activity.defn(name="audit_write")
    async def fake_audit_write(payload: dict[str, Any]) -> None:
        recorder.audit_calls.append(payload)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"iteration-test-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IterationWorkflow],
            activities=[
                fake_prepare_iteration,
                fake_jira_add_comment,
                fake_audit_write,
            ],
        ):
            result = await env.client.execute_workflow(
                IterationWorkflow.run,
                IterationWorkflowInput(
                    trigger="iterate",
                    issue_key="PAY-4211",
                    department_id="platform",
                    actor_account_id="user-alice",
                    issue_reporter_account_id="user-bob",
                    comment_body="[iterate]",
                    dept_config={"approvers": ["user-alice"]},
                    trace_id="trace-test",
                ),
                id=f"iter-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    # Workflow returned a clean ``unauthorized`` envelope — never
    # raised, no child started.
    assert result.decision == "unauthorized"
    assert result.reason == "max_iteration_exceeded"
    assert result.child_workflow_id is None
    assert result.automation_output is None

    # Jira comment posted exactly once with the storm-guard body.
    assert len(recorder.jira_calls) == 1
    issue_key, body, dept_id = recorder.jira_calls[0]
    assert issue_key == "PAY-4211"
    assert dept_id == "platform"
    assert "10" in body  # cap surfaced
    assert "mevcut" in body or "Mevcut" in body  # Turkish-prose hint
    assert "🤖" in body

    # Audit row written exactly once with the storm-guard action.
    assert len(recorder.audit_calls) == 1
    audit = recorder.audit_calls[0]
    assert audit["action"] == "iteration_max_exceeded"
    assert audit["issue_key"] == "PAY-4211"
    payload = audit["payload"]
    assert payload["issue_key"] == "PAY-4211"
    assert payload["current_count"] == 10
    assert payload["cap"] == MAX_ITERATIONS_PER_ISSUE


@pytest.mark.asyncio
async def test_workflow_authorized_path_does_not_emit_storm_guard() -> None:
    """Sanity check: authorized result must *not* trigger the
    storm-guard side effects.

    Guards against an over-eager refactor that calls the
    storm-guard handler on every ``authorized=False`` result, which
    would double-audit unauthorized ``[iterate]`` denials.
    """

    pytest.importorskip("temporalio.testing")
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from automation_worker.workflows.iteration_workflow import (
        IterationWorkflow,
        IterationWorkflowInput,
    )

    recorder = _Recorder()

    @activity.defn(name="prepare_iteration")
    async def fake_prepare_iteration(
        _: PrepareIterationInput,
    ) -> IterationContext:
        # ``not_authorized`` reason — the dispatcher rejected the
        # comment author, the activity confirmed. Storm-guard side
        # effects MUST NOT fire here.
        return IterationContext(
            authorized=False,
            reason="not_authorized",
            issue_key="PAY-4211",
            iteration_number=0,
            workflow_id="",
            workspace_path="",
            previous_branch=None,
            previous_pr_id=None,
            extra_instructions=None,
            dept_id="platform",
            trace_id="trace-test",
            current_count=None,
        )

    @activity.defn(name="jira_add_comment")
    async def fake_jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        recorder.jira_calls.append((issue_key, body, dept_id))

    @activity.defn(name="audit_write")
    async def fake_audit_write(payload: dict[str, Any]) -> None:
        recorder.audit_calls.append(payload)

    async with await WorkflowEnvironment.start_time_skipping() as env:
        task_queue = f"iteration-test-{uuid.uuid4().hex[:8]}"
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[IterationWorkflow],
            activities=[
                fake_prepare_iteration,
                fake_jira_add_comment,
                fake_audit_write,
            ],
        ):
            result = await env.client.execute_workflow(
                IterationWorkflow.run,
                IterationWorkflowInput(
                    trigger="iterate",
                    issue_key="PAY-4211",
                    department_id="platform",
                    actor_account_id="user-stranger",
                    issue_reporter_account_id="user-bob",
                    comment_body="[iterate]",
                    dept_config={"approvers": ["user-alice"]},
                    trace_id="trace-test",
                ),
                id=f"iter-test-{uuid.uuid4().hex[:8]}",
                task_queue=task_queue,
            )

    assert result.decision == "unauthorized"
    assert result.reason == "not_authorized"
    # Crucially: no storm-guard side effects for the not_authorized
    # reason — the dispatcher's audit row is the only record.
    assert recorder.jira_calls == []
    assert recorder.audit_calls == []
