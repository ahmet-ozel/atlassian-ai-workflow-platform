"""Property tests for workflow determinism (Temporal replay invariant).

**Validates: Requirements 5.9, 6.12, 10.1, 10.2, 10.3**

Property 11 (replay): Workflow determinism — Temporal replay invariance

For every sample workflow history under
``platform/tests/fixtures/histories/*.json``, ``temporalio.worker.
Replayer.replay_workflow(...)`` SHALL succeed with
``WorkflowReplayResult.replay_failure is None``. Any non-deterministic
deviation surfaces as a ``temporalio.worker.workflow_sandbox`` /
``temporalio.workflow.NondeterminismError`` and fails the test.

This test complements the static AST scan
(``test_workflow_determinism_static.py``). Together they cover both the
syntactic (no banned-call) and semantic (replay-stable behaviour) sides
of Property 11.

Fixture generation strategy
---------------------------

The history fixtures live under
``platform/tests/fixtures/histories/`` and are pre-recorded. The
generator (``_generate_history_fixtures``) runs the AutomationWorkflow
against the temporalio time-skipping test server with mocked activities
to produce deterministic histories, then dumps each via
``WorkflowHistory.to_json()`` so the replay test runs without
contacting any Temporal cluster — a critical invariant for offline /
hermetic CI lanes.

The fixtures are auto-generated on first run if missing (one-time cost,
~10s while the test-server binary is downloaded). Subsequent runs read
the JSON files directly. To force regeneration after a workflow-body
change, delete the ``*.json`` files under ``tests/fixtures/histories/``
or run the module directly:

    python -m tests.property.test_workflow_determinism_replay

from ``platform/``. The CLI entry point overwrites every fixture in
place.

AgentRunnerWorkflow note
------------------------

``src.workflows.agent_runner_workflow.AgentRunnerWorkflow`` is currently
a stub (no ``@workflow.defn`` body) — see the AgentRunner task in
``.kiro/specs/p0-critical-path/tasks.md``. The replay test for that
workflow type is wired in but ``skip``s until the body is implemented.
Removing the skip is part of that follow-up task.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors and sys.path bootstrapping
# ---------------------------------------------------------------------------

# tests/property/test_workflow_determinism_replay.py → platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]

# Workers ship under ``workers/<name>/src/`` and import their submodules
# under the ``src.`` namespace (e.g. ``from src.activities.jira import
# IssueData``). Adding the *worker* directory (not its ``src/`` child)
# onto ``sys.path`` makes the ``src.`` package resolve to that worker's
# source tree so the workflow modules import cleanly during replay.
_AGENT_RUNNER_WORKER: Path = _PLATFORM_ROOT / "workers" / "agent-runner-worker"

for _candidate in (_AGENT_RUNNER_WORKER,):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)


# ---------------------------------------------------------------------------
# History fixtures
# ---------------------------------------------------------------------------

#: Directory holding pre-generated workflow history JSON fixtures.
HISTORY_DIR: Path = _PLATFORM_ROOT / "tests" / "fixtures" / "histories"


@dataclass(frozen=True)
class HistoryFixture:
    """A single pre-recorded workflow history paired with its scenario."""

    stem: str
    description: str


# Logical fixture inventory. Each entry maps a deterministic file name to
# the scenario it exercises. The generator (below) writes one JSON file
# per entry and the parametrised replay test reads them back.
AUTOMATION_FIXTURES: tuple[HistoryFixture, ...] = (
    HistoryFixture(
        stem="automation_workflow_jira_fetch_failed",
        description=(
            "jira_get_issue raises ApplicationError; workflow short-circuits "
            "via _fail(reason='task_analysis_failed') and completes "
            "deterministically. Exercises the early failure branch."
        ),
    ),
    HistoryFixture(
        stem="automation_workflow_llm_analysis_failed",
        description=(
            "jira_get_issue succeeds but llm_analyze_task fails; workflow "
            "exits via the analysis-error _fail() path. Exercises the deeper "
            "fail-after-fetch branch."
        ),
    ),
)

AGENT_RUNNER_FIXTURES: tuple[HistoryFixture, ...] = (
    HistoryFixture(
        stem="agent_runner_workflow_code_change_happy_path",
        description=(
            "code_change_with_test happy path through bitbucket_create_branch "
            "→ opencode_generate_code → bitbucket_create_commit → "
            "bitbucket_open_pr → ExecutionRunWorkflow child → artifact_upload."
        ),
    ),
)


def _fixture_path(stem: str) -> Path:
    return HISTORY_DIR / f"{stem}.json"


# ---------------------------------------------------------------------------
# Replay helper
# ---------------------------------------------------------------------------


def _load_history(path: Path) -> Any:
    """Load a JSON file and return a ``WorkflowHistory`` object.

    Accepts both Temporal UI/CLI JSON (``camelCase``) and the SDK's own
    ``WorkflowHistory.to_json`` output through the unified
    ``WorkflowHistory.from_json`` entry point.
    """

    from temporalio.client import WorkflowHistory

    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    workflow_id = data.get("workflow_id") or path.stem
    return WorkflowHistory.from_json(workflow_id, data)


async def _replay(history: Any, *workflow_classes: type) -> None:
    """Run ``Replayer`` over ``history`` and assert no replay failure.

    ``raise_on_replay_failure=True`` (the SDK default) makes the call
    throw on any non-determinism; we additionally read
    ``replay_failure`` for diagnostic clarity per Property 11.
    """

    from temporalio.worker import Replayer

    replayer = Replayer(workflows=list(workflow_classes))
    result = await replayer.replay_workflow(history, raise_on_replay_failure=True)

    assert result.replay_failure is None, (
        "Replayer surfaced replay_failure despite "
        f"raise_on_replay_failure=True: {result.replay_failure!r}"
    )


# ---------------------------------------------------------------------------
# Session fixture: ensure history JSON files exist
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _ensure_fixtures() -> None:
    """Generate missing history fixtures.

    Runs once per session. The generator imports
    :mod:`temporalio.testing.WorkflowEnvironment` and lazily downloads
    the test-server binary on first use; subsequent invocations reuse
    the cached binary so generation is fast.

    Only generates the AutomationWorkflow fixtures here.
    AgentRunnerWorkflow fixtures are gated behind the workflow's
    ``@workflow.defn`` body landing (see module docstring). To force
    regeneration delete the ``*.json`` files under ``HISTORY_DIR`` or
    invoke this module directly.
    """

    needs_generation = any(
        not _fixture_path(f.stem).is_file() for f in AUTOMATION_FIXTURES
    )

    if not needs_generation:
        return

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    import asyncio

    asyncio.run(_generate_history_fixtures())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_history_directory_exists() -> None:
    """**Validates: Requirements 5.9, 6.12, 10.1, 10.2, 10.3**

    The fixtures directory must exist; otherwise the parametrised tests
    below collect zero items and the property silently degrades to
    vacuous-true. Mirrors the non-vacuity guard in the static-AST test.
    The session-scoped ``_ensure_fixtures`` autouse fixture creates
    this directory before any test runs.
    """

    assert HISTORY_DIR.is_dir(), (
        f"history fixtures directory missing: "
        f"{HISTORY_DIR.relative_to(_PLATFORM_ROOT)} — the autouse "
        "session fixture should have created it; check generator errors."
    )


@pytest.mark.parametrize(
    "fixture",
    AUTOMATION_FIXTURES,
    ids=[f.stem for f in AUTOMATION_FIXTURES],
)
@pytest.mark.asyncio
async def test_automation_workflow_replays_deterministically(
    fixture: HistoryFixture,
) -> None:
    """**Validates: Requirements 5.9, 6.12, 10.1, 10.2, 10.3**

    Replay each pre-recorded ``AutomationWorkflow`` history. The current
    workflow code MUST be replay-compatible (no replay failure) against
    every sample history; any deviation indicates a determinism
    violation in the workflow body.
    """

    path = _fixture_path(fixture.stem)
    if not path.is_file():
        pytest.fail(
            f"history fixture missing: {path.relative_to(_PLATFORM_ROOT)} — "
            "the autouse session fixture failed to generate it. Delete "
            "any partial JSON files under tests/fixtures/histories/ and "
            "rerun the suite, or invoke "
            "`python -m tests.property.test_workflow_determinism_replay` "
            "from platform/ for an explicit regeneration. "
            f"(scenario: {fixture.description})"
        )

    # Imports here so test collection doesn't pull the worker subtree
    # into ``sys.modules`` for unrelated tests.
    from src.workflows.automation_workflow import AutomationWorkflow

    history = _load_history(path)
    await _replay(history, AutomationWorkflow)


@pytest.mark.parametrize(
    "fixture",
    AGENT_RUNNER_FIXTURES,
    ids=[f.stem for f in AGENT_RUNNER_FIXTURES],
)
@pytest.mark.asyncio
async def test_agent_runner_workflow_replays_deterministically(
    fixture: HistoryFixture,
) -> None:
    """**Validates: Requirements 5.9, 6.12, 10.1, 10.2, 10.3**

    Replay each pre-recorded ``AgentRunnerWorkflow`` history. The
    AgentRunnerWorkflow body is implemented in a follow-up task; until
    then this test ``skip``s to keep the suite green without hiding the
    requirement. The test will start running automatically once the
    fixture file exists and the class carries a ``@workflow.defn``
    decorator.
    """

    path = _fixture_path(fixture.stem)
    if not path.is_file():
        pytest.skip(
            f"history fixture missing: {path.relative_to(_PLATFORM_ROOT)} — "
            "AgentRunnerWorkflow body is currently a stub (see "
            "src/workflows/agent_runner_workflow.py). Generate the "
            "fixture and remove this skip once the workflow body is "
            f"implemented. (scenario: {fixture.description})"
        )

    # Defensive import: the stub class has no @workflow.defn decorator,
    # so Replayer.replay_workflow rejects it with a clear error. We
    # detect that case here and skip with a more actionable message
    # rather than letting the SDK raise.
    from src.workflows.agent_runner_workflow import AgentRunnerWorkflow

    if not _has_workflow_defn(AgentRunnerWorkflow):
        pytest.skip(
            "AgentRunnerWorkflow has no @workflow.defn decorator yet — "
            "implement the workflow body, then regenerate the fixture."
        )

    history = _load_history(path)
    await _replay(history, AgentRunnerWorkflow)


def _has_workflow_defn(cls: type) -> bool:
    """Return True if ``cls`` is decorated with ``@workflow.defn``.

    Uses the public ``temporalio.workflow._Definition.from_class`` lookup
    via the documented attribute exposed by the SDK. Falls back to a
    tolerant ``hasattr`` probe if the SDK changes its private layout.
    """

    from temporalio import workflow

    # Prefer the SDK's own helper.
    try:
        from temporalio.workflow import _Definition  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - defensive
        return any(
            getattr(getattr(cls, attr, None), "__temporal_workflow_run", False)
            for attr in dir(cls)
        )

    return _Definition.from_class(cls) is not None  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Fixture generator
# ---------------------------------------------------------------------------
#
# The generator runs AutomationWorkflow against the temporalio
# time-skipping test server with mocked activities. It does NOT register
# the AgentRunnerWorkflow stub because the chosen scenarios never reach
# the child-dispatch step (they all short-circuit via _fail() before
# the LLM analysis result is consumed).
#
# Activities are mocked so the generator is hermetic (no Vault /
# Postgres / Atlassian MCP / OpenCode / MinIO / LLM provider needed).


async def _generate_history_fixtures() -> None:
    """Generate every AutomationWorkflow history fixture.

    Idempotent: re-running overwrites existing fixtures with fresh (still
    deterministic) recordings. Safe to run from a clean checkout or from
    CI when fixtures need refresh.
    """

    # Local imports keep test-collection-time imports minimal.
    from temporalio import activity
    from temporalio.exceptions import ApplicationError
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationWorkflow,
    )

    # ----- Scenario-specific activity mocks ----------------------------
    # Each scenario binds a distinct set of activity callables to keep
    # the failure point unambiguous. Names match what AutomationWorkflow
    # passes to ``execute_activity("name", ...)``.

    @activity.defn(name="jira_add_comment")
    async def _ack_jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        return None

    @activity.defn(name="jira_transition_issue")
    async def _ack_jira_transition_issue(
        issue_key: str, target_status: str, dept_id: str
    ) -> None:
        return None

    @activity.defn(name="update_work_item_status")
    async def _ack_update_work_item_status(
        workflow_id: str, new_status: str
    ) -> None:
        return None

    # Scenario 1: jira_get_issue fails immediately -> _fail() path.
    @activity.defn(name="jira_get_issue")
    async def _failing_jira_get_issue(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        raise ApplicationError(
            "issue not found via Atlassian MCP (mocked failure)",
            non_retryable=True,
        )

    @activity.defn(name="llm_analyze_task")
    async def _unreachable_llm_analyze_task(
        issue: Any, ctx: Any
    ) -> dict[str, Any]:
        # This branch is never reached in scenario 1; raising makes
        # the mistake loud if the workflow body changes.
        raise ApplicationError(
            "llm_analyze_task should not be reached when "
            "jira_get_issue fails",
            non_retryable=True,
        )

    # Scenario 2: jira_get_issue succeeds, llm_analyze_task fails.
    @activity.defn(name="jira_get_issue")  # type: ignore[no-redef]
    async def _ok_jira_get_issue(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        return {
            "key": issue_key,
            "summary": "Add /healthz endpoint",
            "description": "Service needs a basic health probe.",
            "issue_type": "Story",
            "status": "To Do",
            "assignee_account_id": None,
            "project_key": issue_key.split("-", 1)[0],
            "labels": [],
            "priority": None,
        }

    @activity.defn(name="llm_analyze_task")  # type: ignore[no-redef]
    async def _failing_llm_analyze_task(
        issue: Any, ctx: Any
    ) -> dict[str, Any]:
        raise ApplicationError(
            "LLM provider unreachable (mocked failure)",
            non_retryable=True,
        )

    # ----- Run scenarios -----------------------------------------------

    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Scenario 1: jira_get_issue fails
        await _run_and_dump(
            client=env.client,
            task_queue="agent-runner-replay-gen-1",
            activities=[
                _ack_jira_add_comment,
                _failing_jira_get_issue,
                _unreachable_llm_analyze_task,
                _ack_jira_transition_issue,
                _ack_update_work_item_status,
            ],
            workflow_class=AutomationWorkflow,
            workflow_id="automation-jira-PAY-4211",
            workflow_input=AutomationInput(
                issue_key="PAY-4211",
                department_id="payments",
                available_capabilities=("jira",),
                iteration=1,
            ),
            output_path=_fixture_path(
                "automation_workflow_jira_fetch_failed"
            ),
        )

        # Scenario 2: jira_get_issue OK, llm_analyze_task fails
        await _run_and_dump(
            client=env.client,
            task_queue="agent-runner-replay-gen-2",
            activities=[
                _ack_jira_add_comment,
                _ok_jira_get_issue,
                _failing_llm_analyze_task,
                _ack_jira_transition_issue,
                _ack_update_work_item_status,
            ],
            workflow_class=AutomationWorkflow,
            workflow_id="automation-jira-PAY-4212",
            workflow_input=AutomationInput(
                issue_key="PAY-4212",
                department_id="payments",
                available_capabilities=("jira", "bitbucket", "execution"),
                available_repos=("payment-service",),
                iteration=1,
            ),
            output_path=_fixture_path(
                "automation_workflow_llm_analysis_failed"
            ),
        )


async def _run_and_dump(
    *,
    client: Any,
    task_queue: str,
    activities: list[Any],
    workflow_class: type,
    workflow_id: str,
    workflow_input: Any,
    output_path: Path,
) -> None:
    """Run a workflow against the test server, fetch history, dump JSON."""

    from temporalio.worker import Worker

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[workflow_class],
        activities=activities,
    ):
        handle = await client.start_workflow(
            workflow_class.__name__,
            workflow_input,
            id=workflow_id,
            task_queue=task_queue,
        )
        # Drain the workflow regardless of terminal status. Both chosen
        # scenarios complete (status="failed") rather than raising at
        # the workflow level, so handle.result() returns the
        # AutomationResult dict.
        try:
            await handle.result()
        except Exception:  # noqa: BLE001 — terminal workflow failure is OK here
            pass

        history = await handle.fetch_history()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(history.to_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry-point: regenerate fixtures without running tests
# ---------------------------------------------------------------------------


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import asyncio

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    asyncio.run(_generate_history_fixtures())
    print(f"Regenerated history fixtures under {HISTORY_DIR}")
