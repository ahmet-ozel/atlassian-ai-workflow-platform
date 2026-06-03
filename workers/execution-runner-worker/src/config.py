"""Pydantic v2 settings for the execution-runner-worker.

This module is the canonical home for environment-driven configuration
consumed by the worker. The :attr:`Settings.runner_base_path` field exposes
the workspace root used by ``runners/workspace_path.py::build_workspace_path``.

Backwards compatibility
-----------------------

Older deployments populate ``SSH_BASE_PATH`` instead of the canonical
``RUNNER_BASE_PATH``. ``pydantic_settings.AliasChoices`` lets us read
either name, with the canonical name winning when both are set
as a deprecation fallback. The default ``/var/ai-runner`` matches the
per-worker ``.env.example``.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the execution-runner-worker.

    Only the currently used settings surface is modelled here. Other env
    variables consumed by the worker (Temporal, Vault, MinIO, SSH) are
    still read via ad-hoc ``os.environ.get`` helpers in their respective
    activity modules.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Workspace root for SSH/Docker remote runners. ``RUNNER_BASE_PATH``
    # is canonical (Q13); ``SSH_BASE_PATH`` is the deprecated alias kept
    # for backwards compatibility with existing deployments.
    runner_base_path: str = Field(
        default="/var/ai-runner",
        validation_alias=AliasChoices("RUNNER_BASE_PATH", "SSH_BASE_PATH"),
        description=(
            "Workspace kök klasörü. İş çalışma alanları "
            "{RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}/ formatında oluşturulur. "
            "SSH_BASE_PATH geriye uyum için alias olarak okunur; "
            "RUNNER_BASE_PATH set ise o tercih edilir."
        ),
    )
