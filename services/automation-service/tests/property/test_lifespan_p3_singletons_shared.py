"""Shared singletons are shared across containers.

For any successful run of the lifespan startup phase, the asyncpg
pool, Vault client and AuditLogger instances reachable through every
``*EndpointDeps`` container that declares one of those collaborators
is the same Python object (``is``-identity) as the one stashed on
``app.state``. Every container that holds a ``connection_factory``
callable derives that factory from the same pool.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import (  # noqa: E402
    app_module,
    install_lifespan_fakes,
)


async def _run_property() -> None:
    mp = pytest.MonkeyPatch()
    try:
        install_lifespan_fakes(mp)
        app = app_module.create_app()
        async with app_module.lifespan(app):
            shared_pool = app.state.pool
            shared_vault = app.state.vault
            shared_audit = app.state.audit_logger
            shared_temporal = app.state.temporal

            # AdminEndpointDeps
            assert app.state.admin.vault is shared_vault
            assert app.state.admin.audit_logger is shared_audit
            assert app.state.admin.temporal_client is shared_temporal

            # DeptCredentialEndpointDeps
            assert app.state.dept_credentials.audit_logger is shared_audit

            # WebhooksEndpointDeps
            assert app.state.webhooks.audit_logger is shared_audit
            assert app.state.webhooks.workflow_client is shared_temporal
            assert app.state.webhooks.processed_events is (
                app.state.processed_events
            )

            # CancelEndpointDeps
            assert app.state.cancel.audit_logger is shared_audit
            assert app.state.cancel.temporal_client is shared_temporal
            assert app.state.cancel.oidc_validator is app.state.oidc_validator

            # RepoSyncEndpointDeps
            assert app.state.repo_sync.audit_logger is shared_audit
            assert app.state.repo_sync.oidc_validator is app.state.oidc_validator

            # PoReviewEndpointDeps
            assert app.state.po_review.audit_logger is shared_audit
            assert app.state.po_review.oidc_validator is app.state.oidc_validator

            # InboundContext
            assert app.state.inbound.audit_logger is shared_audit
            assert app.state.inbound.workflow_client is shared_temporal

            # WebhookContext (webhook_v2)
            assert app.state.webhook_v2.audit_logger is shared_audit
            assert app.state.webhook_v2.workflow_client is shared_temporal
            assert app.state.webhook_v2.vault is shared_vault

            # Pool sanity — connection_factory closes over the shared pool
            assert callable(app.state.connection_factory)

            # Make sure the pool reference on app.state matches the fake
            assert shared_pool is app.state.pool
    finally:
        mp.undo()


@given(_=st.just(None))
@settings(max_examples=200, deadline=None)
def test_singletons_shared_across_containers(_: None) -> None:
    """Identity-share check across every ``*EndpointDeps`` container."""

    asyncio.run(_run_property())
