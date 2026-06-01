"""Unit tests for ``src.routers.loki_search`` (platform-gap-fill task 7.3).

**Validates: Requirements 8.6**

Two surfaces are exercised:

1. ``GET /admin/audit/search?trace_id=...`` — the existing audit search
   proxy gains a ``trace_id`` filter that is forwarded to Loki when the
   client supports it and re-applied client-side as a defence-in-depth
   safety net.

2. ``GET /api/v1/workflows/{workflow_id}/logs?trace_id=...`` — the new
   workflow-scoped log filter that builds a LogQL stream selector and
   forwards to ``LokiClient``. The endpoint is the backend half of
   task 7.3; the FE integration is tracked separately.

The tests inject a tiny in-memory Loki stub so we can verify the
LogQL query, the soft-fail path (no client wired), the
graceful-degradation path (client raises), and the defensive
client-side filter that drops rows from a different workflow or
request.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern used by the other unit tests).
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared", "observability"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.loki_search import (  # noqa: E402
    _build_logql,
    _flatten_loki_streams,
    router as audit_router,
    workflow_logs_router,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeLokiClientWithTraceId:
    """Loki stub that accepts the new ``trace_id`` kwarg."""

    def __init__(self, *, hits: list[dict[str, Any]] | None = None) -> None:
        self.hits = hits or []
        self.search_calls: list[dict[str, Any]] = []
        self.query_range_calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.search_calls.append(kwargs)
        return list(self.hits)


class _LegacyLokiClient:
    """Loki stub that mimics the older signature (no ``trace_id`` kwarg).

    Calling ``search(trace_id=...)`` raises ``TypeError`` so the router's
    fallback retry path is exercised.
    """

    def __init__(self, *, hits: list[dict[str, Any]] | None = None) -> None:
        self.hits = hits or []
        self.search_calls: list[dict[str, Any]] = []

    async def search(
        self,
        *,
        actor_id: str | None = None,
        dept_id: str | None = None,
        action: str | None = None,
        client_source: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "actor_id": actor_id,
                "dept_id": dept_id,
                "action": action,
                "client_source": client_source,
                "start": start,
                "end": end,
            }
        )
        return list(self.hits)


class _LokiClientWithQueryRange:
    """Loki stub exposing the canonical ``query_range`` HTTP surface."""

    def __init__(self, *, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def query_range(
        self,
        *,
        query: str,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
    ) -> Any:
        self.calls.append(
            {"query": query, "start": start, "end": end, "limit": limit}
        )
        return self.response


class _ExplodingLokiClient:
    """Loki stub that always raises — exercises the soft-fail branch."""

    async def query_range(self, **kwargs: Any) -> Any:
        raise RuntimeError("loki down")

    async def search(self, **kwargs: Any) -> Any:
        raise RuntimeError("loki down")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_audit_app(*, loki: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(audit_router)
    app.state.loki_client = loki
    app.state.archive_index = None
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub="admin-1", groups=("admin",)
    )
    return app


def _build_workflow_logs_app(*, loki: Any) -> FastAPI:
    app = FastAPI()
    app.include_router(workflow_logs_router)
    app.state.loki_client = loki
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub="admin-1", groups=("admin",)
    )
    return app


# ---------------------------------------------------------------------------
# /admin/audit/search?trace_id=... — extension to existing endpoint
# ---------------------------------------------------------------------------


def test_audit_search_forwards_trace_id_to_loki_client() -> None:
    """The new ``trace_id`` query param is forwarded as a kwarg."""

    loki = _FakeLokiClientWithTraceId(
        hits=[{"id": "1", "trace_id": "abc-123", "summary": "foo"}]
    )
    app = _build_audit_app(loki=loki)
    client = TestClient(app)

    response = client.get("/admin/audit/search", params={"trace_id": "abc-123"})

    assert response.status_code == 200, response.text
    body = response.json()

    # The kwarg was forwarded to the modern client.
    assert len(loki.search_calls) == 1
    assert loki.search_calls[0]["trace_id"] == "abc-123"

    # The hit comes back tagged with ``archived: False``.
    assert body["loki_count"] == 1
    assert body["results"][0]["archived"] is False
    assert body["results"][0]["trace_id"] == "abc-123"


def test_audit_search_drops_hits_with_mismatched_trace_id() -> None:
    """Defensive client-side filter rejects rows the upstream missed."""

    loki = _FakeLokiClientWithTraceId(
        hits=[
            {"id": "1", "trace_id": "abc-123", "summary": "match"},
            {"id": "2", "trace_id": "other", "summary": "leak"},
        ]
    )
    app = _build_audit_app(loki=loki)
    client = TestClient(app)

    response = client.get("/admin/audit/search", params={"trace_id": "abc-123"})

    assert response.status_code == 200
    body = response.json()
    assert body["loki_count"] == 1
    ids = [r["id"] for r in body["results"]]
    assert ids == ["1"]


def test_audit_search_falls_back_when_legacy_client_rejects_trace_id() -> None:
    """Older Loki clients without the ``trace_id`` kwarg still succeed."""

    loki = _LegacyLokiClient(
        hits=[{"id": "1", "trace_id": "abc-123", "summary": "foo"}]
    )
    app = _build_audit_app(loki=loki)
    client = TestClient(app)

    response = client.get("/admin/audit/search", params={"trace_id": "abc-123"})

    assert response.status_code == 200, response.text
    body = response.json()
    # Legacy client was called once (the retry path drops the kwarg).
    assert len(loki.search_calls) == 1
    # Client-side filter still selects the matching row.
    assert body["loki_count"] == 1


# ---------------------------------------------------------------------------
# /api/v1/workflows/{wf_id}/logs — new endpoint (Requirement 8.6)
# ---------------------------------------------------------------------------


def test_workflow_logs_builds_logql_with_workflow_and_trace_id() -> None:
    """The endpoint forwards a properly-formed LogQL stream selector."""

    loki_response = {
        "data": {
            "result": [
                {
                    "stream": {
                        "workflow_id": "auto-PAY-1",
                        "trace_id": "01900000-0000-7000-8000-000000000001",
                        "service": "automation-worker",
                    },
                    "values": [
                        ["1700000000000000000", "step started"],
                        ["1700000000100000000", "step completed"],
                    ],
                }
            ]
        }
    }
    loki = _LokiClientWithQueryRange(response=loki_response)
    app = _build_workflow_logs_app(loki=loki)
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows/auto-PAY-1/logs",
        params={"trace_id": "01900000-0000-7000-8000-000000000001"},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    expected_logql = (
        '{workflow_id="auto-PAY-1", '
        'trace_id="01900000-0000-7000-8000-000000000001"}'
    )
    assert body["logql"] == expected_logql
    assert body["workflow_id"] == "auto-PAY-1"
    assert body["trace_id"] == "01900000-0000-7000-8000-000000000001"
    assert body["warnings"] == []

    # query_range was invoked with the LogQL we built.
    assert len(loki.calls) == 1
    assert loki.calls[0]["query"] == expected_logql

    # The streams response was flattened into one entry per log line.
    assert len(body["results"]) == 2
    first = body["results"][0]
    assert first["workflow_id"] == "auto-PAY-1"
    assert first["trace_id"] == "01900000-0000-7000-8000-000000000001"
    assert first["service"] == "automation-worker"
    assert first["line"] == "step started"


def test_workflow_logs_omits_trace_id_when_not_supplied() -> None:
    """Without ``trace_id`` only the workflow_id matcher is in the selector."""

    loki = _LokiClientWithQueryRange(
        response={"data": {"result": []}},
    )
    app = _build_workflow_logs_app(loki=loki)
    client = TestClient(app)

    response = client.get("/api/v1/workflows/auto-PAY-2/logs")

    assert response.status_code == 200
    assert response.json()["logql"] == '{workflow_id="auto-PAY-2"}'


def test_workflow_logs_returns_warning_when_loki_not_wired() -> None:
    """Missing Loki client → soft-fail with empty results + warning."""

    app = _build_workflow_logs_app(loki=None)
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows/auto-PAY-1/logs",
        params={"trace_id": "abc-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["warnings"] == ["loki_unavailable"]
    # The LogQL is still surfaced so the FE can show "would have run X".
    assert "auto-PAY-1" in body["logql"]


def test_workflow_logs_returns_warning_when_loki_query_fails() -> None:
    """Loki RPC failure → soft-fail with empty results + warning."""

    loki = _ExplodingLokiClient()
    app = _build_workflow_logs_app(loki=loki)
    client = TestClient(app)

    response = client.get("/api/v1/workflows/auto-PAY-1/logs")

    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["warnings"] == ["loki_query_failed"]


def test_workflow_logs_filters_out_rows_for_other_workflows() -> None:
    """Defensive client-side filter rejects rows from other workflows."""

    loki_response = {
        "data": {
            "result": [
                {
                    "stream": {"workflow_id": "auto-PAY-1"},
                    "values": [["1", "good"]],
                },
                {
                    "stream": {"workflow_id": "auto-PAY-OTHER"},
                    "values": [["2", "leak"]],
                },
            ]
        }
    }
    loki = _LokiClientWithQueryRange(response=loki_response)
    app = _build_workflow_logs_app(loki=loki)
    client = TestClient(app)

    response = client.get("/api/v1/workflows/auto-PAY-1/logs")

    assert response.status_code == 200
    body = response.json()
    lines = [r["line"] for r in body["results"]]
    assert lines == ["good"]


def test_workflow_logs_falls_back_to_search_when_no_query_range() -> None:
    """Clients exposing only ``search`` are still usable."""

    loki = _FakeLokiClientWithTraceId(
        hits=[
            {
                "id": "1",
                "workflow_id": "auto-PAY-1",
                "trace_id": "abc-123",
                "line": "fallback",
            }
        ]
    )
    app = _build_workflow_logs_app(loki=loki)
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows/auto-PAY-1/logs",
        params={"trace_id": "abc-123"},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(loki.search_calls) == 1
    assert loki.search_calls[0]["trace_id"] == "abc-123"
    assert body["results"][0]["line"] == "fallback"


def test_workflow_logs_rejects_trace_id_with_label_breaking_chars() -> None:
    """Injection-resistant: ``trace_id`` may not contain LogQL meta chars."""

    app = _build_workflow_logs_app(loki=_FakeLokiClientWithTraceId())
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows/auto-1/logs",
        params={"trace_id": 'evil"} | drop'},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_trace_id"


def test_workflow_logs_respects_max_trace_id_length() -> None:
    """``trace_id`` longer than the cap is rejected by FastAPI as 422."""

    app = _build_workflow_logs_app(loki=_FakeLokiClientWithTraceId())
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows/auto-1/logs",
        params={"trace_id": "a" * 65},
    )

    # FastAPI's Query(max_length=...) returns 422 for over-length.
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def test_build_logql_escapes_double_quotes() -> None:
    """LogQL injection guard: embedded ``"`` is escaped, not stripped."""

    # A workflow_id containing a double quote should survive escaping
    # so the selector remains syntactically valid.
    out = _build_logql('auto"X', None)
    assert out == '{workflow_id="auto\\"X"}'


def test_flatten_loki_streams_handles_already_flat_lists() -> None:
    """Test stubs that pre-flatten streams should pass through unchanged."""

    flat = [{"line": "a"}, {"line": "b"}]
    assert _flatten_loki_streams(flat) == flat


def test_flatten_loki_streams_handles_none() -> None:
    """``None`` from a misbehaving client → empty list, not a crash."""

    assert _flatten_loki_streams(None) == []
