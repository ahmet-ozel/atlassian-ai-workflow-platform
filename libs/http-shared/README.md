# http-shared

Shared HTTP client factory for the platform services. Every outgoing
MCP or Firecrawl call must originate from :func:`http_shared.make_mcp_client`
so that the `X-Client-Source` header is stamped consistently.

## Public API

- `KNOWN_CLIENT_SOURCES: frozenset[str]` — the eight Component identities
  that may appear in `X-Client-Source`. The `automation-worker`
  identity is included for worker-originated MCP calls.
- `make_mcp_client(client_source, *, timeout=30.0, **kwargs) -> httpx.AsyncClient`
  — returns an `httpx.AsyncClient` whose default headers already include
  `X-Client-Source: <client_source>`. Caller-supplied `headers=` are
  merged in, but the factory header wins on key collision.

```python
from http_shared import make_mcp_client

async with make_mcp_client("automation-service") as client:
    resp = await client.get("http://atlassian-mcp:8090/healthz")
```

## Standalone build & run

The package is a plain `pyproject.toml` Python package; it does not ship
its own container image. Build and install it locally with:

```bash
# from libs/http-shared/
python -m pip install --upgrade pip build
python -m build           # produces dist/http_shared-*.whl
python -m pip install dist/http_shared-*.whl
```

To run the unit/property tests against the package without installing it
into the system environment, create a throwaway virtual environment first:

```bash
python -m venv .venv
. .venv/Scripts/activate    # Windows; use bin/activate on Unix
python -m pip install -e .
python -m pip install pytest hypothesis
pytest
```

The package has a single runtime dependency (`httpx>=0.27,<1`) and targets
Python `>=3.12,<3.13`.
