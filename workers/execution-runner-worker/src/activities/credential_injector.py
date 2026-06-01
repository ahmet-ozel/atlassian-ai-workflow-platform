"""Credential Injector activity module for the execution-runner-worker.

Provides :func:`inject_git_credentials` and :func:`cleanup_git_credentials`,
Temporal activities that manage Bitbucket git credentials on the SSH runner.

Workflow:
1. Fetch credentials from Vault at ``vault:atlassian/{dept}/bitbucket``
2. Configure git credential helper on SSH runner with a TTL
3. After git operations complete, clean up credential configuration

Credential Masking:
    ALL credential values (username, password/app_password) are masked
    as ``***`` in any log output. This is enforced by the
    :func:`_mask_credential` helper and the :class:`CredentialMaskingFilter`
    log filter.

Retry Policy (caller-configured):
    max 2 retries, 5s backoff — applied via Temporal RetryPolicy on the
    activity options in the calling workflow.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from temporalio import activity

__all__ = [
    "CredentialInjectInput",
    "CredentialInjectResult",
    "CredentialInjectorError",
    "inject_git_credentials",
    "cleanup_git_credentials",
    "mask_credential_value",
    "build_vault_path",
]


# ---------------------------------------------------------------------------
# Credential Masking
# ---------------------------------------------------------------------------

#: Mask placeholder used in all log outputs for credential values.
CREDENTIAL_MASK: str = "***"


class CredentialMaskingFilter(logging.Filter):
    """Log filter that replaces known credential values with ``***``.

    Attach this filter to any logger that may emit credential-adjacent
    messages. The filter maintains a set of sensitive strings and
    replaces any occurrence in log records.
    """

    def __init__(self) -> None:
        super().__init__()
        self._sensitive_values: set[str] = set()

    def add_sensitive(self, value: str) -> None:
        """Register a value that must be masked in log output."""
        if value:
            self._sensitive_values.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask sensitive values in the log message."""
        if self._sensitive_values:
            msg = record.getMessage()
            for sensitive in self._sensitive_values:
                if sensitive in msg:
                    msg = msg.replace(sensitive, CREDENTIAL_MASK)
            record.msg = msg
            record.args = None
        return True


