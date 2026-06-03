"""``execution_runner`` package — canonical home for the execution-runner-worker.

The boot script (:mod:`execution_runner.main`) registers the canonical
:class:`src.workflows.execution_run_workflow.ExecutionRunWorkflow` and
its SSH/Vault/MinIO activities on the ``execution-runner-tq`` task
queue.  The legacy entrypoint at ``src/main.py`` remains importable
during the migration window so existing Compose / Dockerfile commands
keep working; new code should target this package.
"""
