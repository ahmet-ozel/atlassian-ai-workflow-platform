"""Pydantic-settings configuration for the admin-dashboard-api service.

This module provides a single :class:`Settings` class that reads the
service's environment variables. Real dependency probing is intentionally
not implemented at the initial stage; :meth:`Settings.dependencies_reachable`
returns ``True`` so ``/readyz`` succeeds in local-dev as long as the process
is alive.

In addition to the baseline keys (``PORT``, ``LOG_LEVEL``,
``POSTGRES_DSN``, ``VAULT_*``, ``TEMPORAL_*``, ``CLIENT_SOURCE``,
``AUTH_MODE``, ``OIDC_*``), this module exposes the Control_Plane keys
used by manifest loading, Compose orchestration, health polling, and
prompt sync wiring:

* ``WORKSPACE_ROOT`` - workspace folder containing ``config/``,
  ``infra/`` and ``services/``. Defaults to four levels above this
  module so ``python -m src.main`` works from a checked-out workspace
  without an explicit override.
* ``SERVICES_MANIFEST_PATH`` - workspace-relative path to the
  Service_Manifest JSON document.
* ``COMPOSE_FILE`` - workspace-relative path to the base Compose file
  used by :class:`ComposeRunner` subprocess invocations.
* ``MANIFEST_REFRESH_SECONDS`` - in-memory manifest cache TTL.
* ``HEALTH_POLL_INTERVAL_SECONDS`` - cadence of the background
  ``/healthz`` poll loop.
* ``HEALTH_READY_TIMEOUT_SECONDS`` - max wait after ``compose up``
  before declaring a service ``failed``.
* ``HEALTH_FAIL_STREAK_THRESHOLD`` - consecutive unhealthy polls that
  trigger a single ``health_streak_alert`` audit entry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_workspace_root() -> Path:
    """Return the workspace folder relative to this module's location.

    ``services/admin-dashboard-api/src/config.py``  ``parents[3]`` is
    the workspace root that contains ``config/``, ``infra/`` and
    ``services/``. The fallback only kicks in when the operator does
    not set ``WORKSPACE_ROOT`` - in production Compose passes the
    correct path via the env file.
    """
    try:
        return Path(__file__).resolve().parents[3]
    except IndexError:
        # Inside Docker container where path depth is shallow (/app/src/config.py)
        return Path("/app")


class Settings(BaseSettings):
    """Runtime configuration for the admin-dashboard-api service.

    Values are read from process environment variables (and a local ``.env``
    file when present) following the local-development and production
    environment model.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    port: int = Field(default=8082, description="Port the FastAPI app binds to.")
    log_level: str = Field(default="INFO", description="Process log level.")

    postgres_dsn: str = Field(
        default="postgresql://ai:ai_dev_only@postgres:5432/ai",
        description="PostgreSQL DSN for the admin/audit/cost views.",
    )
    vault_addr: str = Field(
        default="http://vault:8200",
        description="HashiCorp Vault address used for secret lookups.",
    )
    vault_token: str = Field(
        default="dev-token-not-for-prod",
        description="Dev-mode Vault token (placeholder, never a real secret).",
    )
    temporal_host: str = Field(
        default="temporal:7233",
        description="Temporal frontend host:port pair.",
    )

    client_source: str = Field(
        default="admin-dashboard-api",
        description="Default value advertised in outgoing X-Client-Source headers.",
    )

    # --- McpTrafficRouter metrics client wiring --------------------------------------
    # Base URL of the Atlassian MCP server. The traffic-stats router
    # appends ``/metrics`` and parses the Prometheus exposition for
    # ``mcp_requests_total{client_source, tool, status}`` metrics.
    # Defaults to the Compose-internal hostname; production
    # deployments override via the env file.
    mcp_metrics_url: str = Field(
        default="http://atlassian-mcp:8090",
        description=(
            "Base URL of the Atlassian MCP server (without trailing "
            "/metrics). Used by /api/v1/mcp/traffic to scrape the "
            "Prometheus counter mcp_requests_total."
        ),
    )

    # --- AdminProxy (admin proxy wiring - platform foundation) ----------
    # ``AUTOMATION_SERVICE_URL`` is the base URL of automation-service,
    # which owns every ``/admin/*`` endpoint forwarded by
    # :class:`src.proxy.AdminProxy`. The default
    # points at the Compose-internal hostname; production deployments
    # override it via the env file.
    automation_service_url: str = Field(
        default="http://automation-service:8080",
        description="Base URL of automation-service used by AdminProxy.",
    )

    # --- Deployment profile ----------------------------------------
    # ``DEPLOYMENT_PROFILE`` identifies the runtime environment for
    # admin-only safety guards. The lifecycle stop endpoint refuses
    # ``purge_vault=true`` when this resolves to ``"production"`` -
    # the dev-only Vault override purge is a developer escape hatch
    # and must never apply on a production cluster.
    # The value is matched case-insensitively in the router; common
    # values are ``"dev"``, ``"staging"``, ``"production"``. Defaults
    # to ``"dev"`` so the local-dev developer experience is unchanged.
    deployment_profile: str = Field(
        default="dev",
        description=(
            "Runtime deployment profile (``dev`` / ``staging`` / "
            "``production``). Production blocks ``purge_vault=true`` "
            "on the lifecycle stop endpoint."
        ),
    )

    # --- OIDC / auth -----------------------------------------------
    # ``AUTH_MODE`` selects between the dev-mode bypass (``dev``, accepts
    # any non-empty bearer token) and the full JWKS-backed validator
    # (``production``). The remaining three fields wire the validator to
    # the configured IdP. They are intentionally optional so the service
    # still boots in ``dev`` mode without an IdP configured.
    auth_mode: Literal["dev", "production"] = Field(
        default="dev",
        description="OIDC validator mode; ``production`` enforces full JWKS verification.",
    )
    oidc_issuer: str = Field(
        default="",
        description="Expected ``iss`` claim for production tokens.",
    )
    oidc_audience: str = Field(
        default="",
        description="Expected ``aud`` claim for production tokens.",
    )
    oidc_jwks_url: str = Field(
        default="",
        description="HTTPS URL of the IdP JWKS document used in production mode.",
    )

    # --- Control_Plane ----------------------------------------------
    # These keys back the manifest loader, ComposeRunner, HealthProbe
    # and AuditWriter wiring performed by ``src/main.py``'s lifespan
    # context.
    workspace_root: Path = Field(
        default_factory=_default_workspace_root,
        description=(
            "Absolute path to the workspace root that contains config/, "
            "infra/ and services/. Defaults to the folder four levels "
            "above this module."
        ),
    )
    services_manifest_path: str = Field(
        default="config/services.manifest.json",
        description="Workspace-relative path to the Service_Manifest JSON.",
    )
    compose_file: str = Field(
        default="infra/docker-compose.yml",
        description="Workspace-relative path to the base Compose file.",
    )
    manifest_refresh_seconds: int = Field(
        default=60,
        ge=10,
        le=600,
        description="In-memory manifest cache TTL in seconds.",
    )
    health_poll_interval_seconds: int = Field(
        default=10,
        ge=5,
        le=30,
        description="Cadence of the /healthz polling loop in seconds.",
    )
    health_ready_timeout_seconds: int = Field(
        default=60,
        ge=5,
        le=180,
        description=(
            "Max wait after ``compose up`` before marking the service "
            "``failed``."
        ),
    )
    health_fail_streak_threshold: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Consecutive unhealthy polls that trigger a single "
            "``health_streak_alert`` audit entry."
        ),
    )

    # --- PromptsGitRouter (operations surface prompt git wiring) -------------
    # ``PROMPTS_REPO_PATH`` is the absolute path to the local git
    # clone that holds prompt Markdown files. The
    # :class:`PromptsGitRouter` opens this clone via
    # :class:`git_shared.GitRepo` and routes every CRUD / draft / PR
    # call against it. Defaults to ``workspace_root`` so the same
    # checkout that hosts ``config/`` and ``services/`` also hosts
    # ``platform/prompts/``; production deployments that run
    # admin-dashboard-api as a sidecar with a separate prompts clone
    # override this via the env file.
    prompts_repo_path: Path = Field(
        default_factory=_default_workspace_root,
        description=(
            "Absolute path to the local git clone that the prompt "
            "CRUD endpoints operate on. Defaults to ``workspace_root``."
        ),
    )
    prompts_main_branch: str = Field(
        default="main",
        description=(
            "Name of the branch the prompts router forks draft "
            "branches from."
        ),
    )
    prompts_dir_prefix: str = Field(
        default="platform/prompts/",
        description=(
            "Repository-relative prefix listed by GET /admin/prompts. "
            "Empty string lists every Markdown file in the clone."
        ),
    )

    def dependencies_reachable(self) -> bool:
        """Stub readiness probe.

        Real implementations will probe Postgres/Temporal/Vault. The stack
        always returns ``True`` so the service starts cleanly under
        ``docker compose up``.
        """

        return True
