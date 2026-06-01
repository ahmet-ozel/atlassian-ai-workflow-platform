# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# automation-worker — multi-stage Dockerfile (Temporal worker, no HTTP port)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of workers/). Local libs (observability
# and friends referenced via ``@ file:../../libs/...`` in pyproject.toml) are
# copied into the build image and installed BEFORE the worker so pip's
# direct-file references resolve.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Copy every lib the automation-worker depends on (transitively).
COPY libs/observability/ ./libs/observability/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/auth-shared/ ./libs/auth-shared/
COPY libs/db-shared/ ./libs/db-shared/
COPY libs/vault_client/ ./libs/vault_client/
COPY libs/mcp_client/ ./libs/mcp_client/
COPY libs/temporal-shared/ ./libs/temporal-shared/
COPY libs/http-shared/ ./libs/http-shared/
COPY libs/prompts/ ./libs/prompts/
COPY libs/git-shared/ ./libs/git-shared/
COPY libs/notification/ ./libs/notification/
COPY libs/llm-orchestrator/ ./libs/llm-orchestrator/
COPY libs/messages/ ./libs/messages/

# Worker source.
COPY workers/automation-worker/pyproject.toml workers/automation-worker/README.md ./
COPY workers/automation-worker/src/ ./src/

# Strip cross-lib file: references so pip installs each lib independently.
RUN find libs/ -name "pyproject.toml" -exec sed -i '/@.*file:/d' {} \;

# Install libs in dependency order.
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
    ./libs/notification \
    ./libs/llm-orchestrator \
    ./libs/messages

# Strip the worker's file: references and install (external deps land here).
RUN sed -i '/@.*file:/d' pyproject.toml \
    && pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY workers/automation-worker/src/ ./src/
COPY workers/agent-runner-worker/prompts/ ./workers/agent-runner-worker/prompts/

USER appuser

ENV PYTHONPATH=/app/src

HEALTHCHECK CMD python -c "import asyncio,os; from temporalio.client import Client; asyncio.run(Client.connect(os.environ['TEMPORAL_HOST']))"

CMD ["automation-worker"]
