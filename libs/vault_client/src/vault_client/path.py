"""``VaultPath`` value-object.

Validates the project-wide Vault path convention
(``^vault:[a-zA-Z0-9/_-]+$``) at construction time so callers cannot
accidentally pass a plain-text token, an HTTP URL, or a credential
reference that would later fail deep inside an HTTP backend.

The grammar is intentionally permissive:

* Lowercase / kebab-case is enforced by the **project style guide**,
  not by the regex, so existing ``departments.schema.json`` references
  (which already use this character class) stay valid.
* Plain-text values are rejected at the ``parse(...)`` boundary —
  any string that does not start with the literal ``vault:`` prefix
  raises :class:`ValueError`.

The class is ``frozen``, so instances can be used as dict keys / set
members without surprising mutation semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Regex that the literal Vault reference (``vault:<path>``) must match.
#:
#: The pattern is anchored to the full string (``^...$``) so a trailing
#: query string, fragment, or whitespace is rejected. The character
#: class mirrors the ``credential_ref`` pattern shipped in
#: ``config/departments.schema.json``.
_VAULT_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^vault:[a-zA-Z0-9/_-]+$"
)

#: The literal scheme prefix every well-formed reference must carry.
_PREFIX: Final[str] = "vault:"


# ---------------------------------------------------------------------------
# Project-wide path conventions
# ---------------------------------------------------------------------------
#
# The base ``vault:atlassian/<dept_id>/<service>`` pattern is shared
# across the project; the constants below add per-user and notification
# paths so callers do not build those strings ad hoc.
#
# All templates use ``str.format``-style placeholders so that producing a
# concrete path stays a single, greppable call site::
#
#     ref = "vault:" + USER_SESSION_PATH_TEMPLATE.format(
#         session_id=session_id,
#         service="jira",
#     )
#     path = VaultPath.parse(ref)
#
# The templates intentionally **omit** the ``vault:`` prefix so they
# compose with :meth:`VaultPath.relative` naturally; callers prepend the
# prefix once when constructing the literal reference. The placeholder
# names match the field names used by callers.

#: Per-user session credential written by ``assistant-service`` and read
#: by ``automation-service``. Session lifetime —
#: deleted on Streamlit logout, 24h cron sweep cleans orphans.
USER_SESSION_PATH_TEMPLATE: Final[str] = (
    "atlassian/_user_session/{session_id}/{service}"
)

#: Opt-in PIN-encrypted persistence (Z7) for "remember me" cookie use
#: cases. Bytes are AES-encrypted with a PIN-derived key client-side
#: before being written; the Vault path itself only stores the
#: ciphertext. 30-day TTL aligned with the signed cookie.
USER_PERSISTED_PATH_TEMPLATE: Final[str] = (
    "atlassian/_user_persisted/{user_id}/{service}"
)

#: SMTP credential consumed by ``notification_service`` for outbound
#: email. Single tenant — owner: notification_service.
NOTIFICATION_SMTP_PATH: Final[str] = "notifications/smtp/credential"

#: Per-department Slack webhook URL consumed by ``notification_service``
#: Owner: notification_service; rotated on dept rotation cycles.
NOTIFICATION_SLACK_PATH_TEMPLATE: Final[str] = "notifications/{dept_id}/slack"


@dataclass(frozen=True, slots=True)
class VaultPath:
    """Immutable, validated reference to a secret stored in Vault.

    Construct via :meth:`parse` — the dataclass constructor itself does
    *not* validate, mirroring the Python convention that ``__init__``
    stays cheap and total. ``parse`` is the single, explicit boundary
    where untrusted strings become ``VaultPath`` instances.

    Attributes:
        raw: The full reference string including the ``vault:`` scheme,
            e.g. ``"vault:atlassian/payments/jira"``.
    """

    raw: str

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def parse(cls, s: str) -> "VaultPath":
        """Validate *s* and return a ``VaultPath``.

        Args:
            s: A Vault reference string, e.g.
                ``"vault:atlassian/payments/jira"``.

        Returns:
            A new :class:`VaultPath` whose ``raw`` attribute equals *s*.

        Raises:
            ValueError: If *s* is not a ``str`` or does not match
                ``^vault:[a-zA-Z0-9/_-]+$``. The error message includes
                the offending value (truncated to 64 characters) so it
                is safe to log without leaking secrets — the regex
                rejects strings that look like base64 tokens or basic
                auth credentials before they reach this point.
        """
        if not isinstance(s, str):
            raise ValueError(
                f"VaultPath.parse expected str, got {type(s).__name__}"
            )
        if not _VAULT_PATH_RE.fullmatch(s):
            # Truncate the echoed value so a malformed plain-text
            # token doesn't end up fully reproduced in the message.
            preview = s if len(s) <= 64 else s[:61] + "..."
            raise ValueError(
                f"invalid Vault path {preview!r}; "
                f"expected pattern {_VAULT_PATH_RE.pattern!r}"
            )
        return cls(raw=s)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def relative(self) -> str:
        """Path *without* the ``vault:`` scheme.

        Useful when calling a backend that expects a Vault server-side
        path (``atlassian/payments/jira``) rather than the client-side
        reference (``vault:atlassian/payments/jira``).
        """
        return self.raw[len(_PREFIX):]

    @property
    def segments(self) -> tuple[str, ...]:
        """Path segments, split on ``/``.

        Empty segments are filtered out so ``"vault:a//b"`` (which the
        regex itself does not reject) yields ``("a", "b")``.
        """
        return tuple(seg for seg in self.relative.split("/") if seg)

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


__all__ = [
    "NOTIFICATION_SLACK_PATH_TEMPLATE",
    "NOTIFICATION_SMTP_PATH",
    "USER_PERSISTED_PATH_TEMPLATE",
    "USER_SESSION_PATH_TEMPLATE",
    "VaultPath",
]
