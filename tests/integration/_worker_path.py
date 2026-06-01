"""Helper for resolving the right worker's ``src.*`` namespace per test.

Both ``platform/workers/agent-runner-worker`` and
``platform/workers/execution-runner-worker`` ship their code under the
``src.`` Python package name. When several integration tests run in the
same pytest session, the first ``import src.workflows.*`` wins and
subsequent ``import src.*`` calls get the *wrong* subtree (Python caches
the package in ``sys.modules`` after the first resolution).

The :func:`isolate_worker` context manager guards every integration
test that needs a specific worker's ``src.*`` package. It snapshots
``sys.path`` and the loaded ``src.*`` modules at entry, swaps them for
the requested worker, then restores the snapshot on exit. This keeps
each test hermetic regardless of run order and avoids leaking state
into pre-existing tests (notably the ``test_temporal_*.py`` family,
which assumes the agent-runner worker is the only ``src.*`` provider
on ``sys.path``).

Usage::

    from tests.integration._worker_path import isolate_worker

    @pytest.mark.asyncio
    async def test_something() -> None:
        with isolate_worker("agent-runner"):
            from src.workflows.automation_workflow import ...
            # exercise the workflow inside the with-block.

The context manager is a thin pure helper; it performs no I/O beyond
the ``sys.path`` / ``sys.modules`` book-keeping.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator

_PLATFORM_ROOT: str = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

_AGENT_RUNNER_DIR: str = os.path.join(
    _PLATFORM_ROOT, "workers", "agent-runner-worker"
)
_EXECUTION_RUNNER_DIR: str = os.path.join(
    _PLATFORM_ROOT, "workers", "execution-runner-worker"
)

_KNOWN_WORKERS: dict[str, str] = {
    "agent-runner": _AGENT_RUNNER_DIR,
    "execution-runner": _EXECUTION_RUNNER_DIR,
}


@contextlib.contextmanager
def isolate_worker(worker_name: str) -> Iterator[None]:
    """Context manager that pins ``sys.path`` to a specific worker.

    Parameters
    ----------
    worker_name:
        One of ``"agent-runner"`` or ``"execution-runner"``.

    Behaviour
    ---------
    On enter:
        1. Snapshot ``sys.path`` and the set of loaded ``src.*`` modules.
        2. Drop every previously-cached ``src.*`` from ``sys.modules``.
        3. Strip *other* workers' directories from ``sys.path``.
        4. Ensure the requested worker's directory is at index 0.

    On exit:
        Restore both ``sys.path`` and the ``src.*`` ``sys.modules``
        entries to the pre-entry snapshot. Tests that ran before the
        context manager continue to see their original namespace.
    """

    if worker_name not in _KNOWN_WORKERS:
        raise ValueError(
            f"Unknown worker {worker_name!r}; "
            f"expected one of {sorted(_KNOWN_WORKERS)}"
        )

    target_dir = _KNOWN_WORKERS[worker_name]
    other_dirs = {d for k, d in _KNOWN_WORKERS.items() if k != worker_name}

    # ----- Snapshot ----------------------------------------------------
    saved_path = list(sys.path)
    saved_src_modules = {
        m: sys.modules[m]
        for m in list(sys.modules)
        if m == "src" or m.startswith("src.")
    }

    # ----- Apply: strip ``src.*`` cache, rearrange path ----------------
    for modname in saved_src_modules:
        sys.modules.pop(modname, None)

    sys.path[:] = [p for p in saved_path if p not in other_dirs and p != target_dir]
    sys.path.insert(0, target_dir)

    try:
        yield
    finally:
        # ----- Restore -------------------------------------------------
        # Drop anything ``src.*`` loaded inside the block before
        # restoring the original cache. Mixing the two would leave
        # broken references (e.g. dataclasses imported from the
        # in-block tree co-existing with the original tree's classes).
        for modname in [
            m for m in list(sys.modules) if m == "src" or m.startswith("src.")
        ]:
            sys.modules.pop(modname, None)
        sys.modules.update(saved_src_modules)
        sys.path[:] = saved_path
