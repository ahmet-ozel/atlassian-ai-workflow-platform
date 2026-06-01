"""Integration smoke — cascade healthcheck (`platform-mimari-ops` task 16.5).

Drives ``/admin/healthcheck/aggregate`` and asserts the response
shape carries ``services`` (mapping of name → status) and
``transitions`` (list).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("ADMIN_API_BASE_URL"),
    reason="ADMIN_API_BASE_URL not set",
)
def test_healthcheck_aggregate_shape(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    import httpx

    base = os.environ["ADMIN_API_BASE_URL"].rstrip("/")
    headers = {}
    token = os.environ.get("ADMIN_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    r = httpx.get(
        f"{base}/admin/healthcheck/aggregate", headers=headers, timeout=15.0
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "services" in body and isinstance(body["services"], dict)
    assert "transitions" in body and isinstance(body["transitions"], list)
    # Every status value belongs to the canonical alphabet.
    for name, status in body["services"].items():
        assert status in {"healthy", "unhealthy", "unknown", "degraded"}, (
            f"unexpected status for {name}: {status!r}"
        )
