# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

COPY libs/vault_client/ ./libs/vault_client/

RUN pip install --no-cache-dir --prefix=/install \
    fastapi \
    httpx \
    "uvicorn[standard]" \
    ./libs/vault_client

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser \
    && mkdir -p /vault-local \
    && chown appuser:appuser /vault-local

WORKDIR /app

COPY --from=builder /install /usr/local
COPY services/outlook-mcp/src/ ./src/

USER appuser

EXPOSE 8120

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8120/healthz

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8120"]
