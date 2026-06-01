# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# firecrawl — egress-allowlisted scrape/search wrapper (HTTP service on :3002)
# ---------------------------------------------------------------------------
# Stage 1 (builder): install the wrapper package + transitive deps into a
# clean prefix so the runtime stage copies a single layer of artefacts.
# Stage 2 (runtime): minimal python:3.12-slim with curl for HEALTHCHECK and
# a non-root appuser (uid 10001) per Requirement 9.5.
#
# Build context = this directory. No `COPY ..` directives — the wrapper is
# self-contained and does NOT depend on the workspace ``libs/`` packages.
# That keeps the build deterministic and lets the egress filter run as a
# self-contained sidecar in any environment that needs it.
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# README.md is referenced from pyproject.toml (`readme = "README.md"`) and
# must therefore be present at install time.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

# curl: needed by the Dockerfile HEALTHCHECK directive below.
# ca-certificates: needed by ``httpx`` for HTTPS to allow-listed targets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

COPY --from=builder /install /usr/local

# uvicorn is the runtime server but is not declared in pyproject.toml's
# main dependency list; install it explicitly so the CMD resolves.
RUN pip install --no-cache-dir "uvicorn[standard]"

COPY src/ ./src/

USER appuser

EXPOSE 3002

# Healthcheck shape matches the Compose-level invariant
# (interval ∈ [5s, 30s], retries ≤ 3, timeout < interval — Property 4.4).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:3002/healthz || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "3002"]
