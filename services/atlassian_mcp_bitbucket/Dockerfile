# syntax=docker/dockerfile:1.7

ARG MCP_ATLASSIAN_REPO=https://github.com/jellythomas/mcp-atlassian-with-bitbucket.git
ARG MCP_ATLASSIAN_REF=9face8350494dff9c0daf0c04754721f0ecf3848

FROM alpine/git:2.49.1 AS source
ARG MCP_ATLASSIAN_REPO
ARG MCP_ATLASSIAN_REF
WORKDIR /src
COPY compat_headers.patch /tmp/compat_headers.patch
RUN git init . \
    && git remote add origin "${MCP_ATLASSIAN_REPO}" \
    && git fetch --depth 1 origin "${MCP_ATLASSIAN_REF}" \
    && git checkout FETCH_HEAD \
    && git apply /tmp/compat_headers.patch \
    && rm -rf .git

FROM ghcr.io/astral-sh/uv:python3.13-alpine AS uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY --from=source /src /app
RUN uv lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --no-editable
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable
RUN find /app/.venv -name '__pycache__' -type d -exec rm -rf {} + \
    && find /app/.venv -name '*.pyc' -delete \
    && find /app/.venv -name '*.pyo' -delete

FROM python:3.13-alpine
RUN adduser -D -h /home/app -s /bin/sh app
WORKDIR /app
USER app

COPY --from=uv --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["mcp-atlassian"]
