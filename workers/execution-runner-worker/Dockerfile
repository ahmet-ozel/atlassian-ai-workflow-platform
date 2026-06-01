# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# execution-runner-worker — multi-stage Dockerfile (Temporal worker, no HTTP)
# ---------------------------------------------------------------------------
# Build context = platform root (parent of workers/). Although the
# worker's pyproject.toml does not declare ``temporal-shared`` as a
# dependency, the runtime code imports it heavily, so we install the
# lib alongside the worker package.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Copy the shared libs the worker imports at runtime.
COPY libs/temporal-shared/ ./libs/temporal-shared/
COPY libs/observability/ ./libs/observability/
COPY libs/audit_logger/ ./libs/audit_logger/
COPY libs/http-shared/ ./libs/http-shared/

# Worker source.
COPY workers/execution-runner-worker/pyproject.toml \
     workers/execution-runner-worker/README.md ./
COPY workers/execution-runner-worker/src/ ./src/

# Strip cross-lib file: references so pip installs each lib independently.
RUN find libs/ -name "pyproject.toml" -exec sed -i '/@.*file:/d' {} \;
RUN sed -i 's|file:../../libs/|file:./libs/|g' pyproject.toml

# Install libs first.
RUN pip install --no-cache-dir --prefix=/install \
    ./libs/observability \
    ./libs/audit_logger \
    ./libs/http-shared \
    ./libs/temporal-shared

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

COPY workers/execution-runner-worker/src/ ./src/

USER appuser

ENV PYTHONPATH=/app/src

HEALTHCHECK CMD python -c "import asyncio,os; from temporalio.client import Client; asyncio.run(Client.connect(os.environ['TEMPORAL_HOST']))"

CMD ["python", "-m", "src.main"]