def mask_credential_value(value: str) -> str:
    """Return a masked representation of a credential value.

    Shows only the first character followed by ``***`` for non-empty
    values. Returns ``***`` for empty strings.

    Parameters
    ----------
    value:
        The credential value to mask.

    Returns
    -------
    str
        Masked string safe for logging.
    """
    if not value:
        return CREDENTIAL_MASK
    if len(value) <= 2:
        return CREDENTIAL_MASK
    return value[0] + CREDENTIAL_MASK


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class CredentialInjectorError(RuntimeError):
    """Raised when credential injection or cleanup fails.

    Attributes
    ----------
    workflow_id : str
        The workflow that requested the operation.
    cause : str
        Human-readable description of what went wrong.
    error_code : str
        Machine-readable error category for workflow status.
    """

    def __init__(
        self,
        workflow_id: str,
        cause: str,
        *,
        error_code: str = "credential_unavailable",
    ) -> None:
        self.workflow_id = workflow_id
        self.cause = cause
        self.error_code = error_code
        super().__init__(
            f"credential injector failed for workflow={workflow_id}: {cause}"
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CredentialInjectInput:
    """Input for the inject_git_credentials activity.

    Attributes
    ----------
    dept_id : str
        Department identifier used to construct the Vault path.
    workflow_id : str
        Parent workflow ID for audit/logging context.
    ttl_minutes : int
        Time-to-live for the credential helper configuration on SSH.
        Defaults to 15 minutes per Requirement 2.2.
    """

    dept_id: str
    workflow_id: str
    ttl_minutes: int = 15


@dataclass(frozen=True)
class CredentialInjectResult:
    """Result of the inject_git_credentials activity.

    Attributes
    ----------
    success : bool
        Whether credential injection succeeded.
    error : str | None
        Error message if injection failed; None on success.
    masked_username : str | None
        Masked username for audit logging (e.g. "u***").
    """

    success: bool
    error: str | None = None
    masked_username: str | None = None


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def build_vault_path(dept_id: str) -> str:
    """Construct the Vault KV-v2 path for Bitbucket credentials.

    The path follows the convention: ``atlassian/{dept}/bitbucket``

    Parameters
    ----------
    dept_id:
        Department identifier.

    Returns
    -------
    str
        The relative Vault path (without ``vault:`` prefix).
    """
    return f"atlassian/{dept_id}/bitbucket"


def _vault_addr() -> str:
    """Read Vault address from environment."""
    addr = os.environ.get("VAULT_ADDR", "http://vault:8200")
    return addr.rstrip("/")


def _vault_token() -> str:
    """Read Vault token from environment."""
    return os.environ.get("VAULT_TOKEN", "")


def _kv_mount() -> str:
    """Read the KV-v2 mount path from environment."""
    return os.environ.get("VAULT_KV_MOUNT", "secret")


#: Vault fetch timeout in seconds (Requirement 2.1: max 30s).
VAULT_TIMEOUT_SECONDS: float = 30.0

#: Maximum retry attempts for Vault fetch (Requirement 2.4: max 2 retries).
VAULT_MAX_RETRIES: int = 2

#: Backoff between retries in seconds (Requirement 2.4: 5s backoff).
VAULT_RETRY_BACKOFF_SECONDS: float = 5.0

#: Cleanup timeout in seconds (Requirement 2.3: within 5s).
CLEANUP_TIMEOUT_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Vault credential fetch with retry
# ---------------------------------------------------------------------------


async def _fetch_credential_from_vault(
    dept_id: str,
    workflow_id: str,
) -> dict[str, str]:
    """Fetch Bitbucket credentials from Vault with retry logic.

    Implements Requirement 2.1 (30s timeout) and 2.4 (2 retries, 5s backoff).

    Parameters
    ----------
    dept_id:
        Department identifier for Vault path construction.
    workflow_id:
        Workflow ID for error context.

    Returns
    -------
    dict[str, str]
        Dictionary with ``username`` and ``app_password`` keys.

    Raises
    ------
    CredentialInjectorError
        If all attempts to fetch credentials fail.
    """
    addr = _vault_addr()
    token = _vault_token()
    mount = _kv_mount()
    path = build_vault_path(dept_id)

    if not token:
        raise CredentialInjectorError(
            workflow_id=workflow_id,
            cause="VAULT_TOKEN environment variable is empty or not set",
        )

    url = f"{addr}/v1/{mount}/data/{path}"
    last_error: str = ""

    # Initial attempt + retries (total attempts = 1 + VAULT_MAX_RETRIES)
    for attempt in range(1 + VAULT_MAX_RETRIES):
        if attempt > 0:
            activity.logger.warning(
                "Vault credential fetch retry %d/%d for workflow=%s "
                "(waiting %.1fs)",
                attempt,
                VAULT_MAX_RETRIES,
                workflow_id,
                VAULT_RETRY_BACKOFF_SECONDS,
            )
            await asyncio.sleep(VAULT_RETRY_BACKOFF_SECONDS)

        try:
            async with httpx.AsyncClient(
                timeout=VAULT_TIMEOUT_SECONDS
            ) as client:
                response = await client.get(
                    url,
                    headers={"X-Vault-Token": token},
                )

            if response.status_code == 404:
                last_error = (
                    f"credential not found at vault path: {mount}/data/{path}"
                )
                # No point retrying a 404
                break

            if not (200 <= response.status_code < 300):
                last_error = (
                    f"Vault returned HTTP {response.status_code} "
                    f"for path={mount}/data/{path}"
                )
                continue

            payload = response.json()
            outer_data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(outer_data, dict):
                last_error = "Vault response missing 'data' envelope"
                continue

            inner_data = outer_data.get("data")
            if not isinstance(inner_data, dict):
                last_error = "Vault response missing 'data.data' payload"
                continue

            # Validate required fields
            username = inner_data.get("username", "")
            app_password = inner_data.get("app_password", "")

            if not username or not app_password:
                missing = []
                if not username:
                    missing.append("username")
                if not app_password:
                    missing.append("app_password")
                last_error = (
                    f"incomplete Bitbucket credential: missing {missing}"
                )
                continue

            return {"username": str(username), "app_password": str(app_password)}

        except httpx.TimeoutException:
            last_error = (
                f"Vault request timed out after {VAULT_TIMEOUT_SECONDS}s "
                f"(attempt {attempt + 1}/{1 + VAULT_MAX_RETRIES})"
            )
            continue
        except httpx.HTTPError as exc:
            last_error = f"Vault transport error: {exc.__class__.__name__}: {exc}"
            continue

    # All attempts exhausted
    raise CredentialInjectorError(
        workflow_id=workflow_id,
        cause=f"all Vault credential fetch attempts failed: {last_error}",
        error_code="credential_unavailable",
    )


# ---------------------------------------------------------------------------
# SSH credential helper configuration
# ---------------------------------------------------------------------------


def _build_credential_helper_script(
    username: str,
    app_password: str,
    ttl_minutes: int,
) -> str:
    """Build the shell commands to configure git credential helper on SSH.

    Creates a temporary credential helper that provides the username and
    app_password to git operations. The helper is configured with a TTL
    via git's credential.helper cache timeout.

    Parameters
    ----------
    username:
        Bitbucket username.
    app_password:
        Bitbucket app password.
    ttl_minutes:
        TTL in minutes for the credential cache.

    Returns
    -------
    str
        Shell script to execute on the SSH runner.
    """
    ttl_seconds = ttl_minutes * 60
    # Use git credential-store with a temporary file, plus a cache timeout
    # The credential file is workflow-scoped to avoid conflicts
    return (
        f"git config --global credential.helper "
        f"'cache --timeout={ttl_seconds}' && "
        f"printf 'protocol=https\\nhost=bitbucket.org\\n"
        f"username={username}\\npassword={app_password}\\n\\n' | "
        f"git credential approve"
    )


def _build_cleanup_script() -> str:
    """Build the shell commands to remove git credential configuration.

    Returns
    -------
    str
        Shell script to clean up credential helper on the SSH runner.
    """
    return (
        "git credential-cache exit 2>/dev/null; "
        "git config --global --unset credential.helper 2>/dev/null; "
        "true"
    )


# ---------------------------------------------------------------------------
# SSH execution helper
# ---------------------------------------------------------------------------


async def _execute_on_ssh(
    command: str,
    timeout_seconds: float,
    workflow_id: str,
) -> dict[str, Any]:
    """Execute a command on the SSH runner via the ssh_connect_and_run activity.

    This is a helper that imports and calls the existing SSH activity
    infrastructure. In production, the workflow would schedule this as
    a separate activity; here we use the internal SSH execution helper
    directly for the credential configuration commands.

    Parameters
    ----------
    command:
        Shell command to execute.
    timeout_seconds:
        Maximum execution time.
    workflow_id:
        Workflow ID for credential fetch context.

    Returns
    -------
    dict
        Result with stdout, stderr, exit_code keys.

    Raises
    ------
    CredentialInjectorError
        If SSH execution fails.
    """
    from src.activities.vault import vault_fetch_ssh_credentials
    from src.activities.ssh import _ssh_execute_command, SSHActivityError

    try:
        cred = await vault_fetch_ssh_credentials(workflow_id)
    except Exception as exc:
        raise CredentialInjectorError(
            workflow_id=workflow_id,
            cause=f"SSH credential fetch failed: {exc}",
            error_code="credential_unavailable",
        ) from exc

    try:
        result = await asyncio.to_thread(
            _ssh_execute_command,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            command,
            "",  # no workspace path needed for git config
            int(timeout_seconds),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except SSHActivityError as exc:
        raise CredentialInjectorError(
            workflow_id=workflow_id,
            cause=f"SSH execution failed: {exc.cause}",
            error_code="credential_unavailable",
        ) from exc


# ---------------------------------------------------------------------------
# Temporal activities
# ---------------------------------------------------------------------------


@activity.defn(name="inject_git_credentials")
async def inject_git_credentials(
    input: CredentialInjectInput,
) -> CredentialInjectResult:
    """Fetch Bitbucket credentials from Vault and configure git on SSH runner.

    Implements the full credential injection flow:
    1. Fetch credentials from Vault (30s timeout, 2 retries, 5s backoff)
    2. Configure git credential helper on SSH runner (15 min TTL)
    3. Return masked username for audit

    All credential values are masked in log output (Requirement 2.7).

    Parameters
    ----------
    input:
        Injection parameters including dept_id, workflow_id, and ttl_minutes.

    Returns
    -------
    CredentialInjectResult
        Success/failure result with masked username.
    """
    # Set up credential masking filter for this activity's logger
    masking_filter = CredentialMaskingFilter()
    activity.logger.addFilter(masking_filter)

    try:
        activity.logger.info(
            "Injecting git credentials for dept=%s workflow=%s ttl=%d min",
            input.dept_id,
            input.workflow_id,
            input.ttl_minutes,
        )

        # Step 1: Fetch credentials from Vault
        # Requirement 2.1: 30s timeout, Requirement 2.4: 2 retries, 5s backoff
        try:
            credentials = await _fetch_credential_from_vault(
                dept_id=input.dept_id,
                workflow_id=input.workflow_id,
            )
        except CredentialInjectorError:
            raise
        except Exception as exc:
            raise CredentialInjectorError(
                workflow_id=input.workflow_id,
                cause=f"unexpected error fetching credentials: {exc}",
            ) from exc

        username = credentials["username"]
        app_password = credentials["app_password"]

        # Register sensitive values for masking (Requirement 2.7)
        masking_filter.add_sensitive(username)
        masking_filter.add_sensitive(app_password)

        masked_user = mask_credential_value(username)

        activity.logger.info(
            "Credentials fetched successfully for dept=%s, user=%s",
            input.dept_id,
            masked_user,
        )

        # Step 2: Configure git credential helper on SSH runner
        # Requirement 2.2: 15 min TTL
        configure_script = _build_credential_helper_script(
            username=username,
            app_password=app_password,
            ttl_minutes=input.ttl_minutes,
        )

        try:
            result = await _execute_on_ssh(
                command=configure_script,
                timeout_seconds=30.0,
                workflow_id=input.workflow_id,
            )
        except CredentialInjectorError:
            raise
        except Exception as exc:
            raise CredentialInjectorError(
                workflow_id=input.workflow_id,
                cause=f"SSH credential helper configuration failed: {exc}",
            ) from exc

        if result["exit_code"] != 0:
            error_msg = (
                f"git credential helper configuration failed "
                f"(exit_code={result['exit_code']})"
            )
            activity.logger.error(
                "Credential helper setup failed: %s", error_msg
            )
            return CredentialInjectResult(
                success=False,
                error=error_msg,
                masked_username=masked_user,
            )

        activity.logger.info(
            "Git credential helper configured successfully for "
            "dept=%s workflow=%s (TTL=%d min)",
            input.dept_id,
            input.workflow_id,
            input.ttl_minutes,
        )

        return CredentialInjectResult(
            success=True,
            error=None,
            masked_username=masked_user,
        )

    except CredentialInjectorError as exc:
        activity.logger.error(
            "Credential injection failed for workflow=%s: %s",
            input.workflow_id,
            exc.cause,
        )
        return CredentialInjectResult(
            success=False,
            error=exc.cause,
            masked_username=None,
        )
    finally:
        # Remove the masking filter to avoid leaking state
        activity.logger.removeFilter(masking_filter)


@activity.defn(name="cleanup_git_credentials")
async def cleanup_git_credentials(workflow_id: str) -> None:
    """Remove git credential configuration from the SSH runner.

    Cleans up the credential helper and cached credentials within 5 seconds
    (Requirement 2.3). This activity is called after git push completes
    (success or failure) to ensure no credentials remain on the runner.

    Best-effort: if cleanup fails, the error is logged but not propagated
    since the credential cache has a TTL and will expire naturally.

    Parameters
    ----------
    workflow_id:
        The workflow that owns the credentials being cleaned up.

    Returns
    -------
    None
    """
    activity.logger.info(
        "Cleaning up git credentials for workflow=%s", workflow_id
    )

    cleanup_start = time.monotonic()

    try:
        cleanup_script = _build_cleanup_script()

        result = await _execute_on_ssh(
            command=cleanup_script,
            timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
            workflow_id=workflow_id,
        )

        elapsed = time.monotonic() - cleanup_start

        if result["exit_code"] != 0:
            activity.logger.warning(
                "Credential cleanup returned non-zero exit code=%d "
                "for workflow=%s (elapsed=%.2fs)",
                result["exit_code"],
                workflow_id,
                elapsed,
            )
        else:
            activity.logger.info(
                "Git credentials cleaned up successfully for "
                "workflow=%s (elapsed=%.2fs)",
                workflow_id,
                elapsed,
            )

    except CredentialInjectorError as exc:
        elapsed = time.monotonic() - cleanup_start
        activity.logger.warning(
            "Credential cleanup failed for workflow=%s (elapsed=%.2fs): %s "
            "(best-effort, credentials will expire via TTL)",
            workflow_id,
            elapsed,
            exc.cause,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        elapsed = time.monotonic() - cleanup_start
        activity.logger.warning(
            "Credential cleanup unexpected error for workflow=%s "
            "(elapsed=%.2fs): %s (best-effort, credentials will expire via TTL)",
            workflow_id,
            elapsed,
            str(exc),
        )
