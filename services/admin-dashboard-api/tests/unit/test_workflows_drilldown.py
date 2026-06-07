"""Unit tests for ``src.routers.workflows_drilldown``.

The drill-down endpoint folds three local-DB enrichments into the
upstream Temporal payload:

* ``llm_usage[]`` - read from ``shared.cost_tracking`` joined with
  ``automation.audit_events`` for ``prompt_path`` / ``prompt_version``.
* ``audit_chain[]`` - read from ``automation.audit_events`` filtered
  by ``resource = 'workflow:{wf_id}'`` *or* ``payload->>'workflow_id'``.
* ``external_links{}`` - extracted from the audit payloads via the
  W3 deeplink helper :func:`_external_links.build_external_links`.

The tests inject:

* A fake asyncpg pool that records the SQL it sees and returns
  scripted rows so we can assert the response shape without
  standing up Postgres.
* A fake :class:`AdminProxy` whose ``forward`` returns a
  :class:`ProxyResponse`-like object with a JSON body, exercising
  the best-effort upstream fold-in.

The tests intentionally do NOT assert exact SQL strings - only that
the right tables are read and the right scalar parameters are
bound - so a future query refactor that preserves semantics does
not need to update the test suite.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern in test_prompts_audit_writer.py).
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.workflows_drilldown import (  # noqa: E402
    router as workflows_drilldown_router,
)
from src.routers._external_links import build_external_links  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Records every ``fetch`` call and returns scripted rows."""

    def __init__(
        self,
        *,
        responses: list[list[dict[str, Any]]] | None = None,
        raise_on_fetch: BaseException | None = None,
    ) -> None:
        # ``responses`` is a queue: each ``fetch`` call pops the next
        # row list off the front. Tests that only need a single
        # response can pass a one-element list.
        self.responses = list(responses or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._raise = raise_on_fetch

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((query, args))
        if self._raise is not None:
            raise self._raise
        if not self.responses:
            return []
        return self.responses.pop(0)


class _FakeAcquireContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakePool:
    """Minimal asyncpg-pool-shaped fake.

    Tests configure the ``responses`` queue: the first element answers
    the ``llm_usage`` query, the second answers the ``audit_chain``
    query (matching the order the router calls them).
    """

    def __init__(
        self,
        *,
        responses: list[list[dict[str, Any]]] | None = None,
        raise_on_fetch: BaseException | None = None,
    ) -> None:
        self.connection = _FakeConnection(
            responses=responses, raise_on_fetch=raise_on_fetch
        )

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self.connection)


class _FakeProxyResponse:
    """``ProxyResponse``-shaped object used by the upstream fold-in."""

    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"content-type": "application/json"}


class _FakeUpstreamProxy:
    """Records every ``forward`` call and returns a scripted response."""

    def __init__(self, *, response: _FakeProxyResponse | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response or _FakeProxyResponse(
            status_code=200, body=b""
        )

    async def forward(self, **kwargs: Any) -> _FakeProxyResponse:
        self.calls.append(kwargs)
        return self._response


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    *,
    pool: _FakePool | None = None,
    proxy: _FakeUpstreamProxy | None = None,
) -> FastAPI:
    """Wire the router with overridden dependencies.

    The ``require_admin`` dependency is bypassed via
    :attr:`FastAPI.dependency_overrides` - the focus of these tests
    is the response shape, not the auth boundary.
    """

    app = FastAPI()
    app.include_router(workflows_drilldown_router)
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub="alice", groups=("admin",)
    )
    app.state.pg_pool = pool
    app.state.admin_proxy = proxy
    return app


def _llm_row(
    *,
    activity_id: str = "act-1",
    model: str = "gpt-4",
    token_in: int = 100,
    token_out: int = 50,
    cost_usd: str = "0.001234",
    prompt_path: str | None = "platform/prompts/task_analysis.md",
    prompt_version: str | None = "abc1234",
) -> dict[str, Any]:
    return {
        "activity_id": activity_id,
        "model": model,
        "token_in": token_in,
        "token_out": token_out,
        # asyncpg returns NUMERIC as Decimal; passing a str through is
        # fine because the router stringifies it anyway.
        "cost_usd": cost_usd,
        "prompt_path": prompt_path,
        "prompt_version": prompt_version,
    }


