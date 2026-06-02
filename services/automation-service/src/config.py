"""Pydantic v2 settings for the automation-service scaffold.

Only the surface needed by the ``/healthz`` and ``/readyz`` skeleton is
modelled here. The ``dependencies_reachable()`` method is a stub that
always returns ``True`` so that ``/readyz`` returns 200 in the default
scaffold; tests monkeypatch this method to exercise the 503 branch.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the automation-service scaffold."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Network
    port: int = 8080
    log_level: str = "INFO"

    # Downstream dependencies (placeholders consumed by the real probe later)
    postgres_dsn: str = "postgresql://ai:ai_dev_only@postgres:5432/ai"
    vault_addr: str = "http://vault:8200"
    vault_token: str = "dev-token-not-for-prod"
    temporal_host: str = "temporal:7233"
    temporal_namespace: str = "default"
    mcp_base_url: str = "http://atlassian-mcp:8090"

    # LLM block (real provider by default; see Requirement 10.4)
    llm_provider: str = "openai"
    vllm_base_url: str = "http://host.docker.internal:8000/v1"
    vllm_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    llm_model_name: str = "gpt-4o-mini"
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Observability tag for X-Client-Source header
    client_source: str = "automation-service"

    def dependencies_reachable(self) -> bool:
        """Stub readiness probe.

        Returns ``True`` unconditionally in the scaffold. Real implementations
        will replace this with concrete TCP/HTTP checks against Postgres,
        Vault, Temporal, and the Atlassian MCP. Tests monkeypatch this method
        to drive the ``/readyz`` 503 branch (see Property 9).
        """

        return True
