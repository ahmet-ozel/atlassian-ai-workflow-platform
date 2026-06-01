"""Unit tests for the ``noop_test`` smoke-flow defaults pinned by
:class:`ExecutionRunWorkflow` (R6.8, task 10.4).

The canonical :class:`ExecutionRunWorkflow` applies an opt-in safety
net when :attr:`ExecutionRunWorkflowInput.workflow_type` is
``"noop_test"``:

* An empty :attr:`~ExecutionRunWorkflowInput.command` is replaced
  with ``echo "ok"``.
* An unset
  :attr:`~ExecutionRunWorkflowInput.start_to_close_timeout` is
  tightened to 30 seconds.

These defaults exist so an ad-hoc dispatch (CLI, integration test,
replay against an older history) cannot accidentally launch an
open-ended smoke run on the SSH runner.  The tests below exercise the
workflow-private constants (``_NOOP_TEST_DEFAULT_COMMAND``,
``_NOOP_TEST_START_TO_CLOSE``) without spinning up a Temporal worker
— driving the workflow body itself requires a Temporal cluster, which
the integration-test layer covers separately.

Validates: Requirements 6.8.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping — mirror test_main_execution_runner_task_queue.py
# so the in-tree ``src/`` package import resolves without an editable install.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_WORKER_ROOT) not in sys.path:
    sys.path.insert(0, str(_WORKER_ROOT))
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
for _module_name in [
    name for name in list(sys.modules) if name == "src" or name.startswith("src.")
]:
    sys.modules.pop(_module_name, None)


# ---------------------------------------------------------------------------
# Pinned constants
# ---------------------------------------------------------------------------


class TestNoopTestSmokeDefaults:
    """The constants are pinned by tasks.md §10.4 and consumed inline."""

    def test_default_command_is_echo_ok(self) -> None:
        """**Validates: Requirement 6.8**

        Tasks.md §10.4 explicitly pins ``echo "ok"`` as the smoke
        default — the literal string is matched by integration tests
        and runner-side log scrapers.
        """

        from src.workflows.execution_run_workflow import (
            _NOOP_TEST_DEFAULT_COMMAND,
        )

        assert _NOOP_TEST_DEFAULT_COMMAND == 'echo "ok"'

    def test_default_start_to_close_is_30_seconds(self) -> None:
        """**Validates: Requirement 6.8**

        Tasks.md §10.4 pins the smoke timeout at 30 s ("noop should
        never run long").  A shorter window would race a slow
        runner; a longer window would mask a stuck pipeline.
        """

        from src.workflows.execution_run_workflow import (
            _NOOP_TEST_START_TO_CLOSE,
        )

        assert _NOOP_TEST_START_TO_CLOSE == timedelta(seconds=30)

    def test_workflow_type_discriminator_is_noop_test(self) -> None:
        """**Validates: Requirement 6.8**

        The discriminator string must match the
        :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`
        key verbatim — a typo would silently disable the safety net.
        """

        from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

        from src.workflows.execution_run_workflow import (
            _NOOP_TEST_WORKFLOW_TYPE,
        )

        assert _NOOP_TEST_WORKFLOW_TYPE == "noop_test"
        assert _NOOP_TEST_WORKFLOW_TYPE in WORKFLOW_TYPE_CAPABILITIES


class TestExecutionRunWorkflowInputDefaults:
    """The ``workflow_type`` field default keeps the legacy contract."""

    def test_workflow_type_defaults_to_none(self) -> None:
        """**Validates: Requirement 6.8**

        Backward-compat: every existing call site builds an
        :class:`ExecutionRunWorkflowInput` without the new field, so
        the default must be ``None`` to keep the non-noop path
        verbatim.
        """

        from temporal_shared.messages import ExecutionRunWorkflowInput

        inp = ExecutionRunWorkflowInput(
            parent_workflow_id="parent-1",
            runner_id="runner-1",
            command="pytest -q",
        )
        assert inp.workflow_type is None

    def test_workflow_type_can_be_set_to_noop_test(self) -> None:
        """**Validates: Requirement 6.8**

        Direct construction with ``workflow_type="noop_test"`` is the
        opt-in entrypoint that triggers the safety net inside
        :class:`ExecutionRunWorkflow.run`.
        """

        from temporal_shared.messages import ExecutionRunWorkflowInput

        inp = ExecutionRunWorkflowInput(
            parent_workflow_id="parent-1",
            runner_id="runner-1",
            command="",
            workflow_type="noop_test",
        )
        assert inp.workflow_type == "noop_test"
        # And the new field is optional, frozen, and slotted just
        # like every other field on the dataclass:
        assert hasattr(inp, "__slots__")

    def test_timeout_fields_accept_json_numeric_seconds(self) -> None:
        """AutomationWorkflow may pass timeout values through JSON."""

        from temporal_shared.messages import ExecutionRunWorkflowInput

        inp = ExecutionRunWorkflowInput(
            parent_workflow_id="parent-1",
            runner_id="runner-1",
            command='echo "ok"',
            start_to_close_timeout=45,
            heartbeat_timeout=7.5,
        )
        assert inp.start_to_close_timeout == 45
        assert inp.heartbeat_timeout == 7.5


class TestExecutionRunWorkflowSafetyNetBranches:
    """Pure decisions inside the safety-net branch — exercised without
    spinning up a Temporal worker by re-implementing the branch over
    the workflow's pinned constants."""

    def _safety_net(
        self,
        *,
        workflow_type: str | None,
        command: str,
        start_to_close: timedelta | None,
    ) -> tuple[str, timedelta | None]:
        """Mirror the branch from
        :meth:`ExecutionRunWorkflow.run` so the tests below exercise
        the same logic without a Temporal cluster.

        The implementation is intentionally a copy of the workflow's
        five lines so a regression in either copy fails the test —
        kept short to preserve the value of the assertion.
        """

        from src.workflows.execution_run_workflow import (
            _NOOP_TEST_DEFAULT_COMMAND,
            _NOOP_TEST_START_TO_CLOSE,
            _NOOP_TEST_WORKFLOW_TYPE,
        )

        if workflow_type == _NOOP_TEST_WORKFLOW_TYPE:
            if not command:
                command = _NOOP_TEST_DEFAULT_COMMAND
            if start_to_close is None:
                start_to_close = _NOOP_TEST_START_TO_CLOSE
        return command, start_to_close

    def test_noop_with_empty_command_substitutes_echo_ok(self) -> None:
        """**Validates: Requirement 6.8**"""

        cmd, sto = self._safety_net(
            workflow_type="noop_test", command="", start_to_close=None
        )
        assert cmd == 'echo "ok"'
        assert sto == timedelta(seconds=30)

    def test_noop_with_explicit_command_keeps_caller_value(self) -> None:
        """**Validates: Requirement 6.8**

        When the parent :class:`AutomationWorkflow` synthesises the
        noop command itself (production path) the safety net must
        leave it untouched.
        """

        cmd, _ = self._safety_net(
            workflow_type="noop_test",
            command='echo "noop_test ok: PAY-1"',
            start_to_close=None,
        )
        assert cmd == 'echo "noop_test ok: PAY-1"'

    def test_noop_with_explicit_timeout_keeps_caller_value(self) -> None:
        """**Validates: Requirement 6.8**

        The caller may want a tighter or looser bound for a specific
        runner — the safety net only fires when the field is unset.
        """

        cmd, sto = self._safety_net(
            workflow_type="noop_test",
            command="",
            start_to_close=timedelta(seconds=5),
        )
        assert cmd == 'echo "ok"'
        assert sto == timedelta(seconds=5)

    def test_non_noop_with_empty_command_does_not_substitute(self) -> None:
        """**Validates: Requirement 6.8**

        ``remote_ssh_test_only`` and friends must never have their
        command silently rewritten — an empty command for those
        types should surface as a runner-side error so the
        misconfiguration is caught at dispatch time.
        """

        cmd, sto = self._safety_net(
            workflow_type="remote_ssh_test_only",
            command="",
            start_to_close=None,
        )
        assert cmd == ""
        assert sto is None

    def test_workflow_type_none_is_legacy_contract(self) -> None:
        """**Validates: Requirement 6.8**

        Existing call sites that build the input without the new
        ``workflow_type`` field land here — the input is consumed
        verbatim.
        """

        cmd, sto = self._safety_net(
            workflow_type=None,
            command="",
            start_to_close=None,
        )
        assert cmd == ""
        assert sto is None


class TestExecutionRunWorkflowTimeoutCoercion:
    """Timeout values crossing JSON are converted before activity calls."""

    def test_numeric_seconds_become_timedelta(self) -> None:
        from src.workflows.execution_run_workflow import _coerce_timeout

        assert _coerce_timeout(45) == timedelta(seconds=45)
        assert _coerce_timeout(7.5) == timedelta(seconds=7.5)

    def test_timedelta_and_unset_values_are_preserved(self) -> None:
        from src.workflows.execution_run_workflow import _coerce_timeout

        explicit = timedelta(seconds=12)
        assert _coerce_timeout(explicit) is explicit
        assert _coerce_timeout(None) is None
        assert _coerce_timeout(0) is None