def _audit_row(
    *,
    action: str = "workflow_started",
    actor_id: str = "alice",
    actor_role: str = "admin",
    created_at: datetime | None = None,
    payload: Any = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "created_at": created_at
        or datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "payload": payload,
    }


# ===========================================================================
# Tests - happy path
# ===========================================================================


class TestGetWorkflowHappyPath:
    def test_returns_local_enrichment_when_proxy_unavailable(self) -> None:
        """No upstream proxy  response carries the three new fields."""

        pool = _FakePool(
            responses=[
                [_llm_row()],
                [
                    _audit_row(
                        action="jira_issue_linked",
                        payload={
                            "workflow_id": "wf-1",
                            "jira_issue_url": "https://acme.atlassian.net/browse/PAY-1",
                        },
                    ),
                ],
            ]
        )
        app = _build_app(pool=pool, proxy=None)
        client = TestClient(app)

        response = client.get("/admin/workflows/wf-1")

        assert response.status_code == 200
        body = response.json()

        # The three enrichment fields are always
        # present, regardless of upstream availability.
        assert "llm_usage" in body
        assert "audit_chain" in body
        assert "external_links" in body

        assert body["workflow_id"] == "wf-1"

        # llm_usage row is shaped per the design contract.
        assert body["llm_usage"] == [
            {
                "activity_id": "act-1",
                "prompt_path": "platform/prompts/task_analysis.md",
                "prompt_version": "abc1234",
                "model": "gpt-4",
                "token_in": 100,
                "token_out": 50,
                "cost_usd": "0.001234",
            }
        ]

        # audit_chain row is shaped per the design contract; the raw
        # payload is dropped from the wire response (only the
        # summary is surfaced).
        assert len(body["audit_chain"]) == 1
        entry = body["audit_chain"][0]
        assert entry["action"] == "jira_issue_linked"
        assert entry["actor"] == "alice"
        assert entry["timestamp"] == "2025-01-01T12:00:00+00:00"
        assert "payload_summary" in entry
        assert "payload" not in entry  # internal-only key stripped

        # external_links extracts the Jira URL from the audit payload.
        assert body["external_links"] == {
            "jira_issue_url": "https://acme.atlassian.net/browse/PAY-1"
        }

    def test_folds_upstream_payload_when_proxy_returns_dict(self) -> None:
        """Upstream events / activities are preserved verbatim."""

        upstream_body = {
            "workflow_id": "wf-1",
            "events": [{"id": 1, "type": "WorkflowExecutionStarted"}],
            "activities": [{"id": "act-1", "status": "completed"}],
            "failures": [],
        }
        proxy = _FakeUpstreamProxy(
            response=_FakeProxyResponse(
                status_code=200,
                body=json.dumps(upstream_body).encode("utf-8"),
            ),
        )
        pool = _FakePool(responses=[[], []])
        app = _build_app(pool=pool, proxy=proxy)
        client = TestClient(app)

        response = client.get("/admin/workflows/wf-1")

        assert response.status_code == 200
        body = response.json()
        # Upstream keys preserved.
        assert body["events"] == [
            {"id": 1, "type": "WorkflowExecutionStarted"}
        ]
        assert body["activities"] == [
            {"id": "act-1", "status": "completed"}
        ]
        # Local enrichments still added.
        assert body["llm_usage"] == []
        assert body["audit_chain"] == []
        assert body["external_links"] == {}

        # Upstream forwarded to the right path.
        assert len(proxy.calls) == 1
        assert proxy.calls[0]["method"] == "GET"
        assert proxy.calls[0]["path"] == "/admin/workflows/wf-1"

    def test_payload_object_decoded_when_asyncpg_returns_string(self) -> None:
        """JSONB returned as raw string is parsed for link extraction."""

        # Some asyncpg deployments return JSONB as a string when no
        # codec is registered. The router must still extract URLs.
        pool = _FakePool(
            responses=[
                [],
                [
                    _audit_row(
                        action="bitbucket_pr_opened",
                        payload=json.dumps(
                            {
                                "workflow_id": "wf-1",
                                "pr_url": "https://bitbucket.org/acme/repo/pull-requests/42",
                            }
                        ),
                    ),
                ],
            ]
        )
        app = _build_app(pool=pool, proxy=None)
        client = TestClient(app)

        body = client.get("/admin/workflows/wf-1").json()
        assert body["external_links"] == {
            "bitbucket_pr_url": "https://bitbucket.org/acme/repo/pull-requests/42",
        }

    def test_audit_query_includes_workflow_resource_and_payload_lookups(
        self,
    ) -> None:
        """SQL query targets ``automation.audit_events`` and binds wf id."""

        pool = _FakePool(responses=[[], []])
        app = _build_app(pool=pool, proxy=None)
        client = TestClient(app)

        client.get("/admin/workflows/wf-deadbeef")

        # Two queries fire: one for cost_tracking, one for audit_events.
        assert len(pool.connection.calls) == 2
        first_sql, first_args = pool.connection.calls[0]
        second_sql, second_args = pool.connection.calls[1]

        assert "shared.cost_tracking" in first_sql
        assert first_args[0] == "wf-deadbeef"

        assert "automation.audit_events" in second_sql
        # Resource form (canonical) is bound first; then the bare
        # workflow id for the JSONB ``->>'workflow_id'`` branch.
        assert second_args[0] == "workflow:wf-deadbeef"
        assert second_args[1] == "wf-deadbeef"


