"""Mail OAuth ownership model for Streamlit Mail Chat.

Gmail and Outlook do not fit the Atlassian API-token form used by the
existing credential manager. In the multi-user shape, each user connects a
mail credential for their own session. The credential is stored in Vault and
passed to mail MCP services by credential ref; mail user tokens and provider
client secrets are not required in service-local .env files.
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
    token_storage: str
    required_env_keys: tuple[str, ...]
    future_streamlit_oauth_supported: bool = False


_REQUIRED_ENV_KEYS: dict[MailProvider, tuple[str, ...]] = {
    "gmail": (),
    "outlook": (),
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
        token_storage="per-user-vault-credential-ref",
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
            "Use the per-user credential form so Vault stores only a credential ref "
            "for the mail MCP services."
        )
