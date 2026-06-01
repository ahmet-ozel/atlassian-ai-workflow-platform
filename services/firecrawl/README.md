# firecrawl — Egress-Allowlisted Web Search/Scrape Service

Self-hosted Firecrawl-compatible web scraping/search service with **egress
allowlist enforcement** (MIMARI §16.14 Y3, Requirement 10.3).

This service is a thin Python/FastAPI wrapper that exposes Firecrawl's HTTP
surface (`/scrape`, `/search`, `/healthz`, `/health`) and refuses to fetch any
URL whose host is not present in the comma-separated ``FIRECRAWL_EGRESS_ALLOWLIST``
environment variable. Disallowed hosts return HTTP 403 with
``error_code = "egress_denied"`` and emit a structured log record + metric
(``firecrawl_egress_denied_total``).

The wrapper does **not** re-implement Firecrawl. When a target host is
allow-listed, the request is fetched via ``httpx`` and the response body is
returned to the caller. For projects that need the full Firecrawl feature set
(JS rendering, sitemap crawl, etc.), set ``FIRECRAWL_UPSTREAM_BASE_URL`` to a
co-located upstream Firecrawl instance and the wrapper will forward the
allow-listed request to it.

## Why a wrapper?

Firecrawl itself does not enforce per-host egress filtering. Without a wrapper,
a malicious or misbehaving prompt could ask Firecrawl to fetch an internal
metadata endpoint (e.g. ``169.254.169.254``), an arbitrary intranet host, or a
sensitive third-party service. The wrapper closes this gap deterministically:

- Any request whose target URL fails ``FIRECRAWL_EGRESS_ALLOWLIST`` membership
  returns HTTP 403 + ``egress_denied``.
- The empty allowlist denies **all** external hosts (closed by default).
- Subdomain matches are explicit: ``example.com`` allows ``example.com`` and
  ``foo.example.com`` (the host is matched as a suffix on a label boundary).

The matching logic lives in
``src/firecrawl/egress.py`` and is exercised by both unit tests
(``tests/unit/test_egress.py``) and the property test
``platform/tests/property/test_firecrawl_egress.py`` (Property 16, task 12.9).

## HTTP API

| Method | Path        | Purpose                                                     |
|--------|-------------|-------------------------------------------------------------|
| GET    | `/healthz`  | Liveness probe (200 OK).                                    |
| GET    | `/health`   | Alias for ``/healthz`` (compat with upstream Firecrawl).    |
| POST   | `/scrape`   | Body: ``{"url": "..."}``. Allowlisted: 200 + body. Denied: 403. |
| POST   | `/search`   | Body: ``{"query": "..."}``. Search is allow-listed when ``search.<engine_host>`` is in the allowlist. |
| GET    | `/metrics`  | Plain-text counter snapshot (``egress_allowed_total``, ``egress_denied_total``). |

## Configuration

| Env                          | Required | Default | Description                                        |
|------------------------------|----------|---------|----------------------------------------------------|
| `PORT`                       | false    | `3002`  | HTTP listen port.                                  |
| `LOG_LEVEL`                  | false    | `INFO`  | Standard Python log levels.                        |
| `FIRECRAWL_EGRESS_ALLOWLIST` | false    | (empty) | Comma-separated host suffixes; empty denies all.   |
| `FIRECRAWL_UPSTREAM_BASE_URL`| false    | (empty) | Optional upstream Firecrawl URL for forwarding.    |
| `FIRECRAWL_REQUEST_TIMEOUT_S`| false    | `30`    | Seconds to wait for the upstream/origin response.  |

## Running

### Compose

The service is wired into ``platform/infra/docker-compose.yml`` under the
``firecrawl`` profile and built from this directory:

```bash
docker compose -f infra/docker-compose.yml --profile firecrawl up firecrawl
```

### Standalone

```bash
docker build -t firecrawl-egress .
docker run --rm -p 3002:3002 \
    -e FIRECRAWL_EGRESS_ALLOWLIST=docs.python.org,wikipedia.org \
    firecrawl-egress
```

## Tests

Unit tests live under ``tests/unit/`` and cover the allowlist matcher and the
denial path. The property test for Requirement 10.3 lives at
``platform/tests/property/test_firecrawl_egress.py`` (separate task 12.9).

```bash
pytest services/firecrawl/tests/unit -v
```
