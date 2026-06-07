"""vault_client - pluggable Vault KV / rotation client.

Re-exports the public API so callers can simply do::

    from vault_client import VaultPath, make_client, VaultClient

Concrete backends (:class:`HashicorpBackend`, :class:`LocalDevBackend`)
are exported as well for direct construction in tests; production
code should go through :func:`make_client` so backend selection stays
driven by ``VAULT_BACKEND``.
"""

from .client import (
    Backend,
    RotationResult,
    SshKey,
    VaultClient,
)
from .factory import make_client
from .hashicorp_backend import HashicorpBackend
from .local_dev_backend import KEY_SIZE, LocalDevBackend
from .path import (
    NOTIFICATION_SLACK_PATH_TEMPLATE,
    NOTIFICATION_SMTP_PATH,
    USER_PERSISTED_PATH_TEMPLATE,
    USER_SESSION_PATH_TEMPLATE,
    VaultPath,
)
from .ssh_keys import (
    finalize as finalize_ssh_rotation,
    generate_keypair as generate_ssh_keypair,
    read_active as read_active_ssh_key,
    read_previous as read_previous_ssh_key,
    read_rotation_meta as read_ssh_rotation_meta,
    rotate as rotate_ssh_key,
)
from .webhook_hmac import verify_webhook_hmac
from .webhook_secrets import (
    finalize as finalize_webhook_rotation,
    generate_secret as generate_webhook_secret,
    read_overlap_remaining as read_webhook_overlap_remaining,
    read_rotation_meta as read_webhook_rotation_meta,
    rotate as rotate_webhook_secret,
)

__all__ = [
    "Backend",
    "HashicorpBackend",
    "KEY_SIZE",
    "LocalDevBackend",
    "NOTIFICATION_SLACK_PATH_TEMPLATE",
    "NOTIFICATION_SMTP_PATH",
    "RotationResult",
    "SshKey",
    "USER_PERSISTED_PATH_TEMPLATE",
    "USER_SESSION_PATH_TEMPLATE",
    "VaultClient",
    "VaultPath",
    "finalize_ssh_rotation",
    "finalize_webhook_rotation",
    "generate_ssh_keypair",
    "generate_webhook_secret",
    "make_client",
    "read_active_ssh_key",
    "read_previous_ssh_key",
    "read_ssh_rotation_meta",
    "read_webhook_overlap_remaining",
    "read_webhook_rotation_meta",
    "rotate_ssh_key",
    "rotate_webhook_secret",
    "verify_webhook_hmac",
]
