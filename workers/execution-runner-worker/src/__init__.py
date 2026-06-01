"""execution-runner-worker package.

Temporal worker that hosts `ExecutionRunWorkflow` and the supporting
SSH / Docker / Vault / MinIO activities. The worker subscribes to the
``execution-runner`` task queue and connects to the Temporal cluster
identified by the ``TEMPORAL_HOST`` environment variable
(default ``temporal:7233``).

This is a scaffold; concrete workflow and activity implementations are
filled in by subsequent tasks. See the package README for the
"Standalone build & run" instructions.
"""
