"""FastAPI entry point for task-intake-service.

Exposes the standard ``/healthz`` (liveness) and ``/readyz`` (readiness)
endpoints used across all HTTP services in this stack. Listens on port
8083 by default.

The service is **profile-gated** in Compose (``profiles: ["task-intake"]``)
and is therefore not started by ``docker compose up -d`` unless the
``task-intake`` profile is explicitly enabled.
"""

from __future__ import annotations

from fastapi import FastAPI, Response

from .config import Settings

settings = Settings()

app = FastAPI(
    title="task-intake-service",
    version="0.0.0-scaffold",
    description=(
        "Multi-channel task intake (email, Slack, web form). Profile-gated "
        "scaffold; business logic not yet implemented."
    ),
)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness probe. Always returns 200 while the process is up."""

    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict[str, str]:
    """Readiness probe. Returns 503 when dependencies are not reachable.

    The 503 response body is intentionally minimal (≤64 bytes) so it can be
    consumed by Compose / Kubernetes probes without leaking diagnostic
    detail. Detailed errors should be written to application logs.
    """

    if not settings.dependencies_reachable():
        response.status_code = 503
        return {"status": "not_ready"}
    return {"status": "ready"}


if __name__ == "__main__":  # pragma: no cover - convenience local entry point
    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
