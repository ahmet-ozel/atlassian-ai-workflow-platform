"""Dev-secret boot guard (Y1).

The base ``infra/docker-compose.yml`` ships dev-only credentials
(``ai_dev_only``, ``dev-token-not-for-prod``,
``miniosecret_dev_only``, ``AUTH_PROVIDER=local``). The production
override (``docker-compose.prod.yml``) sets ``REJECT_DEV_SECRETS=true``
so any service that boots with this guard refuses to start when one of
the dev sentinels is detected. This prevents a misconfigured production
deploy from silently running with weak credentials.

Usage::

    from db_shared.secret_guard import enforce_no_dev_secrets

    # In the FastAPI lifespan startup, BEFORE any traffic is served:
    enforce_no_dev_secrets()        # respects REJECT_DEV_SECRETS env
    enforce_no_dev_secrets(force=True)  # ignore env, always check

The function raises :class:`DevSecretDetectedError` when a sentinel
matches; the lifespan should let this propagate so the container exits
with a non-zero code (systemd/Compose will restart-loop and operators
notice the failure).
"""

from __future__ import annotations

import os
from typing import Final, Mapping

__all__ = [
    "DEV_SECRET_SENTINELS",
    "DevSecretDetectedError",
    "detect_dev_secrets",
    "enforce_no_dev_secrets",
]


#: Mapping of env var name → list of forbidden values that mark the
#: variable as carrying a dev-only sentinel. Add new entries here when a
#: new dev default is introduced in the base compose. Values are matched
#: case-sensitively.
DEV_SECRET_SENTINELS: Final[Mapping[str, tuple[str, ...]]] = {
    "POSTGRES_PASSWORD": ("ai_dev_only",),
    "VAULT_TOKEN": ("dev-token-not-for-prod",),
    "VAULT_DEV_ROOT_TOKEN_ID": ("dev-token-not-for-prod",),
    "MINIO_ROOT_PASSWORD": ("miniosecret_dev_only",),
    # ``local`` AuthProvider on automation-service accepts any non-empty
    # bearer token — useful for dev but a security hole in production.
    "AUTH_PROVIDER": ("local",),
}


class DevSecretDetectedError(RuntimeError):
    """Raised by :func:`enforce_no_dev_secrets` when a sentinel matches.

    Attributes
    ----------
    matches : dict[str, str]
        ``{env_var: value}`` of every detected dev sentinel — useful
        for the audit log and the operator-facing error message.
    """

    def __init__(self, matches: dict[str, str]) -> None:
        self.matches = dict(matches)
        listed = ", ".join(f"{k}={v!r}" for k, v in sorted(matches.items()))
        super().__init__(
            "Dev-only secret sentinel(s) detected in production "
            f"environment: {listed}. Set REJECT_DEV_SECRETS=false to "
            "bypass (NOT recommended) or rotate to production values."
        )


def detect_dev_secrets(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return every env var whose value matches a sentinel.

    Pure function — does no I/O, accepts an explicit env mapping for
    testability. ``env=None`` falls back to ``os.environ``.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    hits: dict[str, str] = {}
    for var, sentinels in DEV_SECRET_SENTINELS.items():
        value = source.get(var, "")
        if value and value in sentinels:
            hits[var] = value
    return hits


def enforce_no_dev_secrets(
    *,
    env: Mapping[str, str] | None = None,
    force: bool = False,
) -> None:
    """Raise :class:`DevSecretDetectedError` if a sentinel is present.

    The check is OPT-IN: it only runs when ``REJECT_DEV_SECRETS=true``
    (or ``1``/``yes``) is set in the environment, OR when ``force=True``
    is passed. ``docker-compose.prod.yml`` sets the env var for every
    production service so a misconfigured production deploy fails fast.

    Parameters
    ----------
    env:
        Environment mapping to inspect. ``None`` → ``os.environ``.
    force:
        Bypass the ``REJECT_DEV_SECRETS`` opt-in and always enforce.
        Useful in tests.

    Raises
    ------
    DevSecretDetectedError
        When at least one sentinel matches.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    if not force:
        flag = source.get("REJECT_DEV_SECRETS", "").strip().lower()
        if flag not in ("true", "1", "yes", "on"):
            return

    matches = detect_dev_secrets(source)
    if matches:
        raise DevSecretDetectedError(matches)
