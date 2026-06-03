"""db-shared: tenant-aware database session primitives and shared data models.

Re-exports the public API of the package so callers can simply do::

    from db_shared import with_dept_session, bind_actor

For data models and enums::

    from db_shared import WorkflowStep, StepStatus, ActionType

For config validation::

    from db_shared import validate_departments_config, ConfigValidationError

The legacy :class:`TenantAwareSession` placeholder is also re-exported for backward
compatibility (``libs/db-shared/README.md`` references it directly).
"""

from .bot_identity import (
    BOT_IDENTITY_SERVICES,
    BotAccountIdConflict,
    BotAccountIdConflictError,
    validate_bot_account_id_uniqueness,
    validate_bot_account_id_uniqueness_from_file,
)
from .config_validator import (
    ConfigValidationError,
    load_and_validate_departments,
    validate_department_entry,
    validate_departments_config,
)
from .enums import ActionStatus, ActionType, ApprovalEventType, StepStatus
from .migrations import (
    AppliedMigration,
    ChecksumMismatch,
    MigrationError,
    MigrationResult,
    apply_migrations,
    discover_migrations,
)
from .secret_guard import (
    DEV_SECRET_SENTINELS,
    DevSecretDetectedError,
    detect_dev_secrets,
    enforce_no_dev_secrets,
)
from .models import (
    ApprovalEvent,
    Base,
    DiskQuotaWarning,
    OutputActionLog,
    SSHHealthcheckLog,
    WorkflowStep,
)
from .session import (
    ALLOWED_ROLES,
    AsyncConnection,
    AuthContext,
    TenantAwareSession,
    bind_actor,
    with_actor_session,
    with_dept_session,
)

__all__ = [
    # Bot identity uniqueness
    "BOT_IDENTITY_SERVICES",
    "BotAccountIdConflict",
    "BotAccountIdConflictError",
    "validate_bot_account_id_uniqueness",
    "validate_bot_account_id_uniqueness_from_file",
    # Config validation
    "ConfigValidationError",
    "load_and_validate_departments",
    "validate_department_entry",
    "validate_departments_config",
    # Enums
    "ActionStatus",
    "ActionType",
    "ApprovalEventType",
    "StepStatus",
    # SQL migration runner (K1, Y5)
    "AppliedMigration",
    "ChecksumMismatch",
    "MigrationError",
    "MigrationResult",
    "apply_migrations",
    "discover_migrations",
    # Dev-secret boot guard (Y1)
    "DEV_SECRET_SENTINELS",
    "DevSecretDetectedError",
    "detect_dev_secrets",
    "enforce_no_dev_secrets",
    # Models
    "ApprovalEvent",
    "Base",
    "DiskQuotaWarning",
    "OutputActionLog",
    "SSHHealthcheckLog",
    "WorkflowStep",
    # Session helpers
    "ALLOWED_ROLES",
    "AsyncConnection",
    "AuthContext",
    "TenantAwareSession",
    "bind_actor",
    "with_actor_session",
    "with_dept_session",
]
