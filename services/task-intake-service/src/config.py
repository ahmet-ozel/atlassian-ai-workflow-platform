"""Configuration for task-intake-service.

Reads environment variables via Pydantic Settings. The
``dependencies_reachable`` method is a stub used by ``/readyz`` until
real dependency probes (Postgres, Temporal, MCP, channel adapters) are
wired up.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for task-intake-service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = Field(default=8083, description="HTTP listen port.")
    log_level: str = Field(default="INFO", description="Log level.")

    postgres_dsn: str = Field(
        default="postgresql://ai:ai_dev_only@postgres:5432/ai",
        description="Postgres DSN (intake metadata persistence).",
    )
    temporal_host: str = Field(
        default="temporal:7233",
        description="Temporal frontend host:port.",
    )
    mcp_base_url: str = Field(
        default="http://atlassian-mcp:8090",
        description="atlassian_mcp_bitbucket MCP base URL.",
    )
    firecrawl_base_url: str = Field(
        default="http://firecrawl:3002",
        description="Firecrawl base URL (web ingestion channel).",
    )
    client_source: str = Field(
        default="task-intake-service",
        description="X-Client-Source header value for outbound MCP calls.",
    )

    def dependencies_reachable(self) -> bool:
        """Stub readiness probe.

        Always returns ``True`` in this lightweight implementation. Real implementations should
        probe Postgres, Temporal, the MCP base URL, and any active channel
        adapters and return ``False`` if any required dependency is down.
        """

        return True
