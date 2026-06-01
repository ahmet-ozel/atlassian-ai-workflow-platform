"""Pure-function helpers for AgentRunnerWorkflow saga compensation.

This module encapsulates the saga compensation logic referenced by
:class:`src.workflows.agent_runner_workflow.AgentRunnerWorkflow` in a
form that is

* **deterministic** — no wallclock, no randomness, no hidden state;
* **pure** — given the same history and inverse table, ``compensate_actions``
  always invokes the same inverses in the same order;
* **testable in isolation** — the helper accepts an arbitrary inverse table
  (``Mapping[str, Callable]``) so unit/property tests can pass mocks
  without spinning up a Temporal worker or any external service.

Design reference: ``.kiro/specs/p0-critical-path/design.md`` §"AgentRunnerWorkflow"
and Property 10 ("Saga compensation determinism and idempotence").
Validates Requirement 6.8.

Compensation contract
---------------------

For an execution history ``H = [a_1, a_2, ..., a_k]`` of completed
side-effecting activities (recorded append-only on the workflow), the
``compensate_actions(H, inverse_table)`` function MUST satisfy *all* of
the following invariants:

1. **Reverse order.** The inverse for ``a_k`` runs first, then ``a_{k-1}``,
   then ... ``a_1``. No reordering is permitted.
2. **Inverse-only.** *Only* recorded actions trigger inverses; an action
   whose ``name`` is absent from ``inverse_table`` (read-only ops such as
   ``jira_get_issue``) is skipped silently.
3. **Idempotence.** Running compensation twice is equivalent to running
   it once: the second pass operates on an empty list (the workflow
   discards the history after compensation) and is a no-op.
4. **Determinism.** Order of inverse invocations is a pure function of
   ``H``; no random shuffling and no clock-dependent ordering.
5. **Empty history.** ``compensate_actions([], _)`` is a no-op — no
   inverse is invoked.

Compensation P0 inverse table
-----------------------------

Per design.md, the P0 saga compensation table is::

    bitbucket_create_branch -> bitbucket_delete_branch
    artifact_upload         -> artifact_delete

All other completed activities (``bitbucket_create_commit``,
``bitbucket_open_pr``, ``confluence_create_page``, ``llm_*``, ...) have
*no* inverse in P0 and are skipped during compensation. ``bitbucket_delete_branch``
and ``artifact_delete`` are themselves idempotent (404 acceptable).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompensableAction:
    """A single completed side-effecting activity recorded for saga rollback.

    The workflow appends one of these to its ``_recorded_actions`` list
    after every successful side-effecting activity. On failure, the list
    is fed to :func:`compensate_actions` (in order) to roll the world back.

    Attributes
    ----------
    name : str
        The forward activity name as registered with Temporal — for
        example ``"bitbucket_create_branch"`` or ``"artifact_upload"``.
        This is the lookup key into the inverse table; if it is absent
        from the table the action is treated as having no inverse.
    inverse_args : tuple[Any, ...]
        Positional arguments to pass to the inverse activity. Stored
        as a tuple so the dataclass remains hashable / frozen and so
        property tests can rely on by-value equality.
    inverse_kwargs : Mapping[str, Any]
        Keyword arguments to pass to the inverse activity. Stored as a
        mapping for ergonomics; tests that need by-value equality
        compare the materialised ``dict``.

    Notes
    -----
    The dataclass intentionally records *the inverse call shape* rather
    than the forward call shape. Forward activities and their inverses
    rarely share a signature (``bitbucket_create_branch`` returns a
    ``BranchInfo`` while ``bitbucket_delete_branch`` takes ``repo, branch,
    dept_id``), so threading the inverse arguments through at record
    time avoids any branching at compensation time.
    """

    name: str
    inverse_args: tuple[Any, ...] = field(default_factory=tuple)
    inverse_kwargs: Mapping[str, Any] = field(default_factory=dict)


#: Type alias for an inverse activity. Inverses are expected to be
#: ``async`` callables in production (Temporal activities), but the
#: helper does not enforce that — synchronous callables are accepted by
#: ``compensate_actions_sync`` for unit/property tests that prefer to
#: keep their assertions free of an event loop.
InverseCallable = Callable[..., Awaitable[None]]
SyncInverseCallable = Callable[..., None]


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------


def plan_compensation(
    history: Sequence[CompensableAction],
    inverse_table: Mapping[str, Any],
) -> list[CompensableAction]:
    """Return the ordered list of actions whose inverses MUST be invoked.

    This is the deterministic, side-effect-free *planner* underneath
    :func:`compensate_actions`. It returns a fresh list — the input
    ``history`` is never mutated — containing the subset of recorded
    actions that have a registered inverse, in **reverse** of the order
    they were appended.

    Parameters
    ----------
    history :
        The append-only list of completed side-effecting activities.
    inverse_table :
        Lookup table mapping forward-activity names to their inverse
        callables. Actions whose ``name`` is not a key in the table are
        skipped (read-only or no-op activities).

    Returns
    -------
    list[CompensableAction]
        The actions whose inverses to invoke, in compensation order.
        Empty list if ``history`` is empty or no recorded action has
        a registered inverse.
    """

    return [a for a in reversed(list(history)) if a.name in inverse_table]


# ---------------------------------------------------------------------------
# Synchronous executor (for tests; the workflow body uses the async one)
# ---------------------------------------------------------------------------


def compensate_actions_sync(
    history: Sequence[CompensableAction],
    inverse_table: Mapping[str, SyncInverseCallable],
) -> list[tuple[str, tuple[Any, ...], Mapping[str, Any]]]:
    """Synchronously invoke inverses for a recorded history.

    Mirrors :func:`compensate_actions` but uses *synchronous* callables;
    intended for property tests and unit tests where the inverses are
    instrumented mocks. Returns the list of ``(name, args, kwargs)``
    tuples in the order they were invoked, so tests can make
    by-value assertions without inspecting mock state.

    Behaviour matches the contract described in the module docstring:

    * Reverse-order over ``history``.
    * Skips actions whose ``name`` is absent from ``inverse_table``.
    * No-op when ``history`` is empty.
    * Pure function of ``history`` and ``inverse_table`` (no random,
      no clock).

    Inverse callables are invoked once each and are responsible for
    their own idempotence (production inverses
    ``bitbucket_delete_branch`` / ``artifact_delete`` already are).

    Parameters
    ----------
    history :
        The completed-actions list to compensate.
    inverse_table :
        Mapping of forward-activity name to a synchronous inverse callable.

    Returns
    -------
    list[tuple[str, tuple[Any, ...], Mapping[str, Any]]]
        The invocation log: one entry per inverse call, in invocation order.
    """

    plan = plan_compensation(history, inverse_table)
    invocations: list[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = []
    for action in plan:
        inverse = inverse_table[action.name]
        inverse(*action.inverse_args, **dict(action.inverse_kwargs))
        invocations.append(
            (action.name, action.inverse_args, dict(action.inverse_kwargs))
        )
    return invocations


# ---------------------------------------------------------------------------
# Async executor (production wiring; the workflow body awaits this)
# ---------------------------------------------------------------------------


async def compensate_actions(
    history: Sequence[CompensableAction],
    inverse_table: Mapping[str, InverseCallable],
) -> list[tuple[str, tuple[Any, ...], Mapping[str, Any]]]:
    """Asynchronously invoke inverses for a recorded history.

    The production counterpart of :func:`compensate_actions_sync`.
    Inverses are ``await``-ed sequentially in compensation order; any
    exception raised by an inverse is *swallowed* by Temporal at the
    activity boundary in normal usage (the inverses are idempotent
    activities with their own retry policy), but this helper deliberately
    does **not** add a try/except around each call so unit tests can
    assert that buggy inverses still surface.

    See :func:`compensate_actions_sync` for the full behaviour contract.
    """

    plan = plan_compensation(history, inverse_table)
    invocations: list[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = []
    for action in plan:
        inverse = inverse_table[action.name]
        await inverse(*action.inverse_args, **dict(action.inverse_kwargs))
        invocations.append(
            (action.name, action.inverse_args, dict(action.inverse_kwargs))
        )
    return invocations


__all__ = [
    "CompensableAction",
    "InverseCallable",
    "SyncInverseCallable",
    "plan_compensation",
    "compensate_actions_sync",
    "compensate_actions",
]
