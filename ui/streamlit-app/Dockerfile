# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# streamlit-app — multi-stage Dockerfile (UI component on :8501)
# ---------------------------------------------------------------------------
# Stage 1 (builder): install Streamlit + scaffold deps from requirements.txt
# into an isolated /install prefix so the runtime stage gets a single layer
# of artifacts.
# Stage 2 (runtime): minimal python:3.12-slim with curl for HEALTHCHECK and
# a non-root appuser (uid 10001) per Requirement 9.5 / design §5.5.
#
# Build context = this directory (ui/streamlit-app/). No `COPY ../...`
# directives — Standalone Mode (Property 12.3) requires that
# `docker build .` succeeds from inside the Component folder without parent
# traversal.
#
# Healthcheck choice
# ------------------
# The scaffold uses `/_stcore/health`, which is Streamlit's *built-in* HTTP
# health endpoint (served by Tornado on the same port as the app). The task
# description references `/healthz` for symmetry with FastAPI services, but
# Streamlit does not expose `/healthz`; documenting the chosen path here
# avoids an out-of-band reverse proxy. See:
# https://docs.streamlit.io/develop/concepts/configuration/options
# ---------------------------------------------------------------------------

FROM python:3.12-slim AS builder

WORKDIR /build

RUN python -m pip install --no-cache-dir --upgrade pip

# Pull the full dependency set declared by the Streamlit app.
COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------

FROM python:3.12-slim AS runtime

# curl is required by the Dockerfile HEALTHCHECK below.
# ca-certificates keeps outbound HTTPS (httpx) working against real endpoints.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 10001 -m appuser

WORKDIR /app

# Bring over installed site-packages from the builder.
COPY --from=builder /install /usr/local

# Application source (entry, page modules, helpers, config). Tests and
# environment files are excluded via .dockerignore.
#
# ``components/`` MUST be copied — every multipage module under
# ``pages/`` imports it (``from components import ...``); omitting it
# made each page crash at import with ``ModuleNotFoundError: No module
# named 'components'``.
COPY app.py chat_mcp.py chat_planner.py chat_runtime.py config.py mcp_client.py ./
COPY pages/ ./pages/
COPY config/ ./config/
COPY .streamlit/ ./.streamlit/
COPY components/ ./components/

USER appuser

EXPOSE 8501

# Streamlit health endpoint: `/_stcore/health` (see header comment).
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
