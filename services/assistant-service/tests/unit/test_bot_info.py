"""Unit tests for the bot-info endpoint (R7.6, Task 7.1).

Tests the ``GET /api/dept/{id}/bot-info`` endpoint with a fake database
pool to verify:
  - Correct response shape when department exists with bots.
  - 404 when department does not exist.
  - Empty bots list when department has no bot registrations.
  - Probe status from capability_probes table is joined correctly.
  - 503 when database pool is not wired.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fake database pool
# ---------------------------------------------------------------------------


@dataclass
class FakeDbPool:
    """In-memory fake that mimics asyncpg pool's fetchrow/fetch interface."""

    departments: dict[str, dict[str, Any]] = field(default_factory=dict)
    department_bots: list[dict[str, Any]] = field(default_factory=list)
    capability_probes: list[dict[str, Any]] = field(default_factory=list)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Simulate SELECT from automation.departments."""
        if "automation.departments" in query:
            dept_id = args[0]
            return self.departments.get(dept_id)
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Simulate the bots + probes LEFT JOIN query."""
        if "department_bots" in query:
            dept_id = args[0]
            results = []
            for bot in self.department_bots:
                if bot["department_id"] != dept_id:
                    continue
                # Find matching probe
                probe = next(
                    (
                        p
                        for p in self.capability_probes
                        if p["dept_id"] == dept_id and p["service"] == bot["service"]
                    ),
                    None,
                )
                results.append(
                    {
                        "service": bot["service"],
                        "username": bot["username"],
                        "account_id": bot["account_id"],
                        "probe_status": probe["status"] if probe else "not_probed",
                        "probed_at": probe["probed_at"] if probe else None,
                    }
                )
            return results
        return []


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(db: FakeDbPool | None = None):
    """Build a minimal FastAPI app with the bot-info router mounted."""
    from src.bot_info import BotInfoDeps, router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)

    if db is not None:
        app.state.bot_info_deps = BotInfoDeps(db=db)
    else:
        app.state.bot_info_deps = None

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBotInfoEndpoint:
    """Tests for GET /api/dept/{id}/bot-info."""

    def test_returns_bot_info_for_existing_department(self):
        """Happy path: department exists with bots and probe results."""
        probed_at = datetime(2024, 1, 15, 10, 30, 0, tzinfo=timezone.utc)
        db = FakeDbPool(
            departments={
                "payment-team": {"display_name": "Payment Team"},
            },
            department_bots=[
                {
                    "department_id": "payment-team",
                    "service": "jira",
                    "username": "payment-ai-bot",
                    "account_id": "5fc9e78dabcd1234",
                },
                {
                    "department_id": "payment-team",
                    "service": "bitbucket",
                    "username": "payment-ai-bot",
                    "account_id": "abc123def456",
                },
            ],
            capability_probes=[
                {
                    "dept_id": "payment-team",
                    "service": "jira",
                    "status": "ok",
                    "probed_at": probed_at,
                },
                {
                    "dept_id": "payment-team",
                    "service": "bitbucket",
                    "status": "error",
                    "probed_at": probed_at,
                },
            ],
        )

        app = _build_app(db)
        client = TestClient(app)
        resp = client.get("/api/dept/payment-team/bot-info")

        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Payment Team"
        assert len(body["bots"]) == 2

        jira_bot = body["bots"][0]
        assert jira_bot["service"] == "jira"
        assert jira_bot["username"] == "payment-ai-bot"
        assert jira_bot["account_id"] == "5fc9e78dabcd1234"
        assert jira_bot["probe_status"] == "ok"
        assert jira_bot["probed_at"] is not None

        bb_bot = body["bots"][1]
        assert bb_bot["service"] == "bitbucket"
        assert bb_bot["probe_status"] == "error"

    def test_returns_404_for_nonexistent_department(self):
        """Department not found → 404."""
        db = FakeDbPool(departments={})

        app = _build_app(db)
        client = TestClient(app)
        resp = client.get("/api/dept/nonexistent/bot-info")

        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"]

    def test_returns_empty_bots_when_no_registrations(self):
        """Department exists but has no bot registrations."""
        db = FakeDbPool(
            departments={
                "empty-dept": {"display_name": "Empty Department"},
            },
            department_bots=[],
        )

        app = _build_app(db)
        client = TestClient(app)
        resp = client.get("/api/dept/empty-dept/bot-info")

        assert resp.status_code == 200
        body = resp.json()
        assert body["display_name"] == "Empty Department"
        assert body["bots"] == []

    def test_returns_not_probed_when_no_capability_probe(self):
        """Bot exists but no probe has been run → probe_status='not_probed'."""
        db = FakeDbPool(
            departments={
                "new-dept": {"display_name": "New Department"},
            },
            department_bots=[
                {
                    "department_id": "new-dept",
                    "service": "confluence",
                    "username": "wiki-bot",
                    "account_id": None,
                },
            ],
            capability_probes=[],
        )

        app = _build_app(db)
        client = TestClient(app)
        resp = client.get("/api/dept/new-dept/bot-info")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["bots"]) == 1
        assert body["bots"][0]["probe_status"] == "not_probed"
        assert body["bots"][0]["probed_at"] is None
        assert body["bots"][0]["account_id"] is None

    def test_returns_503_when_deps_not_wired(self):
        """Database pool not available → 503."""
        app = _build_app(db=None)
        client = TestClient(app)
        resp = client.get("/api/dept/any/bot-info")

        assert resp.status_code == 503
        assert "not wired" in resp.json()["detail"]
