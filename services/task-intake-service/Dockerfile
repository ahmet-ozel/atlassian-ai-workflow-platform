# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# task-intake-service — multi-stage Dockerfile (HTTP service on :8083)
# ---------------------------------------------------------------------------
# Stage 1 (builder): install the package and its dependencies into a clean
# prefix so the runtime stage can copy a single layer of artifacts.
# Stage 2 (runtime): minimal python:3.12-slim with curl for HEALTHCHECK and
# a non-root appuser (uid 10001) per Requirement 9.5.
#
# Build context = this directory. No `COPY ..` directives — Standalone Mode
# (Property 12.3) requires that `docker build .` works from inside the
# Component folder without parent traversal.
#
# Note: this service is profile-gated in Compose (`profiles: ["task-intake"]`)
# and therefore not started by the default `up -d` flow.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Setuptools backend reads `readme = "README.md"` from pyproject.toml; ship it
# alongside the metadata so installation succeeds.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

# uvicorn is only declared in the `dev` extra of this service's
# pyproject.toml, so add it explicitly to the runtime stage. curl is needed
# by the HEALTHCHECK directive below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY --from=builder /install /usr/local

RUN pip install --no-cache-dir "uvicorn[standard]"

COPY src/ ./src/

USER appuser

EXPOSE 8083

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8083/healthz

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8083"]
