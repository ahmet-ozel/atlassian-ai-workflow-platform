"""Legacy uvicorn entry point — re-exports the canonical FastAPI app.

The canonical FastAPI application now lives under
:mod:`automation_service.app` per the platform-mimari-foundation design
(§"Komponent Sahipliği Özeti", task 5.1). This module is kept as a thin
re-export so that:

* the production ``Dockerfile`` keeps working unchanged
  (``CMD ["uvicorn", "src.main:app", ...]``);
* the existing decision / webhook test suite that imports from
  ``src.webhooks`` and ``src.decision`` keeps the same package layout;
* the multi-service-scaffold ``Settings`` continues to drive the
  readiness probe.

New code should import from ``automation_service.app`` directly.
"""

from __future__ import annotations

from automation_service.app import app, create_app

from .config import Settings

# Re-export ``settings`` for any consumer that imported it from the
# legacy module (e.g. the multi-service-scaffold integration tests).
settings = Settings()

__all__ = ["app", "create_app", "settings"]


def main() -> None:
    """Launch the service with uvicorn (used by Standalone Mode runs).

    The Compose stack and the Dockerfile invoke ``uvicorn`` directly
    via ``CMD ["uvicorn", "src.main:app", ...]`` so this helper is
    only used for ad-hoc local runs (``python -m src.main``).
    """

    import uvicorn

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover - manual entry
    main()
