"""Module for Jira issue notification operations (Requirement 17).

This module exposes :class:`NotificationsMixin`, a :class:`JiraClient`
subclass that wires the ``POST /rest/api/2/issue/{key}/notify`` Jira DC
endpoint. The endpoint is broadcast-capable (it sends email) and therefore
lives in the opt-in ``toolset:jira_notifications`` toolset (see Req 17.2,
47.1).

The mixin itself does not enforce opt-in or produce receipts; those are
cross-cutting concerns wired at the server-tool registration layer. This
module's responsibility is limited to building the exact JSON request body
shape documented by Jira DC and reporting the effective recipient count so
the caller can construct a reversible-receipt note (Req 17.3).
"""

from __future__ import annotations

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class NotificationsMixin(JiraClient):
    """Mixin for Jira issue notification operations."""

    def notify_issue(
        self,
        issue_key: str,
        *,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        to_watchers: bool = False,
        to_voters: bool = False,
        to_reporter: bool = False,
        to_assignee: bool = False,
        to_users: list[str] | None = None,
        to_groups: list[str] | None = None,
        restrict_groups: list[str] | None = None,
        restrict_permissions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Send an email notification about an issue (Req 17.1).

        Posts to ``/rest/api/2/issue/{issue_key}/notify`` with a body
        shaped per the Jira DC REST reference:

        .. code-block:: json

            {
              "subject": "...",
              "textBody": "...",
              "htmlBody": "...",
              "to": {
                "reporter": false,
                "assignee": false,
                "watchers": true,
                "voters": false,
                "users": [{"name": "alice"}],
                "groups": [{"name": "jira-users"}]
              },
              "restrict": {
                "groups": [{"name": "..."}],
                "permissions": [{"key": "BROWSE"}]
              }
            }

        Optional fields are omitted rather than sent as ``None`` so Jira
        does not reject the payload. ``htmlBody`` is only included when
        the caller supplies it; ``to.users`` / ``to.groups`` are only
        included when non-empty; and ``restrict`` is only included when
        at least one of ``restrict_groups`` / ``restrict_permissions`` is
        non-empty.

        Args:
            issue_key: The issue key (e.g. ``"PROJ-123"``) that the
                notification is about.
            subject: Email subject line (required by Jira DC).
            text_body: Plain-text body of the email (required by Jira
                DC).
            html_body: Optional HTML body; omitted from the payload when
                ``None``.
            to_watchers: Send to issue watchers.
            to_voters: Send to issue voters.
            to_reporter: Send to the issue reporter.
            to_assignee: Send to the issue assignee.
            to_users: Optional list of usernames (DC ``name`` field) to
                notify directly.
            to_groups: Optional list of group names to notify.
            restrict_groups: Optional list of group names; recipients
                outside these groups are filtered out by Jira before
                delivery.
            restrict_permissions: Optional list of permission keys (for
                example ``"BROWSE"``); recipients without these
                permissions are filtered out by Jira before delivery.

        Returns:
            A dict ``{"recipient_count": int}`` where ``recipient_count``
            is the caller-visible count of requested recipients computed
            by summing the four booleans and the lengths of the
            ``to_users`` / ``to_groups`` lists. This count feeds the
            ``recipient_scope`` field of the reversible receipt
            constructed at the server-tool layer (Req 17.3).
        """
        to_block: dict[str, Any] = {
            "reporter": bool(to_reporter),
            "assignee": bool(to_assignee),
            "watchers": bool(to_watchers),
            "voters": bool(to_voters),
        }
        if to_users:
            # Jira DC identifies users by ``name`` (username); Atlassian
            # Cloud uses ``accountId`` (opaque, typically contains a
            # colon, e.g. "557058:aabbccdd..."). A single user list has
            # to work for both deployment shapes, so each entry is
            # inspected: values that look like a Cloud account id are
            # sent under ``accountId`` and everything else under the
            # legacy ``name`` key. This matches Jira's REST behavior —
            # the notify payload accepts either field per user entry.
            user_entries: list[dict[str, str]] = []
            for u in to_users:
                if isinstance(u, str) and ":" in u:
                    user_entries.append({"accountId": u})
                else:
                    user_entries.append({"name": u})
            to_block["users"] = user_entries
        if to_groups:
            to_block["groups"] = [{"name": g} for g in to_groups]

        payload: dict[str, Any] = {
            "subject": subject,
            "textBody": text_body,
            "to": to_block,
        }
        if html_body is not None:
            payload["htmlBody"] = html_body

        if restrict_groups or restrict_permissions:
            restrict_block: dict[str, Any] = {}
            if restrict_groups:
                restrict_block["groups"] = [{"name": g} for g in restrict_groups]
            if restrict_permissions:
                restrict_block["permissions"] = [
                    {"key": p} for p in restrict_permissions
                ]
            payload["restrict"] = restrict_block

        endpoint = f"rest/api/2/issue/{issue_key}/notify"
        logger.info(
            "Sending Jira notification for %s (subject=%r)", issue_key, subject
        )
        self.jira.post(endpoint, json=payload)

        recipient_count = (
            int(bool(to_watchers))
            + int(bool(to_voters))
            + int(bool(to_reporter))
            + int(bool(to_assignee))
            + (len(to_users) if to_users else 0)
            + (len(to_groups) if to_groups else 0)
        )
        return {"recipient_count": recipient_count}
