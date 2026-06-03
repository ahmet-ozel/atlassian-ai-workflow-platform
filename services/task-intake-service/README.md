# task-intake-service

Multi-channel task intake service (email, Slack, web form, …) for the
Atlassian automation platform. Receives unstructured requests, normalises
them, and dispatches Temporal workflows through `automation-service`.

This is an initial implementation: only the FastAPI skeleton with `/healthz` and
`/readyz` is implemented. Business logic lives in future tasks.

## Profile-gated

This service is **opt-in** in the Compose stack. It is declared with
`profiles: ["task-intake"]` in `infra/docker-compose.yml`, which means
the default `docker compose -f infra/docker-compose.yml up -d` command
does **not** start it. Enable it explicitly:

```bash
# from the workspace root
docker compose -f infra/docker-compose.yml --profile task-intake up -d task-intake-service
```

The matching root feature flag is `FEATURE_FLAG_TASK_INTAKE_ENABLED`
(default `false` in `.env.example`).

## Endpoints

| Method | Path       | Purpose                                        |
| ------ | ---------- | ---------------------------------------------- |
| GET    | `/healthz` | Liveness probe. Returns `{"status":"ok"}`.     |
| GET    | `/readyz`  | Readiness probe. 200 when ready, 503 if not.   |

The contract matches the other HTTP services in this repo
(`automation-service`, `assistant-service`, `admin-dashboard-api`).

## Standalone build & run

The service can be built and run on its own, without the full Compose
stack:

```bash
# from this directory (services/task-intake-service)
docker build -t task-intake-service:local .

cp .env.example .env
docker run --env-file .env -p 8083:8083 task-intake-service:local
```

Then verify:

```bash
curl -fsS http://localhost:8083/healthz
curl -fsS http://localhost:8083/readyz
```

When running outside Compose, dependency hostnames in `.env` (Postgres,
Temporal, MCP, Firecrawl) need to point at reachable endpoints; otherwise
`/readyz` will eventually return 503 once real probes are implemented.

## Layout

```
services/task-intake-service/
├── pyproject.toml          # python>=3.12,<3.13; fastapi, pydantic v2, temporalio, httpx
├── src/
│   ├── __init__.py
│   ├── main.py             # FastAPI app on :8083 with /healthz + /readyz
│   ├── config.py           # Pydantic Settings (env-driven)
│   ├── intake/__init__.py  # Pipeline placeholder
│   └── channels/__init__.py# Channel adapters placeholder
├── tests/
│   ├── unit/.gitkeep
│   ├── integration/.gitkeep
│   └── e2e/.gitkeep
└── README.md
```

## Environment variables

See `.env.example`. Key variables:

- `PORT=8083`
- `LOG_LEVEL=INFO`
- `POSTGRES_DSN`
- `TEMPORAL_HOST`
- `MCP_BASE_URL`
- `FIRECRAWL_BASE_URL`
- `CLIENT_SOURCE=task-intake-service`
