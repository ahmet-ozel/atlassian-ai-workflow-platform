"""``make_client(env)`` - environment-driven backend factory.

Picks the concrete :class:`vault_client.client.VaultClient`
implementation based on the ``VAULT_BACKEND`` environment variable
(R6.6).

Mapping
-------

* ``VAULT_BACKEND=hashicorp`` → :class:`HashicorpBackend`. Requires
  ``VAULT_ADDR`` and ``VAULT_TOKEN``.
* ``VAULT_BACKEND=local-dev`` → :class:`LocalDevBackend`. Requires
  ``VAULT_LOCAL_KEY`` (32 bytes after hex/base64 decoding); rejects
  weak placeholder keys (R6.6 - plain-text rejected).
* Anything else (including missing) raises :class:`ValueError` so a
  misconfigured deployment fails fast at startup rather than silently
  picking a default.

The factory never *reads* secret material from the environment - it
only forwards the env mapping to the chosen backend's own constructor.
This keeps the factory itself trivially safe to log around.
"""

from __future__ import annotations

import logging
from typing import Mapping

from .client import VaultClient
from .hashicorp_backend import HashicorpBackend
from .local_dev_backend import LocalDevBackend

_LOG = logging.getLogger(__name__)


def make_client(env: Mapping[str, str]) -> VaultClient:
    """Build a :class:`VaultClient` from the supplied environment.

    Args:
        env: A mapping of environment variables. Pass
            ``os.environ`` in production; tests usually pass a plain
            ``dict`` so the call is deterministic.

    Returns:
        A concrete :class:`VaultClient` implementation.

    Raises:
        ValueError: If ``VAULT_BACKEND`` is missing, empty, or set to
            an unrecognised value, or if the chosen backend's required
            inputs are missing / malformed.
    """
    backend = env.get("VAULT_BACKEND", "").strip().lower()
    if not backend:
        raise ValueError(
            "VAULT_BACKEND is unset; expected 'hashicorp' or 'local-dev' "
            "(R6.6 pluggable backend selection)."
        )

    if backend == "hashicorp":
        addr = env.get("VAULT_ADDR", "").strip()
        token = env.get("VAULT_TOKEN", "")
        if not addr:
            raise ValueError(
                "VAULT_ADDR is required when VAULT_BACKEND=hashicorp"
            )
        if not token:
            raise ValueError(
                "VAULT_TOKEN is required when VAULT_BACKEND=hashicorp"
            )
        mount = env.get("VAULT_KV_MOUNT", "secret")
        return HashicorpBackend(addr=addr, token=token, mount=mount)

    if backend == "local-dev":
        # Visible-and-loud: this backend MUST NOT run in production.
        # The factory log here is the canonical "you are using local
        # storage" indicator that operators look for in CI / staging.
        _LOG.warning(
            "VAULT_BACKEND=local-dev is for development only; "
            "do not deploy this backend to production."
        )
        return LocalDevBackend.from_env(env)

    raise ValueError(
        f"unknown VAULT_BACKEND={backend!r}; expected 'hashicorp' or 'local-dev'"
    )


__all__ = ["make_client"]
