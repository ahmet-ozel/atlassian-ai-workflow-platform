"""``automation_service`` package — FastAPI HTTP gateway.

This package follows the layout defined in the
``platform-mimari-foundation`` design document (§"Komponent Sahipliği
Özeti"): the FastAPI app and its routes live under
``services/automation-service/src/automation_service/``. Subsequent
tasks (5.2–5.7) add the webhook handlers, ``/admin/*`` endpoint
ownership, the probe runner and the credential resolver under this same
package root.

The legacy ``src.main`` and ``src.config`` modules from the
``multi-service-scaffold`` skeleton continue to exist as thin
re-exports so the Dockerfile (``CMD ["uvicorn", "src.main:app", ...]``)
and the existing webhook / decision tests keep working without
modification.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .app import app, create_app

__all__ = ["app", "create_app", "__version__"]
