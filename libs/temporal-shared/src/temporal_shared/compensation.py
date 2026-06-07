"""Pure compensation-chain constants and report dataclasses.

This module is the **single source of truth** for the cancel +
compensation chain ordering and report shapes.

The actual side-effecting :func:`run` activity body is **not** in
scope for this module - it lives in the worker layer
(``platform/workers/agent-runner-worker``) so it can talk to Jira /
Bitbucket / Confluence / MinIO.  Here we expose only the closed
vocabulary, the deterministic order, and the pure dataclasses the
worker uses to build a :class:`CompensationReport`.

Public API
----------

* :data:`COMPENSATION_STEPS` - fixed-order tuple of activity step
  names.
* :class:`CompensationContext` - re-exported from
  :mod:`temporal_shared.messages` for caller ergonomics
  (``from temporal_shared.compensation import CompensationContext``).
* :class:`CompensationReport` - frozen dataclass returned by the
  worker's chain runner.
* :data:`STEP_RESULT_OK`, :data:`STEP_RESULT_FAILED`,
  :data:`STEP_RESULT_SKIPPED` - outcome string vocabulary.

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from temporal_shared.messages import CompensationContext, CompensationReason

__all__ = [
    "COMPENSATION_STEPS",
    "CompensationContext",
    "CompensationReason",
    "CompensationReport",
    "StepOutcome",
    "STEP_RESULT_OK",
    "STEP_RESULT_FAILED",
    "STEP_RESULT_SKIPPED",
]


# ---------------------------------------------------------------------------
# Step vocabulary - the closed set of activity names dispatched by
# the cancel + compensation chain. The order is part of the contract;
# reordering would change the user-visible
# cleanup behaviour and would silently break compensation idempotency
# on re-cancel. Tests assert the order verbatim.
# ---------------------------------------------------------------------------

COMPENSATION_STEPS: Final[tuple[str, ...]] = (
    "close_draft_pr_if_open",
    "delete_ai_branch_if_unused",
    "label_confluence_page_cancelled",
    "leave_minio_artifacts_for_retention",
    "post_cancel_jira_comment",
    "transition_jira_issue_if_configured",
)


# ---------------------------------------------------------------------------
# Step outcome vocabulary
# ---------------------------------------------------------------------------

#: One step succeeded (the side effect was applied or already present).
STEP_RESULT_OK: Final[str] = "ok"

#: One step failed after exhausting its retry budget.  The chain
#: continues regardless; failures on a single step do not
#: abort the chain).
STEP_RESULT_FAILED: Final[str] = "failed"

#: One step was a no-op because the target side effect was never
#: written in the first place (e.g. ``branch is None`` ⇒ skip
#: ``delete_ai_branch_if_unused``).
STEP_RESULT_SKIPPED: Final[str] = "skipped"

#: Closed-vocabulary type alias for step results.  Using ``Literal``
#: rather than ``str`` lets ``mypy`` / ``pyright`` catch typos in the
#: worker layer when the chain runner builds a report.
StepOutcome = Literal["ok", "failed", "skipped"]


# ---------------------------------------------------------------------------
# Report dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompensationReport:
    """Per-cancel summary of every compensation step's outcome.

    The chain runner constructs one of these as it walks
    :data:`COMPENSATION_STEPS`; the final value is surfaced in the
    audit log + final Jira comment so operators can see every step
    that ran.  The dataclass is frozen + immutable so the value
    survives Temporal replay verbatim.

    Attributes
    ----------
    attempted_steps:
        Tuple of step names actually attempted (in the order they
        ran).  In the happy path this is identical to
        :data:`COMPENSATION_STEPS`; in the early-abort path it is a
        prefix. The chain *continues* on failure so
        ``len(attempted_steps) == len(COMPENSATION_STEPS)`` should be
        the norm.
    step_results:
        Tuple of ``(step_name, outcome)`` pairs aligned with
        ``attempted_steps``.  ``outcome`` is one of
        :data:`STEP_RESULT_OK`, :data:`STEP_RESULT_FAILED`,
        :data:`STEP_RESULT_SKIPPED`.
    """

    attempted_steps: tuple[str, ...] = ()
    step_results: tuple[tuple[str, StepOutcome], ...] = ()

    def __post_init__(self) -> None:
        # Defensive: keep ``attempted_steps`` and ``step_results``
        # aligned so an ill-formed report from the worker layer
        # surfaces as a construction error rather than a downstream
        # parsing surprise.  Frozen dataclasses are constructed via
        # ``object.__setattr__`` so we cannot fix the inputs here;
        # we only validate.
        if len(self.attempted_steps) != len(self.step_results):
            raise ValueError(
                "CompensationReport: attempted_steps and step_results "
                "must align (lengths "
                f"{len(self.attempted_steps)} vs "
                f"{len(self.step_results)})"
            )
        for step_name, outcome in self.step_results:
            if step_name not in COMPENSATION_STEPS:
                raise ValueError(
                    f"CompensationReport: unknown step name "
                    f"{step_name!r}; must be one of "
                    f"{COMPENSATION_STEPS!r}"
                )
            if outcome not in (
                STEP_RESULT_OK,
                STEP_RESULT_FAILED,
                STEP_RESULT_SKIPPED,
            ):
                raise ValueError(
                    f"CompensationReport: unknown outcome "
                    f"{outcome!r} for step {step_name!r}; must be one "
                    "of ok / failed / skipped"
                )


# Silence the unused-import warning on ``field``: kept available for
# future extension of :class:`CompensationReport` with default-factory
# fields without re-importing.
_ = field
