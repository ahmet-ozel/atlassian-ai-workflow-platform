"""HTTP-level tests for the FastAPI wrapper.

The tests exercise the 403 / metric / log triple on the denial path and the
healthcheck/metrics endpoints. We avoid any real network egress by setting
up an allow-list that the test inputs deliberately miss; the allow-listed
branch is covered separately by mocking ``httpx``.
"""

from __future__ import annotations

import logging
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from firecrawl.app import create_app
from firecrawl.config import Settings
from firecrawl.metrics import metrics


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    """Each test starts with fresh counters."""

    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture()
def client_denied() -> TestClient:
    """App configured with an allowlist that the tests deliberately miss."""

    settings = Settings(
        FIRECRAWL_EGRESS_ALLOWLIST="example.com,wikipedia.org",
        FIRECRAWL_UPSTREAM_BASE_URL="",
    )
    return TestClient(create_app(settings=settings))


def test_healthz_returns_ok(client_denied: TestClient) -> None:
    resp = client_denied.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_alias_returns_ok(client_denied: TestClient) -> None:
    resp = client_denied.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_scrape_denies_disallowed_host(client_denied: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="firecrawl.egress")
    resp = client_denied.post("/scrape", json={"url": "https://reddit.com/r/python"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["error"] == "egress_denied"
    assert body["error_code"] == "egress_denied"
    assert body["host"] == "reddit.com"
    assert body["reason"] == "not_in_allowlist"
    # Audit log carries the canonical action token.
    assert any("egress_denied" in r.getMessage() for r in caplog.records)
    # Metric counter advanced exactly once.
    assert metrics.denied == 1
    assert metrics.allowed == 0


def test_scrape_denies_empty_allowlist(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(FIRECRAWL_EGRESS_ALLOWLIST="")
    client = TestClient(create_app(settings=settings))
    caplog.set_level(logging.WARNING, logger="firecrawl.egress")
    resp = client.post("/scrape", json={"url": "https://example.com/"})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "empty_allowlist"
    assert metrics.denied == 1


def test_search_denies_disallowed_engine(client_denied: TestClient) -> None:
    resp = client_denied.post(
        "/search",
        json={"query": "platform mimari", "engine": "google.com"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["host"] == "google.com"
    assert metrics.denied == 1


def test_metrics_endpoint_renders_counters(client_denied: TestClient) -> None:
    # Trigger one denial, then check the metrics scrape.
    client_denied.post("/scrape", json={"url": "https://reddit.com/"})
    resp = client_denied.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    assert "firecrawl_egress_denied_total 1" in body
    assert "firecrawl_egress_allowed_total 0" in body


def test_scrape_allows_allowlisted_host_with_mocked_client() -> None:
    """The allowed branch forwards through the configured ``httpx`` client.

    We swap the app-level client for a ``MockTransport`` so the test never
    hits the network, and verify the wrapper actually fetched the target.
    """

    settings = Settings(
        FIRECRAWL_EGRESS_ALLOWLIST="example.com",
        FIRECRAWL_UPSTREAM_BASE_URL="",
    )
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    app = create_app(settings=settings)
    app.state.http_client = httpx.AsyncClient(transport=transport, timeout=5.0)

    client = TestClient(app)
    try:
        resp = client.post("/scrape", json={"url": "https://example.com/page"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["url"] == "https://example.com/page"
        assert body["status_code"] == 200
        assert "<html>ok</html>" in body["content"]
        assert captured["url"] == "https://example.com/page"
        assert metrics.allowed == 1
        assert metrics.denied == 0
    finally:
        # The TestClient ran the AsyncClient's event loop; close it through
        # a lifespan hook by closing the underlying transport directly.
        transport.handler = None  # type: ignore[assignment]


def test_invalid_url_is_denied_without_traceback(client_denied: TestClient) -> None:
    resp = client_denied.post("/scrape", json={"url": "not-a-url"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "invalid_url"
    assert metrics.denied == 1
