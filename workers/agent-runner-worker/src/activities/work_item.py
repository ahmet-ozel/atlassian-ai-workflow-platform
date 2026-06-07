"""Work-item state-machine activities for AutomationWorkflow / AgentRunnerWorkflow.

This module owns the canonical state machine for ``automation.work_items``
rows and exposes:

- :class:`InvalidWorkItemTransition` - raised when callers attempt a
  status transition that is not in the allowed-edge set.
- :func:`validate_work_item_transition` - a *pure* helper (no DB, no
  Temporal runtime) that enforces the state-machine contract. This is
  the entry point exercised by state-machine tests.
- :func:`update_work_item_status` - a Temporal activity that issues the
  ``UPDATE automation.work_items SET status = ...`` statement, but only
  after the same pure validator has accepted the transition.

Allowed transitions:

    pending  → running
    pending  → failed
    running  → completed
    running  → failed

Self-loops (``s → s`` for every status) are also accepted as idempotent
no-ops. Every other ordered pair is rejected.

"""

from __future__ import annotations

from typing import Any, Final

from temporalio import activity

# ---------------------------------------------------------------------------
# State-machine vocabulary
# ---------------------------------------------------------------------------

#: All status values permitted by the ``chk_work_items_status`` CHECK
#: constraint on ``automation.work_items.status``.
WORK_ITEM_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "running", "completed", "failed"}
)

#: Forward edges in the work-item state machine (excluding self-loops).
#:
#: This is a tuple of ``(from_status, to_status)`` ordered pairs. Self-loops are NOT included
#: here because they're handled by the validator without needing an
#: explicit table entry.
_FORWARD_EDGES: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("pending", "running"),
        ("pending", "failed"),
        ("running", "completed"),
        ("running", "failed"),
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidWorkItemTransition(ValueError):
    """Raised when a forbidden ``work_items.status`` transition is attempted.

    The error carries the rejected ``(from_status, to_status)`` pair so
    callers (and Temporal failure histories) can audit the violation
    without parsing the message.

    Inherits from :class:`ValueError` so existing call sites that catch
    ``ValueError`` for input-validation errors continue to work.
    """

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"invalid work_item transition: "
            f"{from_status!r} -> {to_status!r} is not in the allowed-edge "
            f"set {{pending->running, pending->failed, "
            f"running->completed, running->failed}} (self-loops allowed)"
        )


# ---------------------------------------------------------------------------
# Pure validator entry point
# ---------------------------------------------------------------------------


def is_valid_work_item_transition(from_status: str, to_status: str) -> bool:
    """Return ``True`` iff ``(from_status, to_status)`` is an allowed edge.

    The function is total over ``WORK_ITEM_STATUSES × WORK_ITEM_STATUSES``
    and returns ``False`` for any pair where either side is not in the
    canonical status set. Self-loops are accepted for every valid status.

    Parameters
    ----------
    from_status:
        The current status read from the row before the proposed update.
    to_status:
        The status the caller wants to write.

    Returns
    -------
    bool
        ``True`` if the transition is allowed (forward edge or self-loop
        on a valid status), ``False`` otherwise.
    """

    if from_status not in WORK_ITEM_STATUSES:
        return False
    if to_status not in WORK_ITEM_STATUSES:
        return False
    if from_status == to_status:
        # Self-loops are always allowed for valid statuses (idempotent
        # update - the workflow may re-issue the same status if a Temporal
        # activity is retried after the row has already been updated).
        return True
    return (from_status, to_status) in _FORWARD_EDGES


def validate_work_item_transition(from_status: str, to_status: str) -> None:
    """Raise :class:`InvalidWorkItemTransition` if the edge is forbidden.

    This is the canonical guard for every code path that mutates
    ``automation.work_items.status``. Both the pure helper and the
    Temporal activity below funnel through it so the state-machine
    invariant cannot be bypassed by a clever-but-wrong caller.

    Parameters
    ----------
    from_status:
        The current status as read from the row.
    to_status:
        The proposed new status.

    Raises
    ------
    InvalidWorkItemTransition
        If ``(from_status, to_status)`` is not an allowed edge per
        :func:`is_valid_work_item_transition`.
    """

    if not is_valid_work_item_transition(from_status, to_status):
        raise InvalidWorkItemTransition(from_status, to_status)


# ---------------------------------------------------------------------------
# Temporal activity
# ---------------------------------------------------------------------------


@activity.defn(name="update_work_item_status")
async def update_work_item_status(
    workflow_id: str,
    new_status: str,
    db_pool: Any,
) -> None:
    """Validate and apply a ``work_items.status`` update.

    The activity reads the current status for ``workflow_id`` inside the
    same transaction as the update so the validation observes a
    consistent snapshot. If ``(current_status, new_status)`` is not an
    allowed edge per :func:`validate_work_item_transition`, the
    transaction is rolled back and :class:`InvalidWorkItemTransition`
    propagates out of the activity (Temporal will then surface it as an
    application failure).

    Parameters
    ----------
    workflow_id:
        The unique ``automation.work_items.workflow_id`` key. Matches the
        Temporal workflow id assigned by the ingestion handler.
    new_status:
        The status the caller wants to write. Must be one of
        :data:`WORK_ITEM_STATUSES`.
    db_pool:
        An ``asyncpg``-style pool (or duck-typed equivalent) exposing
        ``acquire()`` as an async context manager that yields a
        connection with ``transaction()``, ``fetchval()``, and
        ``execute()`` coroutines. Injected at worker startup so the
        activity stays testable without a real Postgres.

    Raises
    ------
    InvalidWorkItemTransition
        If the transition violates the state machine.
    LookupError
        If no row exists for ``workflow_id``.
    """

    # Fast pre-flight: catch obviously invalid target status before
    # touching the database (saves a round-trip and gives a clearer
    # error). The full ``(from, to)`` validation runs after we read the
    # current status under transaction.
    if new_status not in WORK_ITEM_STATUSES:
        raise InvalidWorkItemTransition("<unknown>", new_status)

    activity.heartbeat(f"updating work_item {workflow_id} -> {new_status}")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            current_status = await conn.fetchval(
                "SELECT status FROM automation.work_items "
                "WHERE workflow_id = $1 FOR UPDATE",
                workflow_id,
            )
            if current_status is None:
                raise LookupError(
                    f"no work_items row for workflow_id={workflow_id!r}"
                )

            # Funnel through the same pure validator the property test
            # exercises so the state machine has exactly one definition.
            validate_work_item_transition(current_status, new_status)

            # Self-loop is a no-op write but we still UPDATE so
            # ``updated_at`` advances - that gives operators a freshness
            # signal without changing the observable status path.
            await conn.execute(
                "UPDATE automation.work_items "
                "SET status = $1, updated_at = now() "
                "WHERE workflow_id = $2",
                new_status,
                workflow_id,
            )


__all__ = [
    "InvalidWorkItemTransition",
    "WORK_ITEM_STATUSES",
    "is_valid_work_item_transition",
    "update_work_item_status",
    "validate_work_item_transition",
]
