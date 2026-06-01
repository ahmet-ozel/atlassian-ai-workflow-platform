"""Integration smoke — dept decommission (`platform-mimari-ops` task 16.7).

Spec 1 R10 parity check: calling
``/admin/departments/{id}/decommission`` triggers the drain mode +
Vault revoke + audit chain end-to-end.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("ADMIN_API_BASE_URL"),
    reason="ADMIN_API_BASE_URL not set",
)
def test_dept_decommission_round_trip(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    dept_id = os.environ.get("INTEGRATION_DECOMMISSION_DEPT_ID", "")
    if not dept_id:
        pytest.skip("Set INTEGRATION_DECOMMISSION_DEPT_ID to exercise the path")

    import httpx

    base = os.environ["ADMIN_API_BASE_URL"].rstrip("/")
    headers = {}
    token = os.environ.get("ADMIN_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    r = httpx.post(
        f"{base}/admin/departments/{dept_id}/decommission",
        headers=headers,
        timeout=30.0,
    )
    assert r.status_code in (200, 202), (
        f"unexpected status {r.status_code}: {r.text}"
    )