# ===========================================================================
# Tests - degraded paths
# ===========================================================================


class TestGetWorkflowDegradesGracefully:
    def test_no_pool_returns_empty_arrays(self) -> None:
        """No pg_pool  response has empty ``llm_usage``/``audit_chain``."""

        app = _build_app(pool=None, proxy=None)
        client = TestClient(app)

        response = client.get("/admin/workflows/wf-1")

        assert response.status_code == 200
        body = response.json()
        assert body["llm_usage"] == []
        assert body["audit_chain"] == []
        assert body["external_links"] == {}
        assert body["workflow_id"] == "wf-1"

    def test_pool_raises_on_fetch_returns_empty_arrays(self) -> None:
        """Postgres errors degrade to empty arrays (no 5xx leaked)."""

        pool = _FakePool(raise_on_fetch=RuntimeError("connection lost"))
        app = _build_app(pool=pool, proxy=None)
        client = TestClient(app)

        response = client.get("/admin/workflows/wf-1")

        assert response.status_code == 200
        body = response.json()
        assert body["llm_usage"] == []
        assert body["audit_chain"] == []
        assert body["external_links"] == {}

    def test_upstream_5xx_falls_back_to_local_enrichment(self) -> None:
        """Upstream 502  local enrichment still surfaces."""

        proxy = _FakeUpstreamProxy(
            response=_FakeProxyResponse(
                status_code=502, body=b'{"detail":"upstream gone"}'
            ),
        )
        pool = _FakePool(
            responses=[
                [_llm_row()],
                [],
            ]
        )
        app = _build_app(pool=pool, proxy=proxy)
        client = TestClient(app)

        response = client.get("/admin/workflows/wf-1")

        assert response.status_code == 200
        body = response.json()
        assert len(body["llm_usage"]) == 1
        assert body["audit_chain"] == []

    def test_upstream_returns_non_dict_payload_wraps_under_upstream(
        self,
    ) -> None:
        """A non-object upstream JSON is preserved under ``upstream``."""

        proxy = _FakeUpstreamProxy(
            response=_FakeProxyResponse(
                status_code=200, body=b'["events"]'
            ),
        )
        pool = _FakePool(responses=[[], []])
        app = _build_app(pool=pool, proxy=proxy)
        client = TestClient(app)

        body = client.get("/admin/workflows/wf-1").json()
        # Non-dict upstream is preserved verbatim under ``upstream``
        # so the FE can render a debug view; the new fields are still
        # present.
        assert body["upstream"] == ["events"]
        assert "llm_usage" in body
        assert "audit_chain" in body
        assert "external_links" in body


