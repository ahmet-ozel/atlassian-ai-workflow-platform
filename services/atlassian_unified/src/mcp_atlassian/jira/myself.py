"""Module for Jira authenticated-user profile operations (DC /myself).

Implements Requirement 21: expose the authenticated user's profile while
defensively stripping any credential-like fields that might leak from the
upstream response. Secret-redaction at the server layer (``redact_secrets``)
is the primary defence; the defensive pop here covers the narrow case where
a field is named exactly ``password``, ``token``, or ``sessionCookie``
without any wrapping container.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


# Field names that must never leave ``get_myself`` even if the upstream
# endpoint were to include them. Matched case-insensitively on the top-level
# key only. Nested credential-like fields are handled by the generic
# ``redact_secrets`` walker applied at the server layer.
_CREDENTIAL_FIELDS: frozenset[str] = frozenset(
    {"password", "token", "sessioncookie"}
)


class MyselfMixin(JiraClient):
    """Mixin exposing the authenticated user's profile (read-only).

    Backed by the DC endpoint ``GET /rest/api/2/myself``. The returned
    payload typically contains ``name``, ``key``, ``displayName``,
    ``emailAddress``, ``timeZone``, ``locale``, ``avatarUrls``, and a
    nested ``groups`` object — none of which are credentials. This mixin
    nevertheless strips ``password``, ``token``, and ``sessionCookie``
    top-level keys if they appear, as a defence-in-depth measure.
    """

    def get_myself(self) -> dict[str, Any]:
        """Return the authenticated user's profile from ``/rest/api/2/myself``.

        Returns:
            The user profile dictionary (``name``, ``displayName``,
            ``emailAddress``, ``timeZone``, etc.) with any top-level
            ``password`` / ``token`` / ``sessionCookie`` fields removed.

        Raises:
            Exception: When the upstream call fails or returns a payload
                that is not a JSON object.
        """
        try:
            data = self.jira.get("rest/api/2/myself")
        except HTTPError as http_err:
            logger.error("Failed to fetch /myself: %s", http_err)
            raise Exception(f"Unable to fetch /myself: {http_err}") from http_err

        if not isinstance(data, dict):
            raise Exception(
                f"Unexpected response type for /myself: {type(data).__name__}"
            )

        # Defensive strip of credential-like top-level keys. Case-insensitive
        # match on the final key name only; nested secret-like keys are
        # redacted by the generic ``redact_secrets`` walker at the server
        # layer.
        for key in list(data.keys()):
            if key.casefold() in _CREDENTIAL_FIELDS:
                data.pop(key, None)

        return data
