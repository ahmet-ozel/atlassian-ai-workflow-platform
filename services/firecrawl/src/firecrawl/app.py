"""FastAPI app exposing the egress-allowlisted scrape/search surface.

The app is intentionally minimal: it owns the HTTP/JSON envelope and the
audit/metric side-effects but defers the *decision* to
:func:`firecrawl.egress.decide_egress`. Tests can therefore exercise the
matching logic directly without a TestClient and the property test gets a
stable invariant to assert against.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from firecrawl.config import Settings
from firecrawl.egress import (
    EGRESS_ALLOWED_AUDIT_ACTION,
    EGRESS_DENIED_AUDIT_ACTION,
    EgressDecision,
    decide_egress,
    parse_allowlist,
)
from firecrawl.metrics import metrics

__all__ = ["app", "create_app", "build_search_url"]

logger = logging.getLogger("firecrawl.egress")

#: Structured logger for client-source observability.
_client_source_logger = logging.getLogger("firecrawl.client_source")

_CLIENT_SOURCE_HEADER = "X-Client-Source"
_CLIENT_SOURCE_UNKNOWN = "unknown"


class ScrapeRequest(BaseModel):
    """Input payload for ``POST /scrape``.

    Mirrors the upstream Firecrawl shape closely enough for the agent runner
    worker's existing ``FIRECRAWL_BASE_URL`` callers to work without code
    changes; the wrapper passes ``url`` straight through after the allowlist
    check passes.
    """

    url: str = Field(..., description="Target URL to fetch.")
    # The upstream Firecrawl accepts a number of optional knobs (formats,
    # onlyMainContent, …). We accept them as opaque ``extra`` fields so the
    # wrapper stays a thin pass-through.
    formats: list[str] | None = None
    only_main_content: bool | None = Field(default=None, alias="onlyMainContent")

    model_config = {"populate_by_name": True, "extra": "allow"}


class SearchRequest(BaseModel):
    """Input payload for ``POST /search``.

    The wrapper builds an HTTP target from the search engine host (taken
    from the optional ``engine`` field, default ``html.duckduckgo.com``)
    and the query, then runs the same egress check against that
    synthesized URL. The HTML results page is parsed into a structured
    list of ``{url, title, content}`` entries so callers receive search
    hits rather than a raw HTML blob.
    """

    query: str = Field(..., description="Free-text search query.")
    engine: str = Field(
        default="html.duckduckgo.com",
        description="Hostname of the search backend the wrapper will fetch.",
    )
    limit: int = Field(default=10, ge=1, le=50)

    model_config = {"populate_by_name": True, "extra": "allow"}


def build_search_url(query: str, engine: str) -> str:
    """Synthesize an HTTPS URL for the configured search engine.

    Kept as a small pure helper so the property test can drive
    ``decide_egress`` against a deterministic URL shape. DuckDuckGo's
    ``html.duckduckgo.com/html/`` endpoint returns a server-rendered
    results page whose anchors are stable to parse.
    """

    from urllib.parse import quote_plus

    safe_engine = engine.strip().lower() or "html.duckduckgo.com"
    # The DuckDuckGo HTML endpoint lives under ``/html/``; other engines
    # fall back to a bare ``/?q=`` form.
    if "duckduckgo.com" in safe_engine:
        return f"https://{safe_engine}/html/?q={quote_plus(query)}"
    return f"https://{safe_engine}/?q={quote_plus(query)}"


def parse_search_results(html: str, *, limit: int) -> list[dict[str, str]]:
    """Parse a DuckDuckGo HTML results page into structured hits.

    Extracts each result's destination URL, title and snippet from the
    server-rendered markup. DuckDuckGo wraps the real target in a
    ``/l/?uddg=<urlencoded>`` redirect, which is unwrapped here so the
    caller receives a directly-fetchable URL. Parsing is best-effort and
    regex-based (no extra dependency); a markup change degrades to fewer
    or zero results rather than raising.
    """

    import html as _html
    import re
    from urllib.parse import parse_qs, unquote, urlparse

    results: list[dict[str, str]] = []
    seen: set[str] = set()

    # Each organic result is an ``<a class="result__a" href="...">title</a>``.
    anchor_re = re.compile(
        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    # Snippets live in ``<a class="result__snippet">...</a>`` (optional).
    snippet_re = re.compile(
        r'<a[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    tag_re = re.compile(r"<[^>]+>")

    def _clean(fragment: str) -> str:
        return _html.unescape(tag_re.sub("", fragment)).strip()

    def _unwrap(href: str) -> str:
        # DuckDuckGo redirect: //duckduckgo.com/l/?uddg=<encoded target>
        if "uddg=" in href:
            qs = parse_qs(urlparse(href if "//" in href else "https:" + href).query)
            target = qs.get("uddg", [""])[0]
            if target:
                return unquote(target)
        if href.startswith("//"):
            return "https:" + href
        return href

    snippets = [_clean(s) for s in snippet_re.findall(html)]
    for idx, (href, title_html) in enumerate(anchor_re.findall(html)):
        url = _unwrap(href)
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        results.append({
            "url": url,
            "title": _clean(title_html),
            "content": snippets[idx] if idx < len(snippets) else "",
        })
        if len(results) >= limit:
            break
    return results


def _log_decision(decision: EgressDecision, *, endpoint: str, client_source: str = _CLIENT_SOURCE_UNKNOWN) -> None:
    """Emit a structured log record for the audit trail.

    Tests assert that the string ``egress_denied`` appears in the log
    output for any disallowed host, so the action token is included
    verbatim.
    """

    extra = {
        "audit_action": decision.audit_action,
        "endpoint": endpoint,
        "host": decision.host,
        "url": decision.url,
        "reason": decision.reason,
        "client_source": client_source,
    }
    if decision.verdict == "denied":
        logger.warning("egress_denied host=%s reason=%s url=%s client_source=%s", decision.host, decision.reason, decision.url, client_source, extra=extra)
    else:
        logger.info("egress_allowed host=%s url=%s client_source=%s", decision.host, decision.url, client_source, extra=extra)


def _record_metric(decision: EgressDecision) -> None:
    if decision.verdict == "denied":
        metrics.record_denied()
    else:
        metrics.record_allowed()


def _denied_response(decision: EgressDecision) -> JSONResponse:
    """Build the canonical 403 body for a denied egress."""

    return JSONResponse(
        status_code=403,
        content={
            "error": "egress_denied",
            "error_code": EGRESS_DENIED_AUDIT_ACTION,
            "host": decision.host,
            "reason": decision.reason,
            "message": (
                "Target host is not in FIRECRAWL_EGRESS_ALLOWLIST. "
                "Update the allowlist or route the request through an approved domain."
            ),
        },
    )


async def _forward_or_fetch(
    settings: Settings,
    *,
    target_url: str,
    upstream_path: str,
    upstream_body: Mapping[str, Any] | None,
    client: httpx.AsyncClient | None = None,
) -> JSONResponse:
    """Either forward to the upstream Firecrawl or perform the fetch directly.

    The branch is decided by ``FIRECRAWL_UPSTREAM_BASE_URL``. When unset
    (the default in the dev profile and in unit tests), the wrapper does a
    minimal ``httpx`` GET on the target URL and returns a JSON envelope so
    callers don't have to special-case dev. The pass-through behaviour is
    not the load-bearing piece of this task — the egress check above is —
    so the response shape is intentionally simple.
    """

    timeout = settings.request_timeout_s
    headers = {}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        if settings.upstream_base_url:
            url = settings.upstream_base_url.rstrip("/") + upstream_path
            resp = await client.post(url, json=upstream_body or {}, headers=headers)
            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw": resp.text}
            return JSONResponse(status_code=resp.status_code, content=payload)

        # Built-in fetcher branch: GET the target URL and return its body.
        resp = await client.get(target_url, headers=headers)
        return JSONResponse(
            status_code=200,
            content={
                "url": target_url,
                "status_code": resp.status_code,
                "content": resp.text,
                "content_type": resp.headers.get("content-type", ""),
            },
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "upstream_error",
                "message": str(exc),
                "url": target_url,
            },
        )
    finally:
        if owns_client:
            await client.aclose()


async def _fetch_and_parse_search(
    settings: Settings,
    *,
    target_url: str,
    limit: int,
    client: httpx.AsyncClient | None = None,
) -> JSONResponse:
    """Fetch a search-engine HTML page and return structured results.

    Returns a ``{"data": [{url, title, content}], "count": N}`` envelope.
    A browser-like ``User-Agent`` is sent because DuckDuckGo's HTML
    endpoint returns an empty body to header-less clients. Transport
    failures surface as HTTP 502 (mirrors :func:`_forward_or_fetch`).
    """

    timeout = settings.request_timeout_s
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    try:
        resp = await client.get(target_url, headers=headers)
        results = parse_search_results(resp.text, limit=limit)
        return JSONResponse(
            status_code=200,
            content={
                "url": target_url,
                "status_code": resp.status_code,
                "data": results,
                "count": len(results),
            },
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={
                "error": "upstream_error",
                "message": str(exc),
                "url": target_url,
            },
        )
    finally:
        if owns_client:
            await client.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Instantiate the FastAPI app with a fresh settings snapshot.

    The factory pattern matches the rest of the platform (cf.
    ``automation_service.app.create_app``) and gives integration tests a
    way to swap settings without monkey-patching module globals.
    """

    s = settings or Settings()
    api = FastAPI(title="firecrawl-egress", version="0.0.0")

    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/health")
    async def health() -> dict[str, str]:
        # Compat alias matching the upstream Firecrawl's `/health` probe.
        return {"status": "ok"}

    @api.get("/metrics", response_class=PlainTextResponse)
    async def metrics_endpoint() -> str:
        return metrics.render()

    @api.post("/scrape")
    async def scrape(payload: ScrapeRequest, request: Request) -> JSONResponse:
        client_source = request.headers.get(_CLIENT_SOURCE_HEADER, _CLIENT_SOURCE_UNKNOWN)
        _client_source_logger.info(
            "firecrawl_request client_source=%s endpoint=/scrape url=%s",
            client_source,
            payload.url,
            extra={
                "client_source": client_source,
                "endpoint": "/scrape",
                "url": payload.url,
                "timestamp": time.time(),
            },
        )
        allowlist = parse_allowlist(s.egress_allowlist_raw)
        decision = decide_egress(payload.url, allowlist)
        _record_metric(decision)
        _log_decision(decision, endpoint="/scrape", client_source=client_source)
        if decision.verdict == "denied":
            return _denied_response(decision)
        return await _forward_or_fetch(
            s,
            target_url=payload.url,
            upstream_path="/v1/scrape",
            upstream_body=payload.model_dump(by_alias=True, exclude_none=True),
            client=getattr(request.app.state, "http_client", None),
        )

    @api.post("/search")
    async def search(payload: SearchRequest, request: Request) -> JSONResponse:
        client_source = request.headers.get(_CLIENT_SOURCE_HEADER, _CLIENT_SOURCE_UNKNOWN)
        _client_source_logger.info(
            "firecrawl_request client_source=%s endpoint=/search query=%s",
            client_source,
            payload.query[:100],
            extra={
                "client_source": client_source,
                "endpoint": "/search",
                "query": payload.query[:100],
                "timestamp": time.time(),
            },
        )
        allowlist = parse_allowlist(s.egress_allowlist_raw)
        target_url = build_search_url(payload.query, payload.engine)
        decision = decide_egress(target_url, allowlist)
        _record_metric(decision)
        _log_decision(decision, endpoint="/search", client_source=client_source)
        if decision.verdict == "denied":
            return _denied_response(decision)
        # When an upstream Firecrawl is configured, forward verbatim so its
        # native search results flow through. Otherwise fetch the HTML
        # results page ourselves and parse it into structured hits so the
        # caller gets ``{url, title, content}`` entries, not raw HTML.
        if s.upstream_base_url:
            return await _forward_or_fetch(
                s,
                target_url=target_url,
                upstream_path="/v1/search",
                upstream_body=payload.model_dump(by_alias=True, exclude_none=True),
                client=getattr(request.app.state, "http_client", None),
            )
        return await _fetch_and_parse_search(
            s,
            target_url=target_url,
            limit=payload.limit,
            client=getattr(request.app.state, "http_client", None),
        )

    return api


#: Module-level app instance for ``uvicorn src.main:app`` style entry points.
app: FastAPI = create_app()
