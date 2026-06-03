"""agent-runner-worker package.

Temporal worker that hosts ``AgentRunnerWorkflow`` and the supporting
Jira / Bitbucket / Confluence / LLM / artifact / opencode activities.
The worker subscribes to the ``agent-runner`` task queue and connects
to the Temporal cluster identified by the ``TEMPORAL_HOST`` environment
variable (default ``temporal:7233``).

This is the package root; concrete workflow and activity implementations are
filled in by subsequent tasks. See the package README for the
"Standalone build & run" instructions.
"""
