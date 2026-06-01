# execution-runner-worker

Temporal worker skeleton for the multi-service scaffold. Hosts the
`ExecutionRunWorkflow` and the supporting SSH / Docker / Vault / MinIO
activities described in the
[design](../../.kiro/specs/multi-service-scaffold/design.md) §3.2.

The worker connects to the Temporal cluster identified by the
`TEMPORAL_HOST` environment variable (default `temporal:7233`) and
subscribes to the **`execution-runner`** task queue. Concrete workflow,
activity, and runner implementations are stubbed out under
`src/workflows/`, `src/activities/`, and `src/runners/` (each module
contains a single `# TODO: implement` marker) and will be filled in by
later specs.

Unlike `agent-runner-worker`, this worker does **not** ship a
`prompts/` directory; it executes pre-authored runner plans instead of
LLM-driven prompt steps.

## Standalone build & run

The worker runs in **Standalone Mode** without the rest of the Compose
stack (Requirement 15). It only needs a reachable Temporal endpoint;
all other dependencies (Vault, MinIO, Postgres, SSH targets) are
exercised by the not-yet-implemented activities and may be omitted at
this scaffold stage.

```bash
# from workers/execution-runner-worker/
docker build -t execution-runner-worker:scaffold .

cp .env.example .env
docker run --rm --env-file .env execution-runner-worker:scaffold
```

The container has no published ports — workers communicate exclusively
over the Temporal gRPC connection. To point the worker at a Temporal
instance running on the host machine override `TEMPORAL_HOST`:

```bash
docker run --rm \
  -e TEMPORAL_HOST=host.docker.internal:7233 \
  execution-runner-worker:scaffold
```

If the Temporal connection cannot be established the process exits
with a non-zero status code so the orchestrator (Compose / supervisor)
can restart it (Requirement 3.7).

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
└── .env.example                         # added in task 7.2
```
