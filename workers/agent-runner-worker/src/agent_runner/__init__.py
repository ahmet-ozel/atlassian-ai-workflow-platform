"""``agent_runner`` package - new layout for the AgentRunnerWorkflow.

This package mirrors the ``automation_worker`` layout as the canonical
home of the Temporal worker code hosting
:class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
on the ``agent-runner-tq`` task queue.

This package re-exports the existing implementation that historically
lived under :mod:`src.workflows`. The legacy module remains importable
for backwards-compatible test fixtures.
"""
