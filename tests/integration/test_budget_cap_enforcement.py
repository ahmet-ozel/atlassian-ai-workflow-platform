"""Integration smoke — budget cap enforcement (ops work).

Exercises the workflow start endpoint with a synthetic dept whose
``budget_caps.weekly_usd_dept`` is below the running cost; the
expected response is HTTP 429 with ``scope`` set in the body.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    not os.environ.get("AUTOMATION_BASE_URL"),
    reason="AUTOMATION_BASE_URL not set",
)
def test_budget_cap_returns_429_on_over_cap(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--run-docker"):
        pytest.skip("requires --run-docker")

    import httpx

    base = os.environ["AUTOMATION_BASE_URL"].rstrip("/")
    dept_id = os.environ.get("INTEGRATION_OVERCAP_DEPT_ID", "")
    if not dept_id:
        pytest.skip(
            "Set INTEGRATION_OVERCAP_DEPT_ID to a dept with a known "
            "weekly_usd cap below current spend to exercise the 429 path."
        )

    r = httpx.post(
        f"{base}/workflows/start",
        json={
            "dept_id": dept_id,
            "workflow_type": "code_change_with_test",
            "summary": "integration smoke",
        },
        timeout=15.0,
    )
    assert r.status_code == 429, (
        f"expected 429 for over-cap dept; got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body.get("error") == "budget_exceeded"
    assert body.get("scope") in {
        "dept_weekly",
        "user_weekly",
        "dept_monthly",
        "user_monthly",
    }
