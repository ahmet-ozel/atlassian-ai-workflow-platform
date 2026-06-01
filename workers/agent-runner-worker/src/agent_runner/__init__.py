"""``agent_runner`` package — new layout for the AgentRunnerWorkflow.

This package mirrors the ``automation_worker`` layout (single-package
under ``src/``) introduced by the ``platform-mimari-workflows`` spec
(task 2.1) and is the canonical home of the Temporal worker code
hosting :class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
on the ``agent-runner-tq`` task queue.

Until task 2.5 (`Worker boot script'leri — tek queue per worker`)
relocates ``main.py`` and tasks 7-10 land their per-workflow-type
bodies, this package re-exports the existing implementation that
historically lived under :mod:`src.workflows`. The legacy module
remains importable for backwards-compatible test fixtures (see
``platform/tests/integration/_e2e_workflow_stubs.py``).
"""
