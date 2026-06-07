"""Top-level package for the ``automation-worker`` Temporal worker.

Hosts workflows and activities that run on the ``automation-tq`` task
queue. The package is intentionally minimal at import time - only the
public dataclasses and workflow classes are re-exported; activity
modules (which carry network-side imports) are imported lazily inside
the worker boot script under
``temporalio.workflow.unsafe.imports_passed_through()`` so the workflow
modules stay sandbox-clean.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
