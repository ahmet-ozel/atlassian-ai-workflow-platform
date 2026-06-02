# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# admin-dashboard-api — multi-stage Dockerfile (HTTP service on :8082)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of services/).
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Copy local libs that pyproject.toml references
COPY libs/auth-shared/ ./libs/auth-shared/
COPY libs/http-shared/ ./libs/http-shared/
COPY libs/observability/ ./libs/observability/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/git-shared/ ./libs/git-shared/
COPY libs/prompts/ ./libs/prompts/
COPY libs/db-shared/ ./libs/db-shared/
COPY libs/vault_client/ ./libs/vault_client/
COPY libs/temporal-shared/ ./libs/temporal-shared/

# Copy the service source
COPY services/admin-dashboard-api/pyproject.toml services/admin-dashboard-api/README.md ./
COPY services/admin-dashboard-api/src/ ./src/

# Fix local lib paths in pyproject.toml to point to /build/libs/
RUN sed -i 's|file:../../libs/|file:./libs/|g' pyproject.toml \
    && sed -i 's|file:\.\./observability|file:./libs/observability|g' libs/http-shared/pyproject.toml

RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git docker-cli docker-compose \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m -G root appuser

WORKDIR /app

COPY --from=builder /install /usr/local

COPY services/admin-dashboard-api/src/ ./src/

USER appuser

EXPOSE 8082

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8082/healthz

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8082"]
