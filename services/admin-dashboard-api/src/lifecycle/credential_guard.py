"""Credential Guard — blocks production boot when dev-default credentials are detected.

This module implements the fail-fast guard for production startup. It checks
environment variables against a configurable list of known insecure dev-default
values and prevents the service from starting in production when any match is
found.

Usage (inside the FastAPI lifespan)::

    from .lifecycle.credential_guard import check_credentials
    import os

    result = check_credentials(
        platform_env=os.environ.get("PLATFORM_ENV", ""),
        env_vars=dict(os.environ),
    )
    if result.blocked:
        app.state.credential_blocked = True
        # skip remaining lifespan steps

The ``DEV_DEFAULTS`` tuple is the single source of truth for insecure values.
Adding a new dev-default credential requires only appending a new ``DevDefault``
entry to this tuple.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DevDefault:
    """Definition of a single dev-default credential.

    Attributes:
        env_var: The environment variable name to check.
        insecure_value: The known insecure dev-only value.
        description: Human-readable description for log messages.
    """

    env_var: str
    insecure_value: str
    description: str


# Extensible configuration list for known dev-default credentials.
# To add a new dev-default credential, simply append a DevDefault entry here.
DEV_DEFAULTS: Sequence[DevDefault] = (
    DevDefault("POSTGRES_PASSWORD", "ai_dev_only", "Dev-only Postgres password"),
    DevDefault("VAULT_TOKEN", "dev-token-not-for-prod", "Dev-only Vault token"),
    DevDefault(
        "VAULT_DEV_ROOT_TOKEN_ID",
        "dev-token-not-for-prod",
        "Dev-only Vault root token (-dev mode)",
    ),
    DevDefault(
        "MINIO_ROOT_PASSWORD", "miniosecret_dev_only", "Dev-only MinIO password"
    ),
    # ``AUTH_PROVIDER=local`` accepts any non-empty bearer token, which is fine
    # for dev but a complete authentication bypass in production.
    DevDefault(
        "AUTH_PROVIDER",
        "local",
        "Dev-only AuthProvider 'local' bypasses bearer token validation",
    ),
)


@dataclass
class CredentialGuardResult:
    """Result of the credential guard check.

    Attributes:
        blocked: True when the service must not start (production + dev creds).
        violations: List of human-readable descriptions of detected insecure
            credentials.
    """

    blocked: bool = False
    violations: list[str] = field(default_factory=list)


def check_credentials(
    platform_env: str,
    env_vars: dict[str, str],
    *,
    dev_defaults: Sequence[DevDefault] = DEV_DEFAULTS,
) -> CredentialGuardResult:
    """Check environment variables against known dev-default credentials.

    When ``platform_env`` equals ``"production"`` and any credential in
    ``env_vars`` matches a value in the ``dev_defaults`` list, the function
    returns ``blocked=True`` and logs a CRITICAL message for each violation.

    When ``platform_env`` is anything other than ``"production"`` (including
    ``"development"``, empty string, or undefined), the function returns
    ``blocked=False`` regardless of credential values. If dev-default
    credentials are detected in non-production mode, a WARNING is logged.

    Args:
        platform_env: The value of the PLATFORM_ENV environment variable.
        env_vars: A mapping of environment variable names to their values.
        dev_defaults: The list of dev-default definitions to check against.
            Defaults to the module-level ``DEV_DEFAULTS`` tuple.

    Returns:
        A :class:`CredentialGuardResult` indicating whether boot should be
        blocked and which violations were found.
    """
    violations: list[str] = []

    for dev_default in dev_defaults:
        current_value = env_vars.get(dev_default.env_var)
        if current_value == dev_default.insecure_value:
            violations.append(dev_default.description)

    is_production = platform_env == "production"

    if violations and is_production:
        for violation in violations:
            logger.critical(
                "CRITICAL: %s detected in production — refusing to start",
                violation,
            )
        return CredentialGuardResult(blocked=True, violations=violations)

    if violations and not is_production:
        logger.warning("WARNING: Running with dev-only credentials")
        for violation in violations:
            logger.warning("  - %s", violation)
        return CredentialGuardResult(blocked=False, violations=violations)

    return CredentialGuardResult(blocked=False, violations=[])
