"""MCP credential injection helper for Atlassian services.

Provides :func:`with_atlassian_creds`, an async context manager that
injects department-specific Atlassian credentials (URL, username,
personal-access-token) into an :class:`httpx.AsyncClient`'s headers for
the duration of a with-block. On exit, the original header state is
restored so the client can be safely reused.

This module is the single point of credential injection for all MCP calls
to the Atlassian Unified MCP server. It ensures:

- Existing headers (including ``X-Client-Source``) are preserved.
- Only the three service-specific credential headers are overwritten.
- Incomplete or missing credentials raise :class:`CredentialResolutionError`.

Design reference: design.md §3.2 (foundation) + uyumluluk design.md R2
Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 2.1, 2.5
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

import httpx


class CredentialResolutionError(RuntimeError):
    """Raised when Vault returns incomplete or missing credentials.

    Attributes
    ----------
    dept_id : str
        The department identifier for which resolution failed.
    service : str
        The Atlassian service (jira, bitbucket, confluence) that was requested.
    """

    def __init__(self, dept_id: str, service: str, cause: str = "incomplete credential") -> None:
        super().__init__(f"resolve {service} for dept={dept_id}: {cause}")
        self.dept_id = dept_id
        self.service = service


ServiceLiteral = Literal["jira", "bitbucket", "confluence"]

_HEADER_PREFIX: dict[ServiceLiteral, str] = {
    "jira": "X-Atlassian-Jira",
    "bitbucket": "X-Atlassian-Bitbucket",
    "confluence": "X-Atlassian-Confluence",
}


def _cred_value(cred: Any, *names: str) -> str | None:
    """Read the first non-empty value from a credential object or mapping."""
    for name in names:
        if isinstance(cred, Mapping):
            value = cred.get(name)
        else:
            value = getattr(cred, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _looks_like_cloud_atlassian(url: str) -> bool:
    hostish = url.lower()
    return ".atlassian.net" in hostish or "api.atlassian.com" in hostish


def _looks_like_bitbucket_cloud(url: str) -> bool:
    hostish = url.lower()
    return "bitbucket.org" in hostish or "api.bitbucket.org" in hostish


def _credential_headers(
    *,
    cred: Any,
    service: ServiceLiteral,
    dept_id: str,
) -> dict[str, str]:
    """Build the stateless MCP credential headers for one service."""
    prefix = _HEADER_PREFIX[service]
    url = _cred_value(cred, "url", "base_url")
    username = _cred_value(cred, "username", "email", "user")
    personal_token = _cred_value(cred, "personal_token", "pat", "token")
    api_token = _cred_value(cred, "api_token")
    app_password = _cred_value(cred, "app_password")
    cloud_access_token = _cred_value(cred, "cloud_access_token")

    if not url:
        raise CredentialResolutionError(dept_id, service, "missing url")

    headers = {f"{prefix}-Url": url}

    if service == "bitbucket" and cloud_access_token:
        headers[f"{prefix}-Cloud-Access-Token"] = cloud_access_token
        if username:
            headers[f"{prefix}-Username"] = username
        return headers

    if service == "bitbucket" and app_password:
        if not username:
            raise CredentialResolutionError(dept_id, service, "missing username")
        headers[f"{prefix}-Username"] = username
        headers[f"{prefix}-App-Password"] = app_password
        return headers

    if api_token:
        if not username:
            raise CredentialResolutionError(dept_id, service, "missing username")
        headers[f"{prefix}-Username"] = username
        headers[f"{prefix}-Api-Token"] = api_token
        return headers

    if personal_token:
        if not username:
            raise CredentialResolutionError(dept_id, service, "missing username")
        headers[f"{prefix}-Username"] = username
        if service in {"jira", "confluence"} and _looks_like_cloud_atlassian(url):
            headers[f"{prefix}-Api-Token"] = personal_token
            return headers
        if service == "bitbucket" and _looks_like_bitbucket_cloud(url):
            if personal_token.startswith("ATCTT"):
                headers[f"{prefix}-Cloud-Access-Token"] = personal_token
            else:
                headers[f"{prefix}-App-Password"] = personal_token
            return headers
        headers[f"{prefix}-Personal-Token"] = personal_token
        return headers

    raise CredentialResolutionError(dept_id, service, "missing token")

#: Credential scopes accepted in P0+R2 (uyumluluk Q7).
#:
#: - ``"org"`` — worker bot scope (Vault path ``secret/atlassian/{dept_id}/...``).
#: - ``"user"`` — Streamlit per-user scope (Vault path
#:   ``secret/atlassian/_user_session/{session_id}/...``).
#:
#: ``"bot"`` is kept as a deprecated alias for ``"org"`` for backward
#: compatibility; calls passing ``scope="bot"`` are silently rerouted to
#: ``"org"`` after emitting a :class:`DeprecationWarning`.
_ALLOWED_SCOPES: frozenset[str] = frozenset({"org", "user"})

ScopeLiteral = Literal["org", "user"]


@asynccontextmanager
async def with_atlassian_creds(
    client: httpx.AsyncClient,
    *,
    dept_id: str,
    service: ServiceLiteral,
    credential_resolver,  # CredentialResolver-like (duck-typed)
    scope: ScopeLiteral = "org",
) -> AsyncIterator[httpx.AsyncClient]:
    """Inject Atlassian credentials into *client* headers for the with-block.

    On entry, reads credentials from Vault via *credential_resolver* and
    sets the three service-specific headers (``-Url``, ``-Username``,
    ``-Personal-Token``). On exit, restores the previous header values
    (or removes the keys if they did not exist before).

    Parameters
    ----------
    client:
        The :class:`httpx.AsyncClient` whose headers will be mutated.
    dept_id:
        Department identifier used for credential lookup.
    service:
        One of ``"jira"``, ``"bitbucket"``, ``"confluence"``.
    credential_resolver:
        An object with an async ``get(dept_id, service, scope=...)`` method
        that returns a credential object with ``url``, ``username``, and
        ``personal_token`` attributes.
    scope:
        Credential scope. Accepted values are ``"org"`` (worker bot,
        default; preserves backward compatibility with the previous
        ``"bot"`` semantics) and ``"user"`` (Streamlit per-user). The
        legacy value ``"bot"`` is silently rerouted to ``"org"`` after
        emitting a :class:`DeprecationWarning`. Any other value raises
        :class:`ValueError`.

    Yields
    ------
    httpx.AsyncClient
        The same *client* instance with credential headers injected.

    Raises
    ------
    ValueError
        If *scope* is not one of ``"org"``, ``"user"`` (after ``"bot"``
        alias resolution).
    CredentialResolutionError
        If the resolved credential is incomplete (any of url, username,
        or personal_token is empty/falsy).
    """
    if scope == "bot":
        warnings.warn(
            "scope='bot' is deprecated; use scope='org' instead",
            DeprecationWarning,
            stacklevel=2,
        )
        scope = "org"

    if scope not in _ALLOWED_SCOPES:
        raise ValueError(
            f"scope must be one of {sorted(_ALLOWED_SCOPES)!r}, got {scope!r}"
        )

    cred = await credential_resolver.get(dept_id, service, scope=scope)
    injected = _credential_headers(cred=cred, service=service, dept_id=dept_id)

    # Save existing header values (None means the key was absent).
    saved: dict[str, str | None] = {
        key: client.headers.get(key) for key in injected
    }

    # Inject credential headers.
    for key, value in injected.items():
        client.headers[key] = value

    try:
        yield client
    finally:
        # Restore original header state.
        for key, original_value in saved.items():
            if original_value is None:
                client.headers.pop(key, None)
            else:
                client.headers[key] = original_value
