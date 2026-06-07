"""``automation_service`` package - FastAPI HTTP gateway.

The FastAPI app and its routes live under
``services/automation-service/src/automation_service/``. Webhook
handlers, ``/admin/*`` endpoint ownership, the probe runner and the
credential resolver live under this same package root.

The legacy ``src.main`` and ``src.config`` modules continue to exist as
thin re-exports so the Dockerfile
(``CMD ["uvicorn", "src.main:app", ...]``) and the existing webhook /
decision tests keep working without modification.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .app import app, create_app

__all__ = ["app", "create_app", "__version__"]
