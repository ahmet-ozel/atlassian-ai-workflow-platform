# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# agent-runner-worker — multi-stage Dockerfile (Temporal worker, no HTTP port)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of workers/). The worker's
# activity modules import several shared libs (http_shared,
# observability, audit_logger, temporal_shared, mcp_client, …) that
# are NOT declared in pyproject.toml, so we copy them into the image
# and install alongside the worker.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Copy every internal lib the worker's activity modules import.
COPY libs/http-shared/ ./libs/http-shared/
COPY libs/observability/ ./libs/observability/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/auth-shared/ ./libs/auth-shared/
COPY libs/db-shared/ ./libs/db-shared/
COPY libs/temporal-shared/ ./libs/temporal-shared/
COPY libs/vault_client/ ./libs/vault_client/
COPY libs/mcp_client/ ./libs/mcp_client/
COPY libs/prompts/ ./libs/prompts/
COPY libs/git-shared/ ./libs/git-shared/
COPY libs/llm-orchestrator/ ./libs/llm-orchestrator/
COPY libs/cost-tracking/ ./libs/cost-tracking/
COPY libs/notification/ ./libs/notification/
COPY libs/pii-shared/ ./libs/pii-shared/
COPY libs/messages/ ./libs/messages/

# Worker source + prompts.
COPY workers/agent-runner-worker/pyproject.toml \
     workers/agent-runner-worker/README.md ./
COPY workers/agent-runner-worker/src/ ./src/
COPY workers/agent-runner-worker/prompts/ ./prompts/

# Strip cross-lib file: references so pip can install each lib in turn.
RUN find libs/ -name "pyproject.toml" -exec sed -i '/@.*file:/d' {} \;

# Install libs first.
RUN pip install --no-cache-dir --prefix=/install \
    ./libs/observability \
    ./libs/audit_logger \
    ./libs/auth-shared \
    ./libs/db-shared \
    ./libs/vault_client \
    ./libs/mcp_client \
    ./libs/temporal-shared \
    ./libs/http-shared \
    ./libs/prompts \
    ./libs/git-shared \
    ./libs/llm-orchestrator \
    ./libs/cost-tracking \
    ./libs/notification \
    ./libs/pii-shared \
    ./libs/messages

# Install the worker (external deps land here).
RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY workers/agent-runner-worker/src/ ./src/
COPY workers/agent-runner-worker/prompts/ ./prompts/

USER appuser

# ``agent_runner.main._load_activities`` imports activity modules as
# ``from src.activities.jira import ...`` — that requires ``/app`` on
# PYTHONPATH so ``src`` resolves as a top-level package (not
# ``/app/src`` which would make ``activities`` the package root and
# break the dotted import).
ENV PYTHONPATH=/app

HEALTHCHECK CMD python -c "import asyncio,os; from temporalio.client import Client; asyncio.run(Client.connect(os.environ['TEMPORAL_HOST']))"

# Use the production ``agent_runner.main`` entrypoint (which loads and
# registers every activity from src.activities.*) rather than the legacy
# ``src/main.py`` that ships ``activities=[]``. The console
# script ``agent-runner-worker`` is installed by pyproject.toml and
# resolves to ``agent_runner.main:run``.
CMD ["agent-runner-worker"]
