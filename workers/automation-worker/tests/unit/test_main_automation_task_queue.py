"""Unit tests for ``automation_worker.main`` boot script.

Validates the **single-queue-per-worker** invariant of
the worker boot path:

    * The boot script's ``Worker(...)`` constructor receives **exactly
      one** ``task_queue`` keyword argument.
    * That value is computed by
      :func:`temporal_shared.workflow_registry.task_queue_for` (no
      hardcoded queue string anywhere in the boot script).
    * The resolved queue equals the registry's entry for
      ``"AutomationWorkflow"`` (i.e. ``"automation-tq"``).
    * The ``Worker(...)`` call registers the canonical workflow set
      for this worker (``AutomationWorkflow``, ``BotBranchRetention``,
      ``AuditPruneWorkflow``) - note ``AuditPruneWorkflow`` is the
      preserved registration; this test asserts it is **not lost** when
      the worker registration changes.

The tests exercise both the **import-time module shape** (the
``AUTOMATION_TASK_QUEUE`` constant materialised at import) and the
**source-level AST** (the ``Worker(...)`` call site itself) so a
future refactor cannot trivially hide a queue-string drift behind a
helper variable.

"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping - the worker package ships under
# ``src/automation_worker/`` (mirrors ``hatchling`` ``packages = ["src/automation_worker"]``).
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ---------------------------------------------------------------------------
# Module-level constant tests - exercise the public surface of the
# boot script (the ``AUTOMATION_TASK_QUEUE`` constant materialised at
# import time).
# ---------------------------------------------------------------------------


class TestModuleLevelTaskQueueConstant:
    """The boot script materialises the queue at import time so the
    boot path and tests share a single, registry-derived value."""

    def test_constant_resolves_via_registry(self) -> None:
        """The constant resolves via the workflow registry."""

        from automation_worker import main as main_mod
        from temporal_shared.workflow_registry import task_queue_for

        assert main_mod.AUTOMATION_TASK_QUEUE == task_queue_for(
            "AutomationWorkflow"
        )

    def test_constant_value_is_automation_tq(self) -> None:
        """The constant value is automation-tq."""

        from automation_worker import main as main_mod

        assert main_mod.AUTOMATION_TASK_QUEUE == "automation-tq"


# ---------------------------------------------------------------------------
# AST-level tests - inspect the ``Worker(...)`` call site without
# instantiating Temporal so the assertions hold even on hosts that
# do not ship the ``temporalio`` SDK.
# ---------------------------------------------------------------------------


def _parse_main_module() -> ast.Module:
    """Return the parsed AST of ``automation_worker.main``."""

    main_path = _SRC_DIR / "automation_worker" / "main.py"
    return ast.parse(main_path.read_text(encoding="utf-8"))


def _find_worker_calls(tree: ast.Module) -> list[ast.Call]:
    """Return every ``Worker(...)`` call in the module's AST."""

    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Worker"
        ):
            calls.append(node)
    return calls


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    """Return the value AST node for keyword *name* on *call* (or None)."""

    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


