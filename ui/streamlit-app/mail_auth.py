"""Mail OAuth ownership model for Streamlit Mail Chat.

Gmail and Outlook do not fit the Atlassian API-token form used by the
existing credential manager. In the first supported shape, the mail MCP
services own OAuth config and token refresh through their own ``.env`` files;
Streamlit only talks to those MCP endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mail_mcp import MailProvider


MailAuthOwner = Literal["mcp-service"]


@dataclass(frozen=True, slots=True)
class MailAuthStatus:
    provider: MailProvider
    label: str
    owner: MailAuthOwner
    streamlit_handles_tokens: bool
    required_env_keys: tuple[str, ...]
    future_streamlit_oauth_supported: bool = False


_REQUIRED_ENV_KEYS: dict[MailProvider, tuple[str, ...]] = {
    "gmail": (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
    ),
    "outlook": (
        "MICROSOFT_TENANT_ID",
        "MICROSOFT_CLIENT_ID",
        "MICROSOFT_CLIENT_SECRET",
        "MICROSOFT_REDIRECT_URI",
    ),
}

_LABELS: dict[MailProvider, str] = {
    "gmail": "Gmail OAuth",
    "outlook": "Outlook OAuth",
}


def mail_auth_status(provider: MailProvider) -> MailAuthStatus:
    """Return the current Streamlit-side auth contract for a mail provider."""

    return MailAuthStatus(
        provider=provider,
        label=_LABELS[provider],
        owner="mcp-service",
        streamlit_handles_tokens=False,
        required_env_keys=_REQUIRED_ENV_KEYS[provider],
    )


def mail_auth_statuses() -> list[MailAuthStatus]:
    """Return auth contracts for the supported mail providers."""

    return [mail_auth_status("gmail"), mail_auth_status("outlook")]


def assert_streamlit_oauth_not_enabled(provider: MailProvider) -> None:
    """Guard future Streamlit OAuth work from silently storing tokens here."""

    status = mail_auth_status(provider)
    if not status.future_streamlit_oauth_supported:
        raise NotImplementedError(
            f"{status.label} Streamlit OAuth flow is not implemented. "
            "OAuth config and refresh tokens must stay inside the mail MCP service."
        )
