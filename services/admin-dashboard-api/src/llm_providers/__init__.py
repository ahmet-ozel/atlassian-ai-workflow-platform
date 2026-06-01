"""LLM provider management feature package.

This package realises the ``llm-provider-management`` spec in
``.kiro/specs/llm-provider-management``: a FastAPI router under
``/admin/llm-providers`` (sibling at
``/admin/departments/{dept_id}/llm-provider``) backed by an asyncpg
repository pair, a Vault KV-v2 credential store and a per-provider
:class:`~llm_providers.connection_tester.ConnectionTester` that
validates upstream connectivity with a hard 10s budget and a fixed
5-token cap.

Cross-cutting concerns — admin auth, log redaction, audit logging — are
reused from existing infrastructure rather than reimplemented; see the
design document's "Audit & redaction wiring" section for the exact
hand-off points.
"""

from __future__ import annotations
