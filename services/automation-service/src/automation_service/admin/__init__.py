"""``automation_service.admin`` — admin-only endpoints (R3, R5.4, R7.6, R10.10).

The package owns the FastAPI surface for the ``/admin/*`` endpoints
listed in ``platform-mimari-foundation/design.md``
§"automation-service HTTP API". ``admin-dashboard-api`` proxies into
these routes after the OIDC + RBAC pre-checks (R3.5).

Task 5.3 ships :func:`POST /admin/departments` here — atomic
department create with Vault staging, probe gating and a single DB
transaction. Sibling tasks (5.4–5.6) extend this package with the
wizard, rotation/disable, and probe-artifact endpoints.
"""

from __future__ import annotations

from .dept_create import (
    DepartmentAlreadyExistsError,
    DepartmentCreateOrchestrator,
    DepartmentCreateRequest,
    DepartmentCreateResult,
    StagingFailureError,
)
from .router import router

__all__ = [
    "DepartmentAlreadyExistsError",
    "DepartmentCreateOrchestrator",
    "DepartmentCreateRequest",
    "DepartmentCreateResult",
    "StagingFailureError",
    "router",
]
