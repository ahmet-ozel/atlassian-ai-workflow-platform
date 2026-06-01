"""User operations for Bitbucket Data Center and Cloud."""

import logging
import re
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_user

logger = logging.getLogger("mcp-atlassian.bitbucket.users")


# Cloud ``account_id`` shape validation (Requirement 13.3). A Cloud
# account identifier is either:
#
#   * a brace-wrapped UUID produced by legacy Bitbucket Cloud APIs, e.g.
#     ``{01234567-89ab-cdef-0123-456789abcdef}``; or
#   * the modern ``account_id`` string, which is a mix of ASCII letters,
#     digits, underscores, hyphens, and the ``:`` separator used by
#     Atlassian-issued identifiers (``557058:...``).
#
# DC-style usernames frequently contain characters outside these
# allowlists (periods, ``@``, spaces from display names, etc.), so when
# ``is_cloud`` is ``True`` we reject anything that matches neither shape
# *before* any outbound HTTP so the caller gets a structured
# ``invalid_target`` error instead of a Cloud 404.
_CLOUD_UUID_RE = re.compile(r"^\{[0-9a-f-]{36}\}$")
_CLOUD_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_:\-]+$")


def _is_cloud_account_id_shape(value: str) -> bool:
    """Return ``True`` iff ``value`` matches a Cloud ``account_id`` shape."""
    return bool(_CLOUD_UUID_RE.match(value) or _CLOUD_ACCOUNT_ID_RE.match(value))


class UsersMixin(BitbucketClient):
    """Mixin providing user lookup/search for Bitbucket.

    Bitbucket Server/DC does not expose a "current user" endpoint usable by
    PAT/OAuth (the canonical introspection endpoint requires the caller's
    own slug as input), so this mixin focuses on the practical needs:

    * Finding users by partial name when adding reviewers
    * Fetching a single user's profile

    On Cloud, ``search_users`` has no public equivalent and is guarded at the
    server layer; ``get_user`` targets ``GET /2.0/users/{account_id}`` and
    returns a normalized payload that exposes both DC (``name``/``slug``) and
    Cloud (``account_id``) identifier fields.
    """

    def get_user(self, user_slug: str) -> dict[str, Any]:
        """Fetch a user's profile.

        Args:
            user_slug: The user's slug on DC (typically the username). On
                Cloud, this value is interpreted as the ``account_id`` and
                forwarded to ``GET /2.0/users/{account_id}``. Cloud mode
                rejects values that do not match either the brace-wrapped
                UUID form ``^\\{[0-9a-f-]{36}\\}$`` or the ``account_id``
                form ``^[A-Za-z0-9_:\\-]+$`` with an ``invalid_target``
                ``ValueError`` *before* issuing any HTTP call
                (Requirement 13.3).

        Returns:
            User object. On DC this is the native shape with ``name``,
            ``displayName``, ``emailAddress``, etc. On Cloud the response is
            normalized so it exposes ``name``, ``slug``, and ``account_id``
            (plus the original Cloud fields like ``display_name`` / ``uuid``).

        Raises:
            ValueError: When running in Cloud mode and ``user_slug`` does
                not match the Cloud ``account_id`` shape. The message is
                prefixed with ``"invalid_target:"`` so the server-tool
                layer can map it to the structured ``invalid_target``
                error envelope listed in the feature's error-code
                allowlist.
        """
        if self.is_cloud:
            # Requirement 13.3 — pre-HTTP ``invalid_target`` guard.
            # Reject values that do not match the Cloud ``account_id``
            # shape before issuing any HTTP call so a DC-style username
            # returns a structured error instead of a 404.
            if not _is_cloud_account_id_shape(user_slug):
                raise ValueError(
                    "invalid_target: Bitbucket Cloud requires an "
                    "account_id for get_user (argument='username', "
                    "required_shape='cloud account_id'); got "
                    f"{user_slug!r}."
                )
            url = f"/2.0/users/{user_slug}"
            raw = self.bitbucket.get(url)
            if not isinstance(raw, dict):
                raise ValueError(f"Unexpected response for user {user_slug}: {raw}")
            normalized = normalize_user(raw)
            # ``normalize_user`` only returns ``None`` for a ``None`` input,
            # which cannot occur here because ``raw`` is a dict.
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/users/{user_slug}"
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response for user {user_slug}: {result}")
        return result

    def search_users(
        self,
        filter_text: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search for users.

        Args:
            filter_text: Substring matched against name/displayName/email
                (omit to list all visible users — typically rate-limited)
            limit: Maximum number of results

        Returns:
            List of user objects
        """
        url = "/rest/api/latest/users"
        params: dict[str, Any] = {}
        if filter_text:
            params["filter"] = filter_text

        return self._get_paged_results(url, params=params, limit=limit)
