# streamlit-app

The end-user Streamlit UI. It provides the per-session **Credentials**,
**Chat** and **Task Creator** surfaces, backed by `assistant-service` and
`atlassian-mcp`.

Governance surfaces (Workflows, PO Review, Orphan Branches) intentionally live
in the **admin dashboard** (admin-gated), not here — every user who opens
Streamlit must not see operator-only controls.

The admin-only MCP debug pages (`3_explorer`, `7_mcp_inspector`) are reachable
from the admin dashboard's "Debugging" navigation group, not from the normal
end-user menu.

## Layout

```
ui/streamlit-app/
├── app.py                     # Streamlit entrypoint (binds to container port 8501)
├── pages/
│   ├── 0_credentials.py       # per-session Jira/Confluence/Bitbucket credentials
│   ├── 1_chat.py              # chat → MCP, formatted by the configured LLM
│   ├── 2_task_creator.py      # Jira task-description drafting assistant
│   ├── 3_explorer.py          # (admin-debug) read-only MCP explorer
│   └── 7_mcp_inspector.py     # (admin-debug) MCP inspector
├── chat_runtime.py            # chat → plan → MCP call → LLM summary
├── chat_planner.py            # intent/plan extraction (Confluence space-key, etc.)
├── chat_mcp.py                # MCP client wiring
├── config.py                  # Pydantic settings loader
├── mcp_client.py              # libs/http-shared wrapper
├── components/                # dept_switcher, credential_manager, theme, …
├── config/quick_actions.yaml  # quick-action presets
├── requirements.txt           # streamlit, httpx, pyyaml, …
├── Dockerfile
└── README.md
```

## Standalone build & run

```bash
# from ui/streamlit-app/
python -m venv .venv
. .venv/Scripts/activate        # Windows; use bin/activate on Unix
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Streamlit binds to its container port 8501.
streamlit run app.py --server.port=8501 --server.address=0.0.0.0
```

In the Compose stack the app is published on the host at
**`http://localhost:38501`** (`STREAMLIT_HOST_PORT`). As a container:

```bash
docker build -t streamlit-app .
docker run --rm --env-file .env -p 38501:8501 streamlit-app
```

## Configuration

Runtime settings come from environment variables (this project uses `.env`
files only — there is no `.env.example`). Key variables:

- `STREAMLIT_HOST_PORT` — host port published by Compose (default `38501`);
  the container always listens on `8501`.
- `LOG_LEVEL` — defaults to `INFO`.
- `ASSISTANT_BASE_URL` — base URL of `assistant-service`
  (e.g. `http://assistant-service:8081`, the internal container address).
- `MCP_BASE_URL` — base URL of `atlassian-mcp`
  (e.g. `http://atlassian-mcp:8090`, the internal container address).
- `LLM_PROVIDER` / `LLM_MODEL_NAME` / `OPENAI_API_KEY` — entered from the
  Dashboard Start modal, not committed to `.env`.
- `CLIENT_SOURCE` — optional override; defaults to `streamlit-app`.

> Service-to-service URLs use the internal container ports (`:8081`, `:8090`)
> and never change. Only host-published ports are configurable, via the
> `*_HOST_PORT` variables in `infra/.env`.
