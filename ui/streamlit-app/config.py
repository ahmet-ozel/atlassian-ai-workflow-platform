"""Streamlit app configuration loader.

Reads runtime settings from environment variables (and a local
``.env`` file when present) following the same two-level model as
the rest of the platform services. The
:class:`Settings` class is consumed by ``app.py``'s session-state
injector and surfaces the URLs every page reaches out to:

* ``ASSISTANT_SERVICE_URL`` — base URL of assistant-service (the
  chat / task creator pages proxy here).
* ``ADMIN_DASHBOARD_API_URL`` — base URL of admin-dashboard-api
  (workflow listing, costs, audit search).
* ``MCP_BASE_URL`` — base URL of the read-only Atlassian MCP server
  used by the Explorer page.
* ``CLIENT_SOURCE`` — value advertised in outgoing
  ``X-Client-Source`` headers.

The ``dependencies_reachable`` method is a stub used by ``/healthz``;
real probes belong in the boot script.
"""

from __future__ import annotations

import os


class Settings:
    """Lightweight, Pydantic-free settings reader.

    Streamlit images intentionally avoid pulling in pydantic-settings
    so the container stays small. The class reads the env once at
    construction and exposes the resolved values as attributes.
    """

    def __init__(self) -> None:
        self.assistant_service_url: str = os.environ.get(
            "ASSISTANT_SERVICE_URL",
            os.environ.get("ASSISTANT_BASE_URL", "http://assistant-service:8081"),
        )
        self.admin_api_url: str = os.environ.get(
            "ADMIN_DASHBOARD_API_URL", "http://admin-dashboard-api:8082"
        )
        self.mcp_base_url: str = os.environ.get(
            "MCP_BASE_URL", "http://atlassian-mcp:8090"
        )
        self.client_source: str = os.environ.get(
            "CLIENT_SOURCE", "streamlit-app"
        )
        self.llm_provider: str = os.environ.get("LLM_PROVIDER", "openai").lower()
        self.llm_model_name: str = os.environ.get("LLM_MODEL_NAME", "gpt-4o-mini")
        self.openai_api_key: str = os.environ.get("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.environ.get(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        self.vllm_base_url: str = os.environ.get(
            "VLLM_BASE_URL", "http://host.docker.internal:8000/v1"
        )
        self.vllm_api_key: str = os.environ.get("VLLM_API_KEY", "")
        self.anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
        self.anthropic_base_url: str = os.environ.get(
            "ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"
        )
        self.log_level: str = os.environ.get("LOG_LEVEL", "INFO")
        self.default_language: str = os.environ.get(
            "DEFAULT_LANGUAGE", "tr"
        )
