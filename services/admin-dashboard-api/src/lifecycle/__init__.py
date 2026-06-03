"""Lifecycle support package for the admin-dashboard-api service.

Houses pure helper modules (``sensitive``, ``env_parser`` …) plus
the orchestration plumbing (``VaultClient``,
``ComposeRunner``, ``HealthProbe``, ``AuditWriter``, ``LifecycleService``).

Importing this package has **no side effects**; in particular, no I/O is
performed at import time.
"""