# ===========================================================================
# Tests - payload summary truncation
# ===========================================================================


class TestPayloadSummary:
    def test_long_payloads_are_truncated(self) -> None:
        """Audit summaries are capped to keep wire size bounded."""

        big_payload = {"big": "x" * 5_000}
        pool = _FakePool(
            responses=[
                [],
                [_audit_row(payload=big_payload)],
            ]
        )
        app = _build_app(pool=pool, proxy=None)
        client = TestClient(app)

        body = client.get("/admin/workflows/wf-1").json()
        summary = body["audit_chain"][0]["payload_summary"]
        assert summary is not None
        # Truncation marker present and length bounded.
        assert summary.endswith("…")
        assert len(summary) <= 280


# ===========================================================================
# Tests - build_external_links (W3 deeplink helper)
# ===========================================================================


class TestBuildExternalLinks:
    def test_extracts_jira_bitbucket_confluence_in_one_pass(self) -> None:
        """All three URL types extracted from the same audit chain."""

        chain = [
            {
                "action": "workflow_started",
                "payload": {
                    "workflow_id": "wf-1",
                    "jira_issue_url": "https://acme.atlassian.net/browse/PAY-1",
                },
            },
            {
                "action": "bitbucket_pr_opened",
                "payload": {
                    "pr_url": "https://bitbucket.org/acme/repo/pull-requests/42",
                },
            },
            {
                "action": "confluence_page_created",
                "payload": {
                    "page_url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/123",
                },
            },
        ]
        links = build_external_links(chain)
        assert links == {
            "jira_issue_url": "https://acme.atlassian.net/browse/PAY-1",
            "bitbucket_pr_url": "https://bitbucket.org/acme/repo/pull-requests/42",
            "confluence_page_url": "https://acme.atlassian.net/wiki/spaces/ENG/pages/123",
        }

    def test_first_url_wins_on_repeated_keys(self) -> None:
        """Earliest audit row's URL is canonical (chronological order)."""

        chain = [
            {"payload": {"jira_issue_url": "https://a.example.test/browse/X-1"}},
            {"payload": {"jira_issue_url": "https://b.example.test/browse/X-1"}},
        ]
        assert build_external_links(chain) == {
            "jira_issue_url": "https://a.example.test/browse/X-1"
        }

    def test_non_https_urls_are_rejected(self) -> None:
        """Only ``https://`` strings are accepted (defence in depth)."""

        chain = [
            {"payload": {"jira_issue_url": "http://insecure.example/browse/X-1"}},
            {"payload": {"pr_url": "javascript:alert(1)"}},
        ]
        assert build_external_links(chain) == {}

    def test_empty_chain_returns_empty_dict(self) -> None:
        assert build_external_links([]) == {}

    def test_falls_back_to_aliases(self) -> None:
        """Older payload keys still match (issue_url / confluence_url)."""

        chain = [
            {"payload": {"issue_url": "https://x.example/browse/X-1"}},
            {"payload": {"confluence_url": "https://x.example/wiki/p/1"}},
        ]
        links = build_external_links(chain)
        assert links == {
            "jira_issue_url": "https://x.example/browse/X-1",
            "confluence_page_url": "https://x.example/wiki/p/1",
        }

    def test_non_string_payload_values_ignored(self) -> None:
        chain = [
            {"payload": {"jira_issue_url": 42, "pr_url": None}},
            {"payload": "not a dict"},
            {"payload": {"jira_issue_url": ""}},
        ]
        assert build_external_links(chain) == {}


@pytest.fixture(autouse=True)
def _reset_dependency_overrides() -> None:  # noqa: PT004 - fixture for cleanup
    """Ensure no test leaks dependency overrides into another."""

    yield
