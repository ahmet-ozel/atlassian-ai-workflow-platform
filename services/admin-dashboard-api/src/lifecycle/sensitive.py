"""Sensitive_Env_Key matcher - Python half of the TSPython ikiz modül.

This module is the **single source of truth** for which environment
variable keys count as a Sensitive_Env_Key on the Python side of the admin
dashboard control plane. The TypeScript twin lives at
``libs/web-shared/src/sensitive.ts`` and uses the **identical** regex strings
in the **identical** order so that invariant C4 (TSPython parity) holds
character-for-character.

Definition:

    Adı `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_DSN`,
    `*_CREDENTIAL`, `*_PRIVATE_*` örüntülerinden birine uyan ortam
    değişkeni anahtarı.

Concretely, a key is *sensitive* iff it ends with one of the suffixes
``_TOKEN``, ``_KEY``, ``_SECRET``, ``_PASSWORD``, ``_DSN``,
``_CREDENTIAL``, **or** contains the infix ``_PRIVATE_``. Bare names
like ``TOKEN`` (no leading underscore) do not match - this mirrors the
glob notation ``*_TOKEN``.

The module is intentionally **pure** (no I/O, no globals beyond the
compiled patterns) and safe to import from synchronous request
handlers, FastAPI dependencies, and invariant tests alike.
"""

from __future__ import annotations

import re

#: Compiled regex patterns identifying a Sensitive_Env_Key, in the exact
#: order shared with the TypeScript twin. The pattern strings must remain
#: **character-by-character identical** to the JavaScript
#: literals in ``libs/web-shared/src/sensitive.ts`` so invariant C4
#: (TSPython parity) can compare both sides on the same input set.
#:
#: We use :func:`re.search` semantics (no leading ``^``) so the suffix
#: anchors ``$`` line up with the JavaScript ``RegExp.test`` behaviour
#: on inputs without embedded newlines (the invariant test strategy
#: constrains keys to ``[A-Z][A-Z0-9_]{3,40}`` which excludes them by
#: construction).
SENSITIVE_ENV_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"_TOKEN$"),
    re.compile(r"_KEY$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_PASSWORD$"),
    re.compile(r"_DSN$"),
    re.compile(r"_CREDENTIAL$"),
    re.compile(r"_PRIVATE_"),
)


def is_sensitive_env_key(key: str) -> bool:
    """Return ``True`` iff ``key`` matches a Sensitive_Env_Key pattern.

    A key is sensitive when **any** of :data:`SENSITIVE_ENV_KEY_PATTERNS`
    finds a match within it. The check is case-sensitive - environment
    variable conventions in this codebase are uppercase-only - and uses
    :meth:`re.Pattern.search`, so the suffix anchors (``$``) bind to the
    end of the string and the infix pattern (``_PRIVATE_``) matches
    anywhere inside.

    Examples
    --------
    >>> is_sensitive_env_key("VAULT_TOKEN")
    True
    >>> is_sensitive_env_key("API_KEY")
    True
    >>> is_sensitive_env_key("DB_PRIVATE_HOST")
    True
    >>> is_sensitive_env_key("TOKEN")
    False
    >>> is_sensitive_env_key("LOG_LEVEL")
    False
    """

    return any(pattern.search(key) is not None for pattern in SENSITIVE_ENV_KEY_PATTERNS)


__all__ = ("SENSITIVE_ENV_KEY_PATTERNS", "is_sensitive_env_key")
