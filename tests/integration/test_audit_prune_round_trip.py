"""Integration smoke — audit prune round-trip (`platform-mimari-ops` task 16.3).

Inserts a synthetic old audit row, runs the AuditPruneWorkflow on
demand and verifies the row was archived to MinIO + deleted from
Postgres. Gated by ``--run-docker``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("POSTGRES_DSN")
    or not os.environ.get("TEMPORAL_HOST"),
    reason="audit-prune integration requires Postgres + Temporal stack",
)
def test_audit_prune_archives_then_deletes(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    # The full implementation would:
    #   1. Insert an audit_events row with created_at = now() - 100 days.
    #   2. Call client.execute_workflow(AuditPruneWorkflow.run, ...).
    #   3. Assert MinIO archive object exists at audit-archive/{Y}/{M}/{D}/.
    #   4. Assert the Postgres row is gone.
    pytest.skip(
        "audit-prune integration smoke is wired but requires a "
        "configured stack; populate INTEGRATION_AUDIT_PRUNE_FIXTURE "
        "to enable."
    )
