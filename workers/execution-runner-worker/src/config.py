"""Pydantic v2 settings for the execution-runner-worker.

This module is the canonical home for environment-driven configuration
consumed by the worker. Task 13.2 of the ``platform-mimari-uyumluluk``
spec (Requirement 11.4 — Q13 ``RUNNER_BASE_PATH`` env standard) introduces
the :attr:`Settings.runner_base_path` field, which exposes the workspace
root used by ``runners/workspace_path.py::build_workspace_path``.

Backwards compatibility
-----------------------

Older deployments populate ``SSH_BASE_PATH`` instead of the canonical
``RUNNER_BASE_PATH``. ``pydantic_settings.AliasChoices`` lets us read
either name, with the canonical name winning when both are set
(Requirement 11.4 deprecation fallback). The default ``/var/ai-runner``
matches the value referenced in ``task-creation-assistant-prompt.md``
v1.9 and the per-worker ``.env.example``.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings for the execution-runner-worker.

    Only the surface needed by task 13.2 is modelled here. Other env
    variables consumed by the worker (Temporal, Vault, MinIO, SSH) are
    still read via ad-hoc ``os.environ.get`` helpers in their respective
    activity modules; consolidating those is out of scope for this task.
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
            "Workspace kök klasörü. Task workspace'leri "
            "{RUNNER_BASE_PATH}/{ISSUE_KEY}/iter-{N}/ formatında oluşturulur. "
            "SSH_BASE_PATH geriye uyum için alias olarak okunur; "
            "RUNNER_BASE_PATH set ise o tercih edilir."
        ),
    )