class TestWorkerConstructorCallSite:
    """The ``Worker(...)`` call must use ``task_queue_for("AutomationWorkflow")``
    rather than a literal queue string."""

    def test_exactly_one_worker_constructor_call(self) -> None:
        """The boot script constructs exactly one Worker."""

        worker_calls = _find_worker_calls(_parse_main_module())
        assert len(worker_calls) == 1, (
            "automation-worker boot script must construct exactly one "
            "Worker (one queue per worker); found "
            f"{len(worker_calls)}."
        )

    def test_task_queue_kwarg_is_present(self) -> None:
        """The Worker call has a task_queue kwarg."""

        (call,) = _find_worker_calls(_parse_main_module())
        assert _kwarg(call, "task_queue") is not None

    def test_task_queue_kwarg_resolves_via_registry(self) -> None:
        """The ``task_queue=`` keyword must resolve through
        ``task_queue_for("AutomationWorkflow")`` - either directly
        or via a module-level constant whose RHS is the same call.
        """

        tree = _parse_main_module()
        (call,) = _find_worker_calls(tree)
        value = _kwarg(call, "task_queue")
        assert value is not None

        # Accept the direct call (``task_queue=task_queue_for("...")``)
        # or the module-level constant pattern
        # (``AUTOMATION_TASK_QUEUE = task_queue_for("AutomationWorkflow")``
        # ``... task_queue=AUTOMATION_TASK_QUEUE``).
        if isinstance(value, ast.Call):
            self._assert_task_queue_for_call(value, "AutomationWorkflow")
        elif isinstance(value, ast.Name):
            self._assert_constant_resolves_to_task_queue_for(
                tree, value.id, "AutomationWorkflow"
            )
        else:  # pragma: no cover - defensive
            pytest.fail(
                "task_queue= kwarg must be either a direct call to "
                "task_queue_for(...) or a module-level constant; "
                f"got {ast.dump(value)}"
            )

    @staticmethod
    def _assert_task_queue_for_call(
        call: ast.Call, expected_workflow_name: str
    ) -> None:
        """Assert *call* is ``task_queue_for("<expected_workflow_name>")``."""

        assert isinstance(call.func, ast.Name) and call.func.id == "task_queue_for", (
            "task_queue= must be derived from task_queue_for(...); "
            f"got {ast.dump(call)}"
        )
        assert len(call.args) == 1
        arg = call.args[0]
        assert isinstance(arg, ast.Constant) and arg.value == expected_workflow_name

    @staticmethod
    def _assert_constant_resolves_to_task_queue_for(
        tree: ast.Module, name: str, expected_workflow_name: str
    ) -> None:
        """Walk the module AST and verify the named constant is bound to
        ``task_queue_for("<expected_workflow_name>")``."""

        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign):
                target = node.target
                if isinstance(target, ast.Name) and target.id == name and node.value is not None:
                    assert isinstance(node.value, ast.Call)
                    TestWorkerConstructorCallSite._assert_task_queue_for_call(
                        node.value, expected_workflow_name
                    )
                    return
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        assert isinstance(node.value, ast.Call)
                        TestWorkerConstructorCallSite._assert_task_queue_for_call(
                            node.value, expected_workflow_name
                        )
                        return
        pytest.fail(
            f"module-level constant {name!r} not found in main.py; "
            "expected ``<NAME>: str = task_queue_for(\"AutomationWorkflow\")``"
        )

    def test_no_hardcoded_queue_string_in_worker_call(self) -> None:
        """The ``task_queue=`` kwarg must not be a string literal - that
        would bypass the registry and break the single-source-of-truth
        invariant.
        """

        (call,) = _find_worker_calls(_parse_main_module())
        value = _kwarg(call, "task_queue")
        assert not isinstance(value, ast.Constant), (
            "task_queue= must not be a hardcoded string literal - use "
            "task_queue_for(...) from temporal_shared.workflow_registry"
        )


class TestWorkerWorkflowsRegistration:
    """The ``Worker(workflows=[...])`` list must register the
    canonical workflow set for the automation-tq queue.

    ``AutomationWorkflow`` and ``BotBranchRetention`` ride this queue
    alongside the pre-existing ``AuditPruneWorkflow``. This test guards
    against a refactor that accidentally drops one of them when wiring
    the others.
    """

    def test_workflows_kwarg_registers_three_workflows(self) -> None:
        """The workflows kwarg registers the expected workflow set."""

        (call,) = _find_worker_calls(_parse_main_module())
        value = _kwarg(call, "workflows")
        assert isinstance(value, ast.List)
        names = {
            elt.id for elt in value.elts if isinstance(elt, ast.Name)
        }
        assert {"AutomationWorkflow", "BotBranchRetention", "AuditPruneWorkflow"} <= names, (
            "automation-worker must register AutomationWorkflow, "
            "BotBranchRetention, and AuditPruneWorkflow; got "
            f"{sorted(names)}"
        )
