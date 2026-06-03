"""Vault activity module for the execution-runner-worker.

Provides :func:`vault_fetch_ssh_credentials`, a Temporal activity that
reads SSH connection credentials from HashiCorp Vault KV-v2 and returns
them as a frozen :class:`SSHCred` dataclass.

Each activity invocation creates a fresh Vault HTTP client with no persistent
session or cached token, ensuring token expiry during long-running workflows
does not affect subsequent activities.

Vault path layout:
    ``{VAULT_KV_MOUNT}/data/ssh/runner/current``
    Expected secret keys: ``host``, ``port``, ``user``, ``private_key``
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from temporalio import activity

__all__ = [
    "SSHCred",
    "CredentialResolutionError",
    "vault_fetch_ssh_credentials",
]


class CredentialResolutionError(RuntimeError):
    """Raised when Vault returns incomplete or missing SSH credentials.

    Attributes
    ----------
    workflow_id : str
        The workflow that requested the credentials.
    cause : str
        Human-readable description of what went wrong.
    missing_fields : list[str]
        The credential fields that were absent or empty.
    """

    def __init__(
        self,
        workflow_id: str,
        cause: str,
        *,
        missing_fields: list[str] | None = None,
    ) -> None:
        self.workflow_id = workflow_id
        self.cause = cause
        self.missing_fields = missing_fields or []
        super().__init__(
            f"vault credential resolution failed for workflow={workflow_id}: {cause}"
        )


@dataclass(frozen=True)
class SSHCred:
    """SSH connection credentials retrieved from Vault.

    All fields are required and validated at construction time by the
    :func:`vault_fetch_ssh_credentials` activity.
    """

    host: str
    port: int
    user: str
    private_key: str


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

#: Required keys in the Vault KV-v2 secret payload.
_REQUIRED_FIELDS: tuple[str, ...] = ("host", "port", "user", "private_key")

#: Default Vault KV-v2 mount path.
_DEFAULT_KV_MOUNT: str = "secret"

#: Default Vault secret path for SSH runner credentials.
_DEFAULT_SSH_SECRET_PATH: str = "ssh/runner/current"


def _vault_addr() -> str:
    """Read Vault address from environment."""
    addr = os.environ.get("VAULT_ADDR", "http://vault:8200")
    return addr.rstrip("/")


def _vault_token() -> str:
    """Read Vault token from environment.

    In production, this would use AppRole or Kubernetes service account
    authentication. For P0, the token is read from the environment
    (set by the Compose stack or injected by the orchestrator).
    """
    return os.environ.get("VAULT_TOKEN", "")


def _kv_mount() -> str:
    """Read the KV-v2 mount path from environment."""
    return os.environ.get("VAULT_KV_MOUNT", _DEFAULT_KV_MOUNT)


def _ssh_secret_path() -> str:
    """Read the SSH secret path from environment."""
    return os.environ.get("VAULT_SSH_SECRET_PATH", _DEFAULT_SSH_SECRET_PATH)


# ---------------------------------------------------------------------------
# Vault KV-v2 read helper
# ---------------------------------------------------------------------------


async def _read_kv2_secret(
    client: httpx.AsyncClient,
    addr: str,
    token: str,
    mount: str,
    path: str,
) -> dict[str, Any]:
    """Read a secret from Vault KV-v2 and return the inner data dict.

    Vault KV-v2 response structure::

        {"data": {"data": {"key1": "val1", ...}, "metadata": {...}}}

    Returns the inner ``data.data`` dict.

    Raises
    ------
    CredentialResolutionError
        On transport errors, non-2xx responses, or malformed payloads.
    """
    url = f"{addr}/v1/{mount}/data/{path}"

    try:
        response = await client.get(
            url,
            headers={"X-Vault-Token": token},
        )
    except httpx.HTTPError as exc:
        raise CredentialResolutionError(
            workflow_id="",  # will be overwritten by caller
            cause=f"vault transport error: {exc.__class__.__name__}: {exc}",
        ) from exc

    if response.status_code == 404:
        raise CredentialResolutionError(
            workflow_id="",
            cause=f"vault secret not found at path={mount}/data/{path}",
        )

    if not (200 <= response.status_code < 300):
        raise CredentialResolutionError(
            workflow_id="",
            cause=(
                f"vault returned HTTP {response.status_code} "
                f"for path={mount}/data/{path}"
            ),
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise CredentialResolutionError(
            workflow_id="",
            cause="vault response is not valid JSON",
        ) from exc

    # Navigate KV-v2 envelope: data -> data -> {secret fields}
    outer_data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(outer_data, dict):
        raise CredentialResolutionError(
            workflow_id="",
            cause="vault response missing 'data' envelope",
        )

    inner_data = outer_data.get("data")
    if not isinstance(inner_data, dict):
        raise CredentialResolutionError(
            workflow_id="",
            cause="vault response missing 'data.data' payload",
        )

    return inner_data


# ---------------------------------------------------------------------------
# Temporal activity
# ---------------------------------------------------------------------------


@activity.defn(name="vault_fetch_ssh_credentials")
async def vault_fetch_ssh_credentials(
    workflow_id: str,
    vault_path: str | None = None,
) -> SSHCred:
    """Fetch SSH credentials from Vault KV-v2 for the given workflow.

    Previously this function ignored its arguments for path selection and
    ALWAYS read the fixed
    ``VAULT_SSH_SECRET_PATH`` env (default ``ssh/runner/current``). That
    made the admin SSH runner pool (``infrastructure.ssh_runners`` +
    ``dept_ssh_assignments`` + ``runner_resolver``) decorative — runners
    configured via the admin panel were never actually used. Now we
    accept an explicit ``vault_path`` (set by the workflow from
    ``runner_resolver``'s :class:`RunnerResolution.vault_path`) and read
    the secret from there.

    Parameters
    ----------
    workflow_id:
        The workflow requesting credentials. Used for error context and
        audit trail.
    vault_path:
        Explicit Vault KV-v2 path inside the configured mount, e.g.
        ``"ssh/runners/runner-01/active"``. Accepts the canonical
        ``vault:`` prefix produced by ``ssh_runners.py:create_ssh_runner``
        (the prefix is stripped before the HTTP call). When ``None`` or
        empty the env-derived ``VAULT_SSH_SECRET_PATH`` is used as a
        backward-compatibility fallback for single-runner deployments
        without an admin-panel-managed pool.

    Returns
    -------
    SSHCred
        Frozen dataclass with validated SSH connection details.

    Raises
    ------
    CredentialResolutionError
        If Vault is unreachable, the secret is missing, or any required
        field is absent/empty.
    """
    addr = _vault_addr()
    token = _vault_token()
    mount = _kv_mount()

    # Resolve the path: explicit argument wins; otherwise fall back to
    # the env-derived single-runner path (legacy contract).
    if vault_path:
        # Accept both ``vault:ssh/runners/.../active`` (canonical write
        # form produced by ``ssh_runners.py``) and a bare KV-v2 path.
        if vault_path.startswith("vault:"):
            path = vault_path[len("vault:") :]
        else:
            path = vault_path
        path = path.strip().lstrip("/")
        path_source = "argument"
    else:
        path = _ssh_secret_path()
        path_source = "env"

    activity.logger.info(
        "Fetching SSH credentials from Vault for workflow=%s "
        "(path=%s source=%s)",
        workflow_id,
        path,
        path_source,
    )

    if not token:
        raise CredentialResolutionError(
            workflow_id=workflow_id,
            cause="VAULT_TOKEN environment variable is empty or not set",
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            secret_data = await _read_kv2_secret(client, addr, token, mount, path)
        except CredentialResolutionError as exc:
            # Re-raise with the correct workflow_id context.
            raise CredentialResolutionError(
                workflow_id=workflow_id,
                cause=exc.cause,
            ) from exc

    # Validate required fields.
    missing: list[str] = [
        field for field in _REQUIRED_FIELDS if not secret_data.get(field)
    ]
    if missing:
        raise CredentialResolutionError(
            workflow_id=workflow_id,
            cause=f"incomplete SSH credential: missing fields {missing}",
            missing_fields=missing,
        )

    # Parse port as integer.
    raw_port = secret_data["port"]
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise CredentialResolutionError(
            workflow_id=workflow_id,
            cause=f"invalid port value: {raw_port!r} (must be an integer)",
        ) from exc

    if not (1 <= port <= 65535):
        raise CredentialResolutionError(
            workflow_id=workflow_id,
            cause=f"port {port} out of valid range (1-65535)",
        )

    return SSHCred(
        host=str(secret_data["host"]),
        port=port,
        user=str(secret_data["user"]),
        private_key=str(secret_data["private_key"]),
    )
