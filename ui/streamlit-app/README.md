# streamlit-app

Streamlit-based user-facing UI for the multi-service scaffold. Provides
the chat, task creator, explorer, workflows, orphan-branches and PO
review inbox surfaces backed by `assistant-service` and `atlassian-mcp`
(see `MIMARI.md` §2 and the multi-service-scaffold spec, Requirement 4).

This package is a scaffold: `app.py` only renders a placeholder title
and every page under `pages/` is a `# TODO: implement page` stub.

## Layout

```
ui/streamlit-app/
├── app.py                     # Streamlit entrypoint, listens on :8501
├── pages/                     # Multi-page navigation entries
│   ├── 1_chat.py
│   ├── 2_task_creator.py
│   ├── 3_explorer.py
│   ├── 4_workflows.py
│   ├── 5_orphan_branches.py
│   └── 6_po_review_inbox.py
├── config.py                  # Runtime settings loader (placeholder)
├── mcp_client.py              # libs/http-shared wrapper (placeholder)
├── config/
│   └── quick_actions.yaml     # Quick-action presets, currently empty (`[]`)
├── requirements.txt           # streamlit, httpx, pyyaml
└── README.md
```

The `config.py` module (Pydantic Settings loader) and the
`config/quick_actions.yaml` file coexist by design: the former is
imported as a Python module, the latter is a YAML data file consumed at
runtime.

## Standalone build & run

The container image and `Dockerfile` arrive in task 6.3; until then the
app runs straight from a local Python environment.

```bash
# from ui/streamlit-app/
python -m venv .venv
. .venv/Scripts/activate        # Windows; use bin/activate on Unix
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# Streamlit binds to its default port 8501.
streamlit run app.py --server.port=8501 --server.address=0.0.0.0
```

Once task 6.3 lands the same component will also be runnable as a
container:

```bash
# from ui/streamlit-app/ (after task 6.3 adds the Dockerfile)
docker build -t streamlit-app .
cp .env.example .env            # provided by task 7.3
docker run --rm --env-file .env -p 8501:8501 streamlit-app
```

## Configuration

Runtime settings come from environment variables. The full list lands
with task 7.3 in `.env.example`; the Streamlit app reads at minimum:

- `PORT` — defaults to `8501` (Streamlit default).
- `LOG_LEVEL` — defaults to `INFO`.
- `ASSISTANT_BASE_URL` — base URL of `assistant-service`
  (e.g. `http://assistant-service:8081`).
- `MCP_BASE_URL` — base URL of `atlassian-mcp`
  (e.g. `http://atlassian-mcp:8090`).
- `CLIENT_SOURCE` — optional override; defaults to `streamlit-app`
  (see Requirement 13 and `libs/http-shared`).
