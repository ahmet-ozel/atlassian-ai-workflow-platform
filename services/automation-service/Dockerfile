# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# automation-service — multi-stage Dockerfile (HTTP service on :8080)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of services/).
# All internal libs are copied and installed so imports resolve at runtime.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Copy ALL internal libs (automation-service uses most of them)
COPY libs/observability/ ./libs/observability/
COPY libs/http-shared/ ./libs/http-shared/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/auth-shared/ ./libs/auth-shared/
COPY libs/temporal-shared/ ./libs/temporal-shared/
COPY libs/db-shared/ ./libs/db-shared/
COPY libs/vault_client/ ./libs/vault_client/
COPY libs/mcp_client/ ./libs/mcp_client/
COPY libs/prompts/ ./libs/prompts/
COPY libs/git-shared/ ./libs/git-shared/

# Copy the service source
COPY services/automation-service/pyproject.toml services/automation-service/README.md ./
COPY services/automation-service/src/ ./src/

# Strip file: references from ALL lib pyproject.toml files
RUN find libs/ -name "pyproject.toml" -exec sed -i '/@.*file:/d' {} \;

# Install all libs (they have no more file: cross-refs after sed)
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
    ./libs/git-shared

# Strip file: references from service pyproject.toml and install WITH deps
# (internal libs already installed, pip will skip them; external deps like fastapi will be installed)
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

RUN pip install --no-cache-dir "uvicorn[standard]" "python-multipart" "structlog"

COPY services/automation-service/src/ ./src/

USER appuser

ENV PYTHONPATH=/app/src

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
