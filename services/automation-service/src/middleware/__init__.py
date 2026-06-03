"""Automation-service request-time guard middleware.

Exports the :mod:`license_cap` enforcement helper used by the
workflow-start path to apply per-license hard caps and the
:mod:`webhook_auth` per-department HMAC authentication middleware.
"""

from .license_cap import (
    BotLicenseCapExceededError,
    LicenseCap,
    enforce_license_cap,
    fetch_cap_for_dept,
)
from .webhook_auth import (
    DepartmentContext,
    VaultSecretReader,
    WebhookAuthMiddleware,
)

__all__ = [
    "BotLicenseCapExceededError",
    "DepartmentContext",
    "LicenseCap",
    "VaultSecretReader",
    "WebhookAuthMiddleware",
    "enforce_license_cap",
    "fetch_cap_for_dept",
]
