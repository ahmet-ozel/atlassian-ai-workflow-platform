"""Workflow modules for the execution-runner-worker.

Exports:

* :class:`ExecutionRunWorkflow` — the canonical workflow. Accepts
  :class:`ExecutionRunWorkflowInput` and returns
  :class:`ExecutionRunWorkflowOutput` from
  :mod:`temporal_shared.messages`.
* :class:`LegacyExecutionRunWorkflow` — the legacy workflow preserved under a
  Temporal-distinct name so the integration test
  ``tests/integration/test_execution_runner.py`` keeps green.  Imports
  alias ``ExecutionRunInput`` / ``ExecutionRunResult`` for the legacy
  class still resolve through this module.
* :class:`SSHHealthcheckCronWorkflow` — Temporal cron workflow for
  proactive SSH runner monitoring.
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
    # canonical
    "ExecutionRunWorkflow",
    "DEFAULT_HEARTBEAT",
    "DEFAULT_START_TO_CLOSE",
    # legacy
    "LegacyExecutionRunWorkflow",
    "ExecutionRunInput",
    "ExecutionRunResult",
    # SSH healthcheck cron
    "SSHHealthcheckCronWorkflow",
    "HealthcheckState",
]
