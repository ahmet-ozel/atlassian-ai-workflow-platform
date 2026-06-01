# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# assistant-service — multi-stage Dockerfile (HTTP service on :8081)
# ---------------------------------------------------------------------------
# Build context = platform root. The service's chat handler and lifespan
# probe several internal libs at runtime; they are copied and installed
# alongside the service so imports resolve and ``/healthz`` returns 200.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Every internal lib the assistant-service touches at runtime.
COPY libs/http-shared/ ./libs/http-shared/
COPY libs/observability/ ./libs/observability/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/auth-shared/ ./libs/auth-shared/
COPY libs/db-shared/ ./libs/db-shared/
COPY libs/vault_client/ ./libs/vault_client/
COPY libs/mcp_client/ ./libs/mcp_client/
COPY libs/temporal-shared/ ./libs/temporal-shared/
COPY libs/prompts/ ./libs/prompts/
COPY libs/llm-orchestrator/ ./libs/llm-orchestrator/
COPY libs/pii-shared/ ./libs/pii-shared/
COPY libs/messages/ ./libs/messages/

# Service source.
COPY services/assistant-service/pyproject.toml services/assistant-service/README.md ./
COPY services/assistant-service/src/ ./src/

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
    ./libs/llm-orchestrator \
    ./libs/pii-shared \
    ./libs/messages

# Strip the service's file: refs and install (external deps land here).
RUN sed -i '/@.*file:/d' pyproject.toml \
    && pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY services/assistant-service/src/ ./src/
COPY prompts/ ./prompts/

USER appuser

ENV PYTHONPATH=/app/src

EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8081/healthz

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8081"]
