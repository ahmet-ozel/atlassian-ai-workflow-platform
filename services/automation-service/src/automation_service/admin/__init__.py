"""``automation_service.admin`` - admin-only endpoints.

The package owns the FastAPI surface for the ``/admin/*`` endpoints
used by ``admin-dashboard-api`` after the OIDC + RBAC pre-checks.

:func:`POST /admin/departments` performs atomic department create with
Vault staging, probe gating and a single DB transaction. This package
also holds the wizard, rotation/disable, and probe-artifact endpoints.
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
