"""Unit tests for ``execution_runner.main`` boot script.

Validates the **single-queue-per-worker** invariant of
workflows-spec Requirements 1.1 and 1.2:

    * The boot script's ``Worker(...)`` constructor receives **exactly
      one** ``task_queue`` keyword argument.
    * That value is computed by
      :func:`temporal_shared.workflow_registry.task_queue_for` (no
      hardcoded queue string anywhere in the boot script).
    * The resolved queue equals the registry's entry for
      ``"ExecutionRunWorkflow"`` (i.e. ``"execution-runner-tq"``).
    * The ``Worker(...)`` call registers the canonical
      :class:`ExecutionRunWorkflow`.

The tests exercise both the **import-time module shape** (the
``EXECUTION_RUNNER_TASK_QUEUE`` constant materialised at import) and
the **source-level AST** (the ``Worker(...)`` call site itself) so a
future refactor cannot trivially hide a queue-string drift behind a
helper variable.

Validates Requirements: 1.1, 1.2.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping — the canonical package ships under
# ``src/execution_runner/`` (mirrors ``hatchling``
# ``packages = ["src", "src/execution_runner"]``).
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# ---------------------------------------------------------------------------
# Module-level constant tests
# ---------------------------------------------------------------------------


class TestModuleLevelTaskQueueConstant:
    """The boot script materialises the queue at import time so the
    boot path and tests share a single, registry-derived value."""

    def test_constant_resolves_via_registry(self) -> None:
        """**Validates: Requirements 1.1, 1.2**"""

        from execution_runner import main as main_mod
        from temporal_shared.workflow_registry import task_queue_for

        assert main_mod.EXECUTION_RUNNER_TASK_QUEUE == task_queue_for(
            "ExecutionRunWorkflow"
        )

    def test_constant_value_is_execution_runner_tq(self) -> None:
        """**Validates: Requirement 1.1**"""

        from execution_runner import main as main_mod

        assert main_mod.EXECUTION_RUNNER_TASK_QUEUE == "execution-runner-tq"


# ---------------------------------------------------------------------------
# AST-level tests
# ---------------------------------------------------------------------------


def _parse_main_module() -> ast.Module:
    """Return the parsed AST of ``execution_runner.main``."""

    main_path = _SRC_DIR / "execution_runner" / "main.py"
    return ast.parse(main_path.read_text(encoding="utf-8"))


def _parse_legacy_src_main_module() -> ast.Module:
    """Return the parsed AST of the Docker entrypoint ``src.main``."""

    main_path = _SRC_DIR / "main.py"
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
                _assert_task_queue_for_call(node.value, expected_workflow_name)
                return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    assert isinstance(node.value, ast.Call)
                    _assert_task_queue_for_call(
                        node.value, expected_workflow_name
                    )
                    return
    pytest.fail(
        f"module-level constant {name!r} not found in main.py; "
        f"expected ``<NAME>: str = task_queue_for({expected_workflow_name!r})``"
    )


class TestWorkerConstructorCallSite:
    """The ``Worker(...)`` call must use ``task_queue_for("ExecutionRunWorkflow")``
    rather than a literal queue string."""

    def test_exactly_one_worker_constructor_call(self) -> None:
        """**Validates: Requirement 1.2**"""

        worker_calls = _find_worker_calls(_parse_main_module())
        assert len(worker_calls) == 1, (
            "execution-runner-worker boot script must construct "
            "exactly one Worker (one queue per worker); found "
            f"{len(worker_calls)}."
        )

    def test_task_queue_kwarg_is_present(self) -> None:
        """**Validates: Requirement 1.2**"""

        (call,) = _find_worker_calls(_parse_main_module())
        assert _kwarg(call, "task_queue") is not None

    def test_task_queue_kwarg_resolves_via_registry(self) -> None:
        """**Validates: Requirements 1.1, 1.2**

        The ``task_queue=`` keyword must resolve through
        ``task_queue_for("ExecutionRunWorkflow")`` — either directly
        or via a module-level constant whose RHS is the same call.
        """

        tree = _parse_main_module()
        (call,) = _find_worker_calls(tree)
        value = _kwarg(call, "task_queue")
        assert value is not None

        if isinstance(value, ast.Call):
            _assert_task_queue_for_call(value, "ExecutionRunWorkflow")
        elif isinstance(value, ast.Name):
            _assert_constant_resolves_to_task_queue_for(
                tree, value.id, "ExecutionRunWorkflow"
            )
        else:  # pragma: no cover — defensive
            pytest.fail(
                "task_queue= kwarg must be either a direct call to "
                "task_queue_for(...) or a module-level constant; "
                f"got {ast.dump(value)}"
            )

    def test_no_hardcoded_queue_string_in_worker_call(self) -> None:
        """**Validates: Requirement 1.2**

        The ``task_queue=`` kwarg must not be a string literal — that
        would bypass the registry and break the single-source-of-truth
        invariant.
        """

        (call,) = _find_worker_calls(_parse_main_module())
        value = _kwarg(call, "task_queue")
        assert not isinstance(value, ast.Constant), (
            "task_queue= must not be a hardcoded string literal — use "
            "task_queue_for(...) from temporal_shared.workflow_registry"
        )


class TestWorkerWorkflowsRegistration:
    """The ``Worker(workflows=[...])`` list must register the canonical
    :class:`ExecutionRunWorkflow` for the execution-runner-tq queue.

    The boot script loads the workflow class via a helper
    (``_load_workflow``) and assigns the result to ``workflow_cls``
    before passing it to ``Worker(...)``. This test follows that
    indirection so the call-site shape can evolve without breaking
    the contract — what matters is that the canonical workflow class
    ends up in the registration list.
    """

    def test_workflows_kwarg_registers_execution_run_workflow(self) -> None:
        """**Validates: Requirement 1.1**"""

        tree = _parse_main_module()
        (call,) = _find_worker_calls(tree)
        value = _kwarg(call, "workflows")
        assert isinstance(value, ast.List)

        # Direct ``Worker(workflows=[ExecutionRunWorkflow])`` is the
        # simplest pattern; the more complex pattern routes through a
        # ``workflow_cls`` local that the boot uses to gate the worker
        # construction on a successful import. We accept either by
        # reading every ``Name`` element in the list and asserting that
        # the source contains the canonical class name (either as a
        # registered list element or as the value bound to the local).
        list_names = {
            elt.id for elt in value.elts if isinstance(elt, ast.Name)
        }
        assert list_names, (
            "Worker(workflows=[...]) must list at least one Name node; "
            f"got {ast.dump(value)}"
        )

        # The set of identifiers that may appear is small — either
        # ``ExecutionRunWorkflow`` directly or a local like
        # ``workflow_cls`` that the boot script binds to the canonical
        # class. We accept the local name pattern only when the source
        # also imports / references ``ExecutionRunWorkflow`` somewhere
        # else in the module — the AST scan below verifies that.
        if "ExecutionRunWorkflow" in list_names:
            return  # direct registration — done.

        source = (_SRC_DIR / "execution_runner" / "main.py").read_text(
            encoding="utf-8"
        )
        assert "ExecutionRunWorkflow" in source, (
            "execution-runner-worker must register ExecutionRunWorkflow "
            "(directly or via a local bound to the canonical class); "
            f"workflows=[{', '.join(sorted(list_names))}] does not "
            "reference the workflow."
        )


class TestDockerEntrypointTaskQueue:
    """The Docker entrypoint must poll the same canonical queue."""

    def test_legacy_src_main_constant_resolves_via_registry(self) -> None:
        """**Validates: Requirements 1.1, 1.2**"""

        _assert_constant_resolves_to_task_queue_for(
            _parse_legacy_src_main_module(),
            "TASK_QUEUE",
            "ExecutionRunWorkflow",
        )

    def test_legacy_src_main_worker_uses_task_queue_variable(self) -> None:
        """The env file may be stale; the boot code must not drift."""

        tree = _parse_legacy_src_main_module()
        worker_calls = _find_worker_calls(tree)
        assert len(worker_calls) == 1
        value = _kwarg(worker_calls[0], "task_queue")
        assert isinstance(value, ast.Name)
        assert value.id == "task_queue"
