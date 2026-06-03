# execution-runner-worker

Temporal worker that hosts the `ExecutionRunWorkflow` and its supporting
SSH / Docker / Vault / MinIO activities (see the
the worker service contract.

The worker connects to the Temporal cluster identified by the
`TEMPORAL_HOST` environment variable (default `temporal:7233`) and
subscribes to the **`execution-runner`** task queue. Workflow, activity,
and runner implementations live under `src/workflows/`, `src/activities/`,
and `src/runners/`.

Unlike `agent-runner-worker`, this worker does **not** ship a
`prompts/` directory; it executes pre-authored runner plans instead of
LLM-driven prompt steps.

## Standalone build & run

The worker runs in **Standalone Mode** without the rest of the Compose
stack. It only needs a reachable Temporal endpoint;
the other dependencies (Vault, MinIO, Postgres, SSH targets) are used by
the activities at runtime.

```bash
# from workers/execution-runner-worker/
docker build -t execution-runner-worker .

# This project uses .env files only (no .env.example). Create workers/
# execution-runner-worker/.env first, then:
docker run --rm --env-file .env execution-runner-worker
```

The container has no published ports — workers communicate exclusively
over the Temporal gRPC connection. To point the worker at a Temporal
instance running on the host machine override `TEMPORAL_HOST`:

```bash
docker run --rm \
  -e TEMPORAL_HOST=host.docker.internal:7233 \
  execution-runner-worker
```

If the Temporal connection cannot be established the process exits
with a non-zero status code so the orchestrator (Compose / supervisor)
can restart it.

## Local development without Docker

```bash
# from workers/execution-runner-worker/
python -m venv .venv
. .venv/Scripts/activate            # Windows; use bin/activate on Unix
python -m pip install -e .
python -m pip install pytest

# requires a reachable Temporal frontend; defaults to temporal:7233
export TEMPORAL_HOST=localhost:7233
python -m src.main
```

## Project layout

```
workers/execution-runner-worker/
├── src/
│   ├── __init__.py
│   ├── main.py                          # asyncio entrypoint, task_queue=execution-runner
│   ├── workflows/
│   │   ├── __init__.py
│   │   └── execution_run_workflow.py    # placeholder
│   ├── activities/
│   │   ├── __init__.py
│   │   ├── ssh.py                       # placeholder
│   │   ├── docker.py                    # placeholder
│   │   ├── vault.py                     # placeholder
│   │   └── minio.py                     # placeholder
│   └── runners/
│       ├── __init__.py
│       ├── local_docker.py              # placeholder
│       ├── remote_ssh.py                # placeholder
│       ├── remote_ssh_docker.py         # placeholder
│       └── noop.py                      # placeholder
├── tests/{unit,integration,e2e}/        # .gitkeep only
├── pyproject.toml
└── .env                                 # service-local env (git-ignored)
```
