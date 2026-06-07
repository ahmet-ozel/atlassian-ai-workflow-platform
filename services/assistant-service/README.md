# assistant-service

FastAPI HTTP service that hosts chat orchestration and LLM access for the
the platform. This is the initial service skeleton - `/healthz`
and `/readyz` are wired, business logic is intentionally absent.

- Runtime: Python 3.12, FastAPI, Pydantic v2
- Listen port: `8081`
- Default `X-Client-Source`: `assistant-service`
- Notable deps: `redis` (chat session state), `temporalio` (kept for
  SDK version parity across services even though this service does not
  start workflows), `httpx`

## Layout

```
services/assistant-service/
├── src/
│   ├── __init__.py
│   ├── main.py            # FastAPI app, /healthz + /readyz
│   ├── config.py          # Pydantic v2 Settings
│   ├── chat/__init__.py   # placeholder
│   └── llm/__init__.py    # placeholder
├── prompts/.gitkeep
├── tests/{unit,integration,e2e}/.gitkeep
├── pyproject.toml
└── README.md
```

## Standalone build & run

Build and run the container directly from this directory, without the
root Compose stack:

```bash
# 1. Seed environment
cp .env.example .env

# 2. Build the image
docker build -t assistant-service:dev .

# 3. Run, publishing the service port to the host
docker run --rm \
  --env-file .env \
  -p 8081:8081 \
  assistant-service:dev

# 4. Probe the health endpoints
curl -fsS http://localhost:8081/healthz   #  {"status":"ok"}
curl -fsS http://localhost:8081/readyz    #  {"status":"ready"}  (or 503 {"status":"not_ready"})
```

When external dependencies (Postgres, Redis, MCP) are not reachable,
`/readyz` returns `503` with `{"status":"not_ready"}` while `/healthz`
keeps returning `200`. The process does not crash on dependency loss
for local smoke testing.

## Local dev (without Docker)

```bash
python -m venv .venv && . .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
python -m src.main
```
