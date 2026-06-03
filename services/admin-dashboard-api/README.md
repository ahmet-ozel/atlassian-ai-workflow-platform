# admin-dashboard-api

FastAPI service that backs the Next.js admin dashboard UI
(`ui/admin-dashboard`). It exposes admin-only views for services,
workflows, departments, prompts, audit, costs, notifications, security,
and feature flags. This package is currently an initial implementation: only the
shared `/healthz` + `/readyz` contract is wired.

- Container port: **8082**
- `X-Client-Source` identity: `admin-dashboard-api`
- Required dependencies (runtime): Postgres, Temporal, Vault,
  `atlassian-mcp` (consumed via the Compose stack or local equivalents).

## Layout

```
services/admin-dashboard-api/
├── src/
│   ├── __init__.py
│   ├── main.py            # FastAPI app + /healthz + /readyz
│   ├── config.py          # Pydantic Settings, dependencies_reachable() stub
│   ├── routers/           # APIRouters per admin page (placeholder)
│   ├── auth/              # OIDC/JWT helpers (placeholder)
│   ├── clients/           # Temporal/Postgres/MCP clients (placeholder)
│   └── prompts_git/       # Prompts-as-code git workflow (placeholder)
├── tests/{unit,integration,e2e}/.gitkeep
├── pyproject.toml
└── README.md
```

## Health contract

| Endpoint  | Status | Body                         |
|-----------|--------|------------------------------|
| `/healthz`| 200    | `{"status": "ok"}`           |
| `/readyz` | 200    | `{"status": "ready"}`        |
| `/readyz` | 503    | `{"status": "not_ready"}` (≤64 bytes) |

Readiness flips to 503 when `Settings.dependencies_reachable()` returns
`False`. The current stub always returns `True`; real probes for
Postgres/Temporal/Vault arrive in later work.

## Standalone build & run

The service ships a multi-stage `Dockerfile` and a
`.env.example` so it can run in isolation, without
the rest of the Compose stack. While those files are not yet present in
the full stack, the local Python workflow already works:

```bash
# from services/admin-dashboard-api/
python -m venv .venv
. .venv/Scripts/activate          # Windows; use bin/activate on Unix
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

# launch the FastAPI app on port 8082
uvicorn src.main:app --host 0.0.0.0 --port 8082
```

Once the Dockerfile and `.env.example` land, the canonical Standalone
Mode commands will be:

```bash
# from services/admin-dashboard-api/
cp .env.example .env
docker build -t admin-dashboard-api:dev .
docker run --env-file .env -p 8082:8082 admin-dashboard-api:dev
```

Verify the service is up:

```bash
curl -fsS http://localhost:8082/healthz   # -> {"status":"ok"}
curl -fsS http://localhost:8082/readyz    # -> {"status":"ready"}
```

## References

- Service topology and folder layout
- Service skeletons and standalone builds
- HTTP service skeleton
