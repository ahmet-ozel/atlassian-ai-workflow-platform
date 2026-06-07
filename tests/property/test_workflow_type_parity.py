"""Workflow-type vocabulary parity property test (EK3).

Locks the three sources that previously drifted apart:

1. ``task_analyzer.VALID_WORKFLOW_TYPES`` - what the analyser is allowed
   to emit (and what the task-creation assistant advertises to users).
2. ``temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`` - what the
   capability gate accepts.
3. ``automation_workflow._AGENT_RUNNER_WORKFLOW_TYPES`` /
   ``_EXECUTION_RUN_WORKFLOW_TYPES`` - what the gateway routes.

Before the EK3 fix the analyser produced ``script_execute``,
``research_publish_confluence`` and ``research_summary_jira`` but the
capability table only knew 10 of the 13 types; the gateway rejected
those tasks with ``unknown_workflow_type``. ``script_execute`` was also
absent from both routing sets so it would have been misrouted to the
agent-runner queue via the ``else`` fallback.

The tests below MUST stay green on every change. Add a workflow type
in only one place → CI red.
"""

from __future__ import annotations

import sys
from pathlib import Path

# K3-compatible worker src injection: the top-level conftest no longer
# globally adds worker dirs to sys.path because doing so caused cross-
# service module-name collisions and regressed the collection-error
# count. This test module needs the worker namespaces, so it injects
# them locally - affecting only the modules this file imports.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKER_DIRS = (
    _REPO_ROOT / "workers" / "automation-worker" / "src",
    _REPO_ROOT / "workers" / "agent-runner-worker" / "src",
    _REPO_ROOT / "workers" / "execution-runner-worker" / "src",
)
for _wd in _WORKER_DIRS:
    if _wd.is_dir() and str(_wd) not in sys.path:
        sys.path.insert(0, str(_wd))


def test_task_analyzer_matches_capability_table() -> None:
    """Every analyser-valid workflow type must have a capability set,
    and every capability-table type must be analyser-valid."""
    from automation_worker.activities.task_analyzer import (  # type: ignore[import-not-found]
        VALID_WORKFLOW_TYPES,
    )
    from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

    analyzer_set = set(VALID_WORKFLOW_TYPES)
    capability_set = set(WORKFLOW_TYPE_CAPABILITIES.keys())

    extra_in_analyzer = analyzer_set - capability_set
    extra_in_capabilities = capability_set - analyzer_set

    assert not extra_in_analyzer, (
        "Workflow types in VALID_WORKFLOW_TYPES but missing from "
        f"WORKFLOW_TYPE_CAPABILITIES: {sorted(extra_in_analyzer)}. "
        "The gateway will reject these as 'unknown_workflow_type'."
    )
    assert not extra_in_capabilities, (
        "Workflow types in WORKFLOW_TYPE_CAPABILITIES but missing from "
        f"VALID_WORKFLOW_TYPES: {sorted(extra_in_capabilities)}. "
        "Either remove from the capability table or add to the analyser."
    )


def test_every_workflow_type_is_routed() -> None:
    """Every workflow type must be in exactly one routing set so the
    gateway's ``_build_child_spec`` picks a deterministic child without
    falling into the defensive ``else`` branch."""
    from automation_worker.workflows.automation_workflow import (  # type: ignore[import-not-found]
        _AGENT_RUNNER_WORKFLOW_TYPES,
        _EXECUTION_RUN_WORKFLOW_TYPES,
    )
    from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

    capability_set = set(WORKFLOW_TYPE_CAPABILITIES.keys())
    agent_set = set(_AGENT_RUNNER_WORKFLOW_TYPES)
    exec_set = set(_EXECUTION_RUN_WORKFLOW_TYPES)

    routed = agent_set | exec_set
    unrouted = capability_set - routed
    assert not unrouted, (
        f"Workflow types with no explicit routing: {sorted(unrouted)}. "
        "Each must be in either _AGENT_RUNNER_WORKFLOW_TYPES or "
        "_EXECUTION_RUN_WORKFLOW_TYPES (not both)."
    )


def test_routing_sets_are_disjoint() -> None:
    """A workflow type cannot be both an agent-runner task and an
    execution-runner task - the gateway picks the wrong child if both
    sets claim it."""
    from automation_worker.workflows.automation_workflow import (  # type: ignore[import-not-found]
        _AGENT_RUNNER_WORKFLOW_TYPES,
        _EXECUTION_RUN_WORKFLOW_TYPES,
    )

    overlap = set(_AGENT_RUNNER_WORKFLOW_TYPES) & set(
        _EXECUTION_RUN_WORKFLOW_TYPES
    )
    assert not overlap, (
        f"Workflow types in BOTH routing sets: {sorted(overlap)}. "
        "Pick one - these dispatch through different Temporal queues."
    )


def test_script_execute_is_executable_route() -> None:
    """Spec lock: ``script_execute`` must land on the SSH executor, not
    the LLM agent runner. Guards against accidental routing-set moves."""
    from automation_worker.workflows.automation_workflow import (  # type: ignore[import-not-found]
        _AGENT_RUNNER_WORKFLOW_TYPES,
        _EXECUTION_RUN_WORKFLOW_TYPES,
    )

    assert "script_execute" in _EXECUTION_RUN_WORKFLOW_TYPES
    assert "script_execute" not in _AGENT_RUNNER_WORKFLOW_TYPES


def test_capability_table_size_at_least_13() -> None:
    """Sanity floor: after the EK3 fix the table has 13 entries. This
    test catches accidental deletions during refactors."""
    from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

    assert len(WORKFLOW_TYPE_CAPABILITIES) >= 13, (
        f"WORKFLOW_TYPE_CAPABILITIES shrunk to "
        f"{len(WORKFLOW_TYPE_CAPABILITIES)} - expected ≥13 after EK3."
    )
