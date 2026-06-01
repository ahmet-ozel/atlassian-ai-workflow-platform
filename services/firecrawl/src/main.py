"""Uvicorn entry point for ``firecrawl-egress``.

The Compose stack and the Dockerfile invoke ``uvicorn src.main:app``; this
module re-exports the FastAPI application from :mod:`firecrawl.app` so the
canonical ``firecrawl.*`` package layout stays intact while the ASGI factory
remains discoverable at the conventional path.
"""

from __future__ import annotations

from firecrawl.app import app, create_app
from firecrawl.config import Settings

__all__ = ["app", "create_app", "settings"]

settings = Settings()


def main() -> None:  # pragma: no cover - manual entry point
    """Launch the wrapper with uvicorn for ad-hoc local runs."""

    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
