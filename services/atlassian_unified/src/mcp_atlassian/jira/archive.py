"""Module for Jira issue archive/restore operations (DC 9.4+).

This mixin implements Requirement 26 from the atlassian-dc-tool-parity
feature: archive and restore individual issues via the Jira Data Center
REST API. Both endpoints were introduced in Jira Data Center 9.4, which
is the minimum supported version for these operations.

The mixin exposes only the raw REST calls. DC version gating is the
responsibility of the server layer (``servers/jira.py``) which uses
``check_dc_version(required="9.4")`` to emit a structured
``dc_version_too_old`` error before routing to this mixin per
Requirement 26.3. Reversible-receipt wrapping (Requirement 26.4) is
likewise built at the server layer; this mixin returns the minimal
operation-confirmation payload the server layer embeds into the
receipt.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class ArchiveMixin(JiraClient):
    """Mixin for Jira issue archive/restore operations (DC 9.4+).

    Endpoints:
        - ``PUT /rest/api/2/issue/{issue_key}/archive``
        - ``PUT /rest/api/2/issue/{issue_key}/restore``

    Both endpoints accept no request body and respond with
    ``204 No Content`` on success. The mixin therefore does not rely on
    the response payload; it constructs a deterministic confirmation
    dict keyed by the operation and the issue key, so the server layer
    can build a ``Reversible_Receipt`` without a second round-trip.
    """

    def archive_issue(self, issue_key: str) -> dict[str, Any]:
        """Archive a single issue.

        Calls ``PUT /rest/api/2/issue/{issue_key}/archive``. The
        endpoint returns ``204 No Content`` on success and the
        atlassian-python-api client surfaces this as ``None``; exceptions
        from the underlying HTTP layer propagate unchanged so the server
        layer can map them into the standard structured-error envelope.

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            A confirmation dictionary of the shape
            ``{"archived": True, "issue_key": issue_key}``.
        """
        self.jira.put(f"rest/api/2/issue/{issue_key}/archive")
        return {"archived": True, "issue_key": issue_key}

    def restore_issue(self, issue_key: str) -> dict[str, Any]:
        """Restore a previously archived issue.

        Calls ``PUT /rest/api/2/issue/{issue_key}/restore``. Like
        :meth:`archive_issue`, the endpoint returns ``204 No Content``
        on success; the mixin returns a deterministic confirmation dict
        rather than the empty response body.

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            A confirmation dictionary of the shape
            ``{"restored": True, "issue_key": issue_key}``.
        """
        self.jira.put(f"rest/api/2/issue/{issue_key}/restore")
        return {"restored": True, "issue_key": issue_key}
