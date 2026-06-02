# agent-runner-worker

Temporal worker that hosts the `AgentRunnerWorkflow` and its supporting
activities (Jira, Bitbucket, Confluence, LLM, artifact, opencode) and
subscribes to the `agent-runner` Temporal task queue.

The worker does **not** publish any TCP port: it only opens an outgoing
connection to the Temporal cluster pointed at by `TEMPORAL_HOST`
(default `temporal:7233`). If the connection cannot be established the
process exits with a non-zero status code so that the orchestrator can
restart it (Requirement 3.7).

Prompt files under `prompts/` drive the LLM activities; workflow and
activity bodies live in `src/workflows/` and `src/activities/`.

## Standalone build & run

The worker runs in **Standalone Mode** without the rest of the Compose
stack (Requirement 15), provided a Temporal cluster is reachable at the
configured `TEMPORAL_HOST`. Build the image and run it directly from
this directory:

```bash
# from workers/agent-runner-worker/
docker build -t agent-runner-worker .

# This project uses .env files only (no .env.example). Create workers/
# agent-runner-worker/.env first, then:
docker run --rm --env-file .env agent-runner-worker
```

To point the worker at a Temporal cluster running on the host machine,
override `TEMPORAL_HOST` on the command line:

```bash
docker run --rm \
    -e TEMPORAL_HOST=host.docker.internal:7233 \
    -e TEMPORAL_TASK_QUEUE=agent-runner \
    agent-runner-worker
```

The container's `HEALTHCHECK` performs a one-shot Temporal client
`connect()` against `TEMPORAL_HOST`; the worker is reported healthy once
the cluster is reachable.

## Local development without Docker

```bash
# from workers/agent-runner-worker/
python -m venv .venv
. .venv/Scripts/activate            # Windows; use bin/activate on Unix
python -m pip install -e .
TEMPORAL_HOST=localhost:7233 python -m src.main
```

## Project layout

```
workers/agent-runner-worker/
├── src/
│   ├── __init__.py
│   ├── main.py                       # asyncio entry point + Worker
│   ├── workflows/
│   │   ├── __init__.py
│   │   └── agent_runner_workflow.py  # placeholder workflow class
│   └── activities/
│       ├── __init__.py
│       ├── jira.py
│       ├── bitbucket.py
│       ├── confluence.py
│       ├── llm.py
│       ├── artifact.py
│       └── opencode.py
├── prompts/
│   ├── task_analysis.md
│   ├── code_generation.md
│   ├── pr_description.md
│   ├── pr_review.md
│   ├── pr_review_brief.md
│   ├── doc_generation.md
│   ├── research.md
│   ├── error_notification.md
│   └── pdf_templates/.gitkeep
├── tests/{unit,integration,e2e}/
├── pyproject.toml
└── README.md
```
