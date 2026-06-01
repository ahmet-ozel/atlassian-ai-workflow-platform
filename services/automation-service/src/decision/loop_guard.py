"""Loop guard predicates for webhook event filtering.

Pure functions that determine whether a webhook event should be processed
or skipped based on the actor identity, assignee identity, and changelog
content. These predicates prevent infinite loops caused by the bot
processing its own generated events.

The bot registry is accepted as a ``frozenset[str]`` of account IDs.
No I/O is performed; all functions are deterministic and side-effect-free.

Requirements: 2.5, 2.6, 2.7, 2.8, 2.9, 2.13, 3.5
"""

from __future__ import annotations

from typing import Any, Literal

# Supported event types that the platform processes.
_ACCEPTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "jira:issue_created",
        "jira:issue_assigned",
        "jira:issue_updated",
        "jira:comment_created",
        "pullrequest:reviewer_added",
        "pullrequest:comment_created",
    }
)


def is_self_actor(actor_id: str | None, bot_account_ids: frozenset[str]) -> bool:
    """Check whether the event actor is one of the registered bots.

    Parameters
    ----------
    actor_id:
        The ``accountId`` of the user who triggered the webhook event.
        May be ``None`` for system-generated events.
    bot_account_ids:
        Frozen set of all registered bot account IDs across departments.

    Returns
    -------
    bool
        ``True`` if the actor is a bot (event should be skipped);
        ``False`` otherwise.
    """
    if actor_id is None:
        return False
    return actor_id in bot_account_ids


def is_bot_assignee(
    assignee_id: str | None, bot_account_ids: frozenset[str]
) -> bool:
    """Check whether the assignee is a registered bot.

    Used for ``jira:issue_created`` and ``jira:issue_assigned`` events
    to determine if the bot was assigned to the issue.

    Parameters
    ----------
    assignee_id:
        The ``accountId`` of the issue assignee. May be ``None`` if
        the issue is unassigned.
    bot_account_ids:
        Frozen set of all registered bot account IDs across departments.

    Returns
    -------
    bool
        ``True`` if the assignee is a registered bot;
        ``False`` otherwise (including when assignee is ``None``).
    """
    if assignee_id is None:
        return False
    return assignee_id in bot_account_ids


def assignee_changed_to_bot(
    changelog: dict[str, Any] | None, bot_account_ids: frozenset[str]
) -> bool:
    """Check whether the changelog contains an assignee change to a bot.

    Inspects the ``items`` list within the changelog for a field item
    where ``field == "assignee"`` and the ``to`` value is in the bot
    registry.

    Parameters
    ----------
    changelog:
        The changelog object from a ``jira:issue_updated`` event.
        Expected structure::

            {
                "items": [
                    {"field": "assignee", "to": "<account_id>", ...},
                    {"field": "status", "to": "In Progress", ...},
                    ...
                ]
            }

        May be ``None`` for events without a changelog.
    bot_account_ids:
        Frozen set of all registered bot account IDs across departments.

    Returns
    -------
    bool
        ``True`` if the changelog contains an assignee field change
        where the ``to`` value is a registered bot account ID;
        ``False`` for empty changelogs, changelogs without an assignee
        item, and assignee-removed cases (``to`` is ``None`` or empty).
    """
    if changelog is None:
        return False

    items = changelog.get("items")
    if not items:
        return False

    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("field") != "assignee":
            continue
        to_value = item.get("to")
        if to_value is None:
            continue
        if to_value in bot_account_ids:
            return True

    return False


def route(event_type: str) -> Literal["accepted", "ignored"]:
    """Classify a webhook event type as accepted or ignored.

    Parameters
    ----------
    event_type:
        The webhook event type string (e.g., ``"jira:issue_created"``,
        ``"pullrequest:reviewer_added"``).

    Returns
    -------
    Literal["accepted", "ignored"]
        ``"accepted"`` if the event type is in the supported set;
        ``"ignored"`` otherwise.
    """
    if event_type in _ACCEPTED_EVENT_TYPES:
        return "accepted"
    return "ignored"
