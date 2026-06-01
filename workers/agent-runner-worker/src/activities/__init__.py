"""Activity modules for the agent-runner-worker.

This package provides Temporal activity implementations for Jira,
Bitbucket, Confluence, LLM, OpenCode, and artifact operations.

The credential resolver is a shared dependency injected at worker startup
and accessed by activities via :func:`get_credential_resolver`.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Shared credential resolver registry
# ---------------------------------------------------------------------------

_credential_resolver: Any | None = None


def set_credential_resolver(resolver: Any) -> None:
    """Set the shared credential resolver for all activities.

    Called once during worker startup (in ``main.py``) after connecting
    to Postgres and Vault.

    Parameters
    ----------
    resolver:
        A :class:`CredentialResolver` instance (or duck-typed equivalent)
        with an async ``get(dept_id, service, scope=...)`` method.
    """
    global _credential_resolver  # noqa: PLW0603
    _credential_resolver = resolver


def get_credential_resolver() -> Any:
    """Retrieve the shared credential resolver.

    Raises
    ------
    RuntimeError
        If the resolver has not been set (worker misconfiguration).
    """
    if _credential_resolver is None:
        raise RuntimeError(
            "Credential resolver not initialized. "
            "Call set_credential_resolver() during worker startup."
        )
    return _credential_resolver
