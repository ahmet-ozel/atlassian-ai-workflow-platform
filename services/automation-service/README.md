# automation-service

FastAPI HTTP service skeleton for the platform. Listens on
port `8080` and exposes the standard liveness/readiness contract from
the expected HTTP service contract:

- `GET /healthz` → `200` with body `{"status": "ok"}`.
- `GET /readyz` → `200` with `{"status": "ready"}` when
  `Settings.dependencies_reachable()` returns `True`; `503` with
  `{"status": "not_ready"}` otherwise. The 503 body is ≤ 64 bytes and
  parseable as JSON whose only top-level key is `status`.

Real automation business logic (Atlassian webhooks, Temporal workflow
start, decision engine) lives behind the placeholder modules
`src/webhooks/`, `src/temporal_client.py`, `src/decision/` and will be
implemented by later

## Standalone build & run

The service runs in **Standalone Mode** without the rest of the Compose
stack. Build the image and run it directly from this
directory:

```bash
# from services/automation-service/
docker build -t automation-service:local .

cp .env.example .env
docker run --rm --env-file .env -p 8080:8080 automation-service:local
```

Once the container is up:

```bash
curl -fsS http://localhost:8080/healthz
# {"status":"ok"}

curl -fsS http://localhost:8080/readyz
# {"status":"ready"}     # while Settings.dependencies_reachable() returns True
```

If downstream services (Postgres, Vault, Temporal, atlassian-mcp) are
unreachable, `/readyz` returns `503` with body `{"status":"not_ready"}`
but the process keeps running so the orchestrator can retry.

## Local development without Docker

```bash
# from services/automation-service/
python -m venv .venv
. .venv/Scripts/activate            # Windows; use bin/activate on Unix
python -m pip install -e .
python -m pip install uvicorn pytest httpx
uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

## Project layout

```
services/automation-service/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app + /healthz + /readyz
│   ├── config.py            # Pydantic v2 Settings
│   ├── webhooks/            # placeholder — Atlassian webhook routes
│   ├── decision/            # placeholder — event → workflow_type routing
│   └── temporal_client.py   # placeholder — Temporal client factory
├── migrations/              # placeholder — Alembic / SQL migrations
├── tests/{unit,integration,e2e}/
├── pyproject.toml
└── .env.example             # local environment template
```
