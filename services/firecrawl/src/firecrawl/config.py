"""Runtime configuration loaded from environment variables.

The wrapper deliberately keeps the surface tiny — every knob maps 1:1 to a
row in ``platform/docs/env-reference.md`` so the env-coverage property test
(task 10.6) stays green.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings"]


class Settings(BaseSettings):
    """Wrapper service settings.

    Notes
    -----
    * ``FIRECRAWL_EGRESS_ALLOWLIST`` is read as a raw string and parsed by
      :func:`firecrawl.egress.parse_allowlist`. We keep parsing in the
      egress module so the matching logic and the parser live next to each
      other and share a single test surface.
    * ``FIRECRAWL_UPSTREAM_BASE_URL`` is optional. When unset, the wrapper
      performs the HTTP fetch itself via ``httpx``. When set, allow-listed
      requests are forwarded to the configured upstream so production can
      run a fully-featured Firecrawl behind the same egress filter.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    port: int = Field(default=3002, validation_alias="PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    egress_allowlist_raw: str = Field(
        default="",
        validation_alias="FIRECRAWL_EGRESS_ALLOWLIST",
    )
    upstream_base_url: str = Field(
        default="",
        validation_alias="FIRECRAWL_UPSTREAM_BASE_URL",
    )
    request_timeout_s: float = Field(
        default=30.0,
        validation_alias="FIRECRAWL_REQUEST_TIMEOUT_S",
    )
    api_key: str = Field(
        default="",
        validation_alias="FIRECRAWL_API_KEY",
    )
