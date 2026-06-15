"""Application configuration for assistant-service.

Pydantic v2 Settings reading environment variables. The
``dependencies_reachable`` method is used by ``/readyz``.
"""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(Exception):
    """Provider credential eksik - boot fail-fast."""


def _is_valid_url(url: str) -> bool:
    """Check that *url* has both a scheme and a network location (host)."""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme) and bool(parsed.netloc)
    except Exception:
        return False


class Settings(BaseSettings):
    """Runtime settings for the assistant-service skeleton."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    port: int = Field(default=8081, description="HTTP listen port.")
    log_level: str = Field(default="INFO", description="Standard logging level name.")

    # Dependency endpoints - placeholders only; real probes added later.
    postgres_dsn: str = Field(
        default="postgresql://ai:ai_dev_only@postgres:5432/ai",
        description="Postgres DSN for the assistant schema.",
    )
    redis_url: str = Field(
        default="redis://redis:6379/0",
        description="Redis connection URL for chat session state.",
    )
    mcp_base_url: str = Field(
        default="http://atlassian-mcp:8090",
        description="Atlassian MCP base URL.",
    )
    gmail_mcp_base_url: str = Field(
        default="http://gmail-mcp:8110",
        description="Gmail MCP base URL.",
    )
    outlook_mcp_base_url: str = Field(
        default="http://outlook-mcp:8120",
        description="Outlook MCP base URL.",
    )
    temporal_host: str = Field(
        default="temporal:7233",
        description="Temporal frontend host:port (SDK kept for version parity even if unused).",
    )
    client_source: str = Field(
        default="assistant-service",
        description="Default value for the X-Client-Source HTTP header.",
    )

    # LLM block - assistant-service is an LLM consumer.
    llm_provider: str = Field(default="openai")
    vllm_base_url: str = Field(default="http://host.docker.internal:8000/v1")
    vllm_api_key: str = Field(default="")
    openai_base_url: str = Field(default="https://api.openai.com/v1")
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1")
    llm_model_name: str = Field(default="gpt-5.5")
    openai_api_key: str = Field(default="")
    anthropic_api_key: str = Field(default="")

    # LLM tuning parameters.
    llm_request_timeout_s: int = Field(default=60, description="LLM request timeout in seconds.")
    llm_max_tokens_output: int = Field(default=4096, description="Max tokens in LLM output.")
    llm_reasoning_effort: str = Field(
        default="",
        description="Reasoning effort for reasoning-capable models "
        "(minimal|low|medium|high); blank = upstream default.",
    )
    llm_verbosity: str = Field(
        default="",
        description="Output verbosity for the gpt-5 family "
        "(low|medium|high); blank = upstream default.",
    )

    def validate_provider_credentials(self) -> None:
        """Validate that required credentials are present for the configured LLM provider.

        Called at boot time. Raises ``ConfigurationError`` for missing or
        invalid credentials - fail-fast behaviour.

        A credential is considered missing/invalid if it is empty or
        contains only whitespace characters.
        """
        allowed = {"openai", "anthropic", "vllm"}
        provider = self.llm_provider.strip().lower()
        if provider not in allowed:
            raise ConfigurationError(
                f"LLM_PROVIDER must be one of {sorted(allowed)}"
            )
        if provider == "openai" and not self.openai_api_key.strip():
            raise ConfigurationError("OPENAI_API_KEY required for openai provider")
        if provider == "anthropic" and not self.anthropic_api_key.strip():
            raise ConfigurationError("ANTHROPIC_API_KEY required for anthropic provider")
        if provider == "vllm":
            if not self.vllm_base_url or not _is_valid_url(self.vllm_base_url.strip()):
                raise ConfigurationError("VLLM_BASE_URL must be a valid URL")
            if not self.vllm_api_key.strip():
                raise ConfigurationError("VLLM_API_KEY required for vllm provider")

    def dependencies_reachable(self) -> bool:
        """Stub readiness probe.

        Returns ``True`` so ``/readyz`` answers ``200`` when no external
        dependency checks are configured.
        """
        return True
