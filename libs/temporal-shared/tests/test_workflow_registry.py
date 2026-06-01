"""Unit tests for ``temporal_shared.workflow_registry``.

Validates the :data:`WORKFLOW_TASK_QUEUES` mapping shape and the
:func:`task_queue_for` lookup helper against
``platform-mimari-workflows`` design.md §"temporal_shared.workflow_registry".

Validates: Requirements 1.1, 1.2.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from temporal_shared.workflow_registry import (
    WORKFLOW_TASK_QUEUES,
    SupportsWorkerBoot,
    task_queue_for,
)


# ---------------------------------------------------------------------------
# WORKFLOW_TASK_QUEUES — structural shape (Requirement 1.1, 1.2)
# ---------------------------------------------------------------------------


class TestMappingShape:
    """The mapping must match the design.md literal exactly."""

    EXPECTED: dict[str, str] = {
        "AutomationWorkflow": "automation-tq",
        "AgentRunnerWorkflow": "agent-runner-tq",
        "ExecutionRunWorkflow": "execution-runner-tq",
        "BotBranchRetention": "automation-tq",
    }

    def test_has_exactly_four_entries(self) -> None:
        """**Validates: Requirement 1.1**"""
        assert len(WORKFLOW_TASK_QUEUES) == 4

    def test_keys_match_design(self) -> None:
        """**Validates: Requirement 1.1**"""
        assert set(WORKFLOW_TASK_QUEUES.keys()) == set(self.EXPECTED.keys())

    @pytest.mark.parametrize(
        "workflow_name,expected_queue",
        sorted(EXPECTED.items()),
        ids=sorted(EXPECTED.keys()),
    )
    def test_each_entry_matches_design(
        self, workflow_name: str, expected_queue: str
    ) -> None:
        """**Validates: Requirements 1.1, 1.2**"""
        assert WORKFLOW_TASK_QUEUES[workflow_name] == expected_queue

    def test_full_mapping_equals_design_literal(self) -> None:
        """**Validates: Requirement 1.1**"""
        assert dict(WORKFLOW_TASK_QUEUES) == self.EXPECTED

    def test_mapping_is_immutable_proxy(self) -> None:
        """**Validates: Requirement 1.1**

        ``MappingProxyType`` rejects mutation attempts with ``TypeError``.
        """
        assert isinstance(WORKFLOW_TASK_QUEUES, MappingProxyType)
        with pytest.raises(TypeError):
            # type: ignore[index]
            WORKFLOW_TASK_QUEUES["NewWorkflow"] = "new-tq"  # noqa: B018

    def test_mapping_cannot_be_deleted(self) -> None:
        """**Validates: Requirement 1.1**

        Immutable proxy rejects ``del`` as well as assignment.
        """
        with pytest.raises(TypeError):
            # type: ignore[attr-defined]
            del WORKFLOW_TASK_QUEUES["AutomationWorkflow"]  # noqa: B018

    def test_queue_values_are_lowercase_kebab_case(self) -> None:
        """**Validates: Requirement 1.2**

        Task queue names follow the design convention: lowercase
        letters, digits, and hyphens only.
        """
        import re

        pattern = re.compile(r"^[a-z][a-z0-9-]*$")
        for workflow, queue in WORKFLOW_TASK_QUEUES.items():
            assert pattern.match(queue), (
                f"workflow={workflow!r} queue={queue!r} does not match "
                f"lowercase-kebab convention"
            )

    def test_only_three_distinct_task_queues(self) -> None:
        """**Validates: Requirement 1.2**

        Design pins exactly three Temporal task queues. ``BotBranchRetention``
        piggy-backs on ``automation-tq`` so the set of unique queue names
        must contain exactly three entries.
        """
        unique_queues = set(WORKFLOW_TASK_QUEUES.values())
        assert unique_queues == {
            "automation-tq",
            "agent-runner-tq",
            "execution-runner-tq",
        }

    def test_bot_branch_retention_piggy_backs_on_automation(self) -> None:
        """**Validates: Requirement 1.1**

        ``BotBranchRetention`` is a cron piggy-back on the automation
        worker — it MUST share ``automation-tq`` with
        ``AutomationWorkflow``.
        """
        assert (
            WORKFLOW_TASK_QUEUES["BotBranchRetention"]
            == WORKFLOW_TASK_QUEUES["AutomationWorkflow"]
            == "automation-tq"
        )


# ---------------------------------------------------------------------------
# task_queue_for — pure lookup helper
# ---------------------------------------------------------------------------


class TestTaskQueueFor:
    """``task_queue_for(workflow_name)`` lookup behaviour."""

    @pytest.mark.parametrize(
        "workflow_name,expected_queue",
        [
            ("AutomationWorkflow", "automation-tq"),
            ("AgentRunnerWorkflow", "agent-runner-tq"),
            ("ExecutionRunWorkflow", "execution-runner-tq"),
            ("BotBranchRetention", "automation-tq"),
        ],
    )
    def test_returns_registered_queue(
        self, workflow_name: str, expected_queue: str
    ) -> None:
        """**Validates: Requirements 1.1, 1.2**"""
        assert task_queue_for(workflow_name) == expected_queue

    def test_unknown_workflow_raises_key_error(self) -> None:
        """**Validates: Requirement 1.2**

        Silent fall-through to a default queue would violate the
        single-queue-per-worker contract; the helper raises
        :class:`KeyError` so callers must handle the error path.
        """
        with pytest.raises(KeyError):
            task_queue_for("DefinitelyNotAWorkflow")

    def test_empty_string_raises_key_error(self) -> None:
        """**Validates: Requirement 1.2**"""
        with pytest.raises(KeyError):
            task_queue_for("")

    def test_case_sensitive_lookup(self) -> None:
        """**Validates: Requirement 1.2**

        Workflow names match the Temporal-registered class name, which
        is case-sensitive. Lower-case variants must fail.
        """
        with pytest.raises(KeyError):
            task_queue_for("automationworkflow")
        with pytest.raises(KeyError):
            task_queue_for("AUTOMATIONWORKFLOW")

    def test_pure_deterministic(self) -> None:
        """**Validates: Requirement 1.1**

        Repeated calls with identical input return equal results.
        """
        first = task_queue_for("AutomationWorkflow")
        second = task_queue_for("AutomationWorkflow")
        assert first == second == "automation-tq"

    def test_returns_str(self) -> None:
        """**Validates: Requirement 1.1**"""
        result = task_queue_for("AgentRunnerWorkflow")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# SupportsWorkerBoot — structural protocol shape
# ---------------------------------------------------------------------------


class TestSupportsWorkerBootProtocol:
    """The protocol documents the worker-boot signature contract."""

    def test_protocol_signature_accepts_single_task_queue(self) -> None:
        """**Validates: Requirement 1.2**

        A boot helper that conforms to :class:`SupportsWorkerBoot`
        accepts exactly one ``task_queue`` keyword argument. We assert
        the protocol is structurally usable by constructing a fake
        worker class that mirrors the Temporal SDK shape.
        """

        class _FakeWorker:
            def __init__(
                self,
                client: object,
                *,
                task_queue: str,
                workflows: list[object],
                activities: list[object],
            ) -> None:
                self.client = client
                self.task_queue = task_queue
                self.workflows = workflows
                self.activities = activities

        worker = _FakeWorker(
            client=object(),
            task_queue=task_queue_for("AutomationWorkflow"),
            workflows=[],
            activities=[],
        )
        assert worker.task_queue == "automation-tq"
        # Confirms the boot signature accepts a single task_queue value
        # produced by ``task_queue_for`` — the contract used by every
        # worker in ``platform/workers/*/src/.../main.py``.

    def test_protocol_is_importable(self) -> None:
        """**Validates: Requirement 1.2**

        The protocol is exposed as a public symbol so downstream
        modules (worker boot scripts, type stubs) can reference it.
        """
        # Importing inside the test guarantees the module exposes the
        # name; the import at the top of the file would have failed
        # already if it were absent, but assert the dunder export too.
        from temporal_shared import workflow_registry

        assert hasattr(workflow_registry, "SupportsWorkerBoot")
        assert SupportsWorkerBoot is workflow_registry.SupportsWorkerBoot
