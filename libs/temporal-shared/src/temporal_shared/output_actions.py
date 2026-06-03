"""``output_actions`` partition + ApplyResult shape.

This module ships the *pure* part of the output-actions pipeline that
the :class:`AgentRunnerWorkflow` needs to wire its ``_execute_output_actions``
step: the deterministic partition between critical and best-effort
actions plus the :class:`ApplyResult` shape that records per-action
success / failure.

The full ``apply()`` orchestrator lives separately because it needs to
invoke activities — that decision belongs to the calling workflow, not
a shared helper. Keeping :func:`partition` and
:class:`ApplyResult` here gives the workflow body a single import
surface and a stable wire shape that property tests / unit tests can
assert against without mocking activity dispatch.

Classification:

* :data:`temporal_shared.messages.CRITICAL_OUTPUT_ACTION_KINDS` and
  :data:`temporal_shared.messages.BEST_EFFORT_OUTPUT_ACTION_KINDS` —
  the single-source-of-truth classification table.  ``partition``
  consults the kind, not the carried ``severity`` field, so a
  malformed action whose ``severity`` disagrees with the kind cannot
  bypass the policy; kind classification wins.

Replay safety: all helpers here are pure — no clocks, no randomness,
no globals.  They are safe to call from a Temporal workflow body or a
plain ``@dataclass``-only test.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from .messages import (
    BEST_EFFORT_OUTPUT_ACTION_KINDS,
    CRITICAL_OUTPUT_ACTION_KINDS,
    OutputAction,
)

__all__ = [
    "ApplyResult",
    "UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE",
    "partition",
]


#: Error message used when an :class:`OutputAction` carries a ``kind``
#: that is in neither :data:`CRITICAL_OUTPUT_ACTION_KINDS` nor
#: :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS`.  Exposed as a constant so
#: tests can match on the prefix without duplicating the literal.
UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE: Final[str] = (
    "OutputAction.kind is not classified in CRITICAL_OUTPUT_ACTION_KINDS "
    "or BEST_EFFORT_OUTPUT_ACTION_KINDS"
)


@dataclass(slots=True)
class ApplyResult:
    """Per-action success / failure record returned by the apply step.

    The four bookkeeping lists are typed as ``list[str]`` /
    ``list[tuple[str, str]]`` rather than ``list[OutputAction]`` so
    callers can render the lists into the final Jira comment via
    :func:`temporal_shared.output_size_cap.format_final_jira_comment`
    without re-extracting names.

    Fields
    ------
    successful_critical:
        Stable names of critical actions that succeeded, in the order
        they were applied.  Populated by the workflow body after each
        successful critical activity returns.
    failed_critical:
        ``(name, reason)`` tuples for critical actions that failed.
        A non-empty list aborts the run and triggers the compensation
        chain.
    successful_best_effort:
        Stable names of best-effort actions that succeeded.
    failed_best_effort:
        ``(name, reason)`` tuples for best-effort actions that failed.
        Surfaced verbatim in the final Jira comment but does NOT abort
        the run.

    The dataclass is mutable (``slots=True`` only — no ``frozen``) so
    the workflow body can append to the lists during a run without
    rebuilding the instance via :func:`dataclasses.replace`.  This is
    safe inside a Temporal workflow because each ``_execute_output_actions``
    invocation builds its own :class:`ApplyResult` and never shares
    the instance across replays — the result is fully reconstructed
    from the activity event history on each replay.
    """

    successful_critical: list[str] = field(default_factory=list)
    failed_critical: list[tuple[str, str]] = field(default_factory=list)
    successful_best_effort: list[str] = field(default_factory=list)
    failed_best_effort: list[tuple[str, str]] = field(default_factory=list)

    def has_critical_failure(self) -> bool:
        """True iff at least one critical action failed."""

        return bool(self.failed_critical)

    def has_best_effort_failure(self) -> bool:
        """True iff at least one best-effort action failed."""

        return bool(self.failed_best_effort)


def partition(
    actions: Iterable[OutputAction],
) -> tuple[tuple[OutputAction, ...], tuple[OutputAction, ...]]:
    """Split *actions* into ``(critical, best_effort)`` tuples.

    Classification is driven by ``action.kind`` — not ``action.severity``
    — so a deliberately mis-labelled action (severity ``"critical"``
    on a kind that lives in :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS`)
    cannot bypass the partition by setting its severity field
    independently.  This mirrors the classification invariant:
    ``CRITICAL_OUTPUT_ACTION_KINDS ∩ BEST_EFFORT_OUTPUT_ACTION_KINDS == ∅``
    and the workflow MUST trust the kind.

    Order preservation: each returned tuple preserves the relative
    ordering of its members from the input iterable so the workflow
    body applies actions in the same sequence the LLM emitted them.

    Parameters
    ----------
    actions:
        Iterable of :class:`OutputAction`.  May be empty.

    Returns
    -------
    tuple[tuple[OutputAction, ...], tuple[OutputAction, ...]]
        ``(critical, best_effort)`` — both immutable tuples so the
        caller cannot accidentally mutate the partition.

    Raises
    ------
    TypeError
        If any element is not an :class:`OutputAction`.
    ValueError
        If any element's ``kind`` is in neither classification set.
        The message is :data:`UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE`
        prefixed so callers can match on it.
    """
    critical: list[OutputAction] = []
    best_effort: list[OutputAction] = []
    for action in actions:
        if not isinstance(action, OutputAction):
            raise TypeError(
                "partition() expected OutputAction instances; "
                f"got {type(action).__name__}"
            )
        kind = action.kind
        if kind in CRITICAL_OUTPUT_ACTION_KINDS:
            critical.append(action)
        elif kind in BEST_EFFORT_OUTPUT_ACTION_KINDS:
            best_effort.append(action)
        else:
            raise ValueError(
                f"{UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE}: {kind!r}"
            )
    return tuple(critical), tuple(best_effort)
