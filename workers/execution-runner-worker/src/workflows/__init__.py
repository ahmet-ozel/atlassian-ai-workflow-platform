"""Workflow modules for the execution-runner-worker.

Exports:

* :class:`ExecutionRunWorkflow` — the canonical workflow defined by
  ``platform-mimari-workflows`` task 2.3 / Requirements 1.1, 1.6.
  Accepts :class:`ExecutionRunWorkflowInput` and returns
  :class:`ExecutionRunWorkflowOutput` from
  :mod:`temporal_shared.messages`.
* :class:`LegacyExecutionRunWorkflow` — the foundation-spec scaffold
  preserved (under a Temporal-distinct name) so the integration test
  ``tests/integration/test_execution_runner.py`` keeps green.  Imports
  alias ``ExecutionRunInput`` / ``ExecutionRunResult`` for the legacy
  class still resolve through this module.
* :class:`SSHHealthcheckCronWorkflow` — Temporal cron workflow for
  proactive SSH runner monitoring (task 20.1, Requirements 14.1-14.5).
"""

from .execution_run_workflow import (
    DEFAULT_HEARTBEAT,
    DEFAULT_START_TO_CLOSE,
    ExecutionRunInput,
    ExecutionRunResult,
    ExecutionRunWorkflow,
    LegacyExecutionRunWorkflow,
)
from .ssh_healthcheck_cron import (
    HealthcheckState,
    SSHHealthcheckCronWorkflow,
)

__all__ = [
    # canonical (task 2.3 — Requirements 1.1, 1.6)
    "ExecutionRunWorkflow",
    "DEFAULT_HEARTBEAT",
    "DEFAULT_START_TO_CLOSE",
    # legacy (foundation scaffold)
    "LegacyExecutionRunWorkflow",
    "ExecutionRunInput",
    "ExecutionRunResult",
    # SSH healthcheck cron (task 20.1 — Requirements 14.1-14.5)
    "SSHHealthcheckCronWorkflow",
    "HealthcheckState",
]
