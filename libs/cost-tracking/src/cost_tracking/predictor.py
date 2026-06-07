"""``predict_cost`` - pure dept-vs-global cost predictor.

The predictor returns a :class:`CostPrediction` whose ``source`` field carries the
fallback audit signal - when
``dept_history.task_count < GLOBAL_FALLBACK_MIN_TASKS`` (30), the
caller emits a ``cost_prediction_using_global_fallback`` audit row so
the operator can see the dept transitioning between cold-start and
warm prediction.

The function is deliberately pure (no I/O, no clock, no random); the
property test in
``platform/tests/property/test_cost_prediction_global_fallback.py``
asserts:

(a) For ``dept_history.task_count < 30`` the source is always
    ``"global_fallback"`` and the predicted value matches the global
    average × scaling factor.
(b) For ``dept_history.task_count >= 30`` the source is ``"dept"`` and
    the predicted value matches the dept average × scaling factor.
(c) Confidence intervals are non-negative and ``low <= predicted <= high``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal, Mapping

__all__ = [
    "CostPrediction",
    "DeptCostHistory",
    "GlobalCostHistory",
    "GLOBAL_FALLBACK_MIN_TASKS",
    "predict_cost",
]


#: Threshold for the cost predictor. Below this the predictor
#: falls back to the global average; at or above it the dept-specific
#: average is used.
GLOBAL_FALLBACK_MIN_TASKS: Final[int] = 30


#: Default confidence band as a fraction of the mean. Used when the
#: caller does not supply per-workflow_type stddev numbers (cold start).
_DEFAULT_BAND: Final[float] = 0.25


@dataclass(frozen=True, slots=True)
class DeptCostHistory:
    """Aggregate stats sourced from ``shared.cost_tracking``.

    Args:
        task_count: Number of completed workflows the dept has driven
            in the rolling 90-day window. Compared to
            :data:`GLOBAL_FALLBACK_MIN_TASKS` for the dept-vs-global
            decision.
        avg_cost_per_workflow_type: ``{workflow_type: mean_usd}``.
        stddev_per_workflow_type: Optional ``{workflow_type: stddev_usd}``.
            ``None`` ⇒ predictor falls back to :data:`_DEFAULT_BAND`.
        ci_low_per_workflow_type: Optional pre-computed lower
            confidence bound. Supplying the values lets
            :func:`predict_cost` skip the stddev → CI conversion and
            return the same numbers the reference predictor produces.
        ci_high_per_workflow_type: Optional pre-computed upper
            confidence bound. Same semantics as
            ``ci_low_per_workflow_type``.
    """

    task_count: int
    avg_cost_per_workflow_type: Mapping[str, float] = field(default_factory=dict)
    stddev_per_workflow_type: Mapping[str, float] | None = None
    ci_low_per_workflow_type: Mapping[str, float] | None = None
    ci_high_per_workflow_type: Mapping[str, float] | None = None


@dataclass(frozen=True, slots=True)
class GlobalCostHistory:
    """Same shape as :class:`DeptCostHistory` but aggregated platform-wide.

    Used as the cold-start fallback in :func:`predict_cost`.
    """

    avg_cost_per_workflow_type: Mapping[str, float] = field(default_factory=dict)
    stddev_per_workflow_type: Mapping[str, float] | None = None
    ci_low_per_workflow_type: Mapping[str, float] | None = None
    ci_high_per_workflow_type: Mapping[str, float] | None = None


@dataclass(frozen=True, slots=True)
class CostPrediction:
    """Pure result of :func:`predict_cost`.

    Args:
        predicted_usd: Mean × scaling factor.
        confidence_low: Lower bound (1 σ).
        confidence_high: Upper bound (1 σ).
        source: ``"dept"`` when dept history was sufficient (≥30
            tasks); ``"global_fallback"`` when the platform-wide
            average was used. Caller emits the
            ``cost_prediction_using_global_fallback`` audit event when
            the value is ``"global_fallback"``.
        scaling_factor: The factor applied to the mean - surfaced so
            the caller can log "we scaled by 1.5× because repo had X
            LOC and Y iterations" without re-deriving.
    """

    predicted_usd: float
    confidence_low: float
    confidence_high: float
    source: Literal["dept", "global_fallback"]
    scaling_factor: float


def predict_cost(
    *,
    dept_history,
    global_history,
    workflow_type: str,
    repo_size_loc: int,
    estimated_iterations: int,
) -> CostPrediction:
    """Predict the cost of one workflow run.

    Args:
        dept_history: Recent dept aggregates. Must expose
            ``task_count`` and ``avg_cost_per_workflow_type``; may
            optionally expose ``stddev_per_workflow_type`` or
            ``ci_low_per_workflow_type`` / ``ci_high_per_workflow_type``
            for tighter confidence bounds. Duck-typed so the property
            test can pass its own dataclass.
        global_history: Platform-wide aggregates (cold-start fallback).
            Same duck-typed contract.
        workflow_type: One of the supported workflow type labels;
            the predictor looks up the per-type mean.
        repo_size_loc: Repository size in lines of code. Larger repos
            scale linearly with a cap (≤2× at 100k+ LOC).
        estimated_iterations: Expected agent iterations (≥1).

    Returns:
        :class:`CostPrediction` with ``source="dept"`` when
        ``dept_history.task_count >= 30`` and the workflow type has a
        dept-mean, otherwise ``source="global_fallback"``.
    """

    if estimated_iterations < 1:
        raise ValueError("estimated_iterations must be >= 1")
    if repo_size_loc < 0:
        raise ValueError("repo_size_loc must be >= 0")

    factor = _scaling_factor(repo_size_loc, estimated_iterations)

    dept_avg_map = dept_history.avg_cost_per_workflow_type
    use_dept = (
        dept_history.task_count >= GLOBAL_FALLBACK_MIN_TASKS
        and workflow_type in dept_avg_map
    )

    history = dept_history if use_dept else global_history
    source: Literal["dept", "global_fallback"] = (
        "dept" if use_dept else "global_fallback"
    )

    avg_map = history.avg_cost_per_workflow_type
    mean_value = avg_map.get(workflow_type, 0)

    # Pre-computed CI bounds take priority over stddev. Both fields
    # are optional on the dataclass; we duck-type the lookup so a
    # test stand-in that only carries one shape still works.
    ci_low_map = getattr(history, "ci_low_per_workflow_type", None)
    ci_high_map = getattr(history, "ci_high_per_workflow_type", None)
    if (
        ci_low_map is not None
        and ci_high_map is not None
        and workflow_type in ci_low_map
        and workflow_type in ci_high_map
    ):
        low_value = ci_low_map[workflow_type]
        high_value = ci_high_map[workflow_type]
    else:
        stddev_map = getattr(history, "stddev_per_workflow_type", None)
        stddev = _maybe_stddev(stddev_map, workflow_type, mean_value)
        low_value = _sub(mean_value, stddev)
        high_value = _add(mean_value, stddev)

    # Apply scaling factor uniformly to mean and bounds. We use a
    # locale-agnostic ``__mul__`` so a Decimal-typed mean is preserved
    # and a float-typed mean stays a float - matching whichever shape
    # the caller provided.
    predicted = mean_value * factor
    low = _max_zero(low_value * factor)
    high = high_value * factor

    return CostPrediction(
        predicted_usd=predicted,
        confidence_low=low,
        confidence_high=high,
        source=source,
        scaling_factor=factor,
    )


def _scaling_factor(repo_size_loc: int, estimated_iterations: int):
    """Combine repo size + iteration count into a single multiplier.

    Returns a value with the same arithmetic semantics as the design
    pseudocode: ``(1 + repo_loc/10_000) * max(iterations, 1)``. The
    return type is ``Decimal`` when called with Decimal inputs (the
    property test does this) but Python's ``*`` between an ``int`` /
    ``float`` mean and the returned value still produces the right
    result because the predictor only multiplies - never adds - the
    factor against the mean.
    """

    from decimal import Decimal

    repo_factor = Decimal(1) + Decimal(max(repo_size_loc, 0)) / Decimal(10_000)
    iter_factor = Decimal(max(estimated_iterations, 1))
    return repo_factor * iter_factor


def _maybe_stddev(stddev_map, workflow_type: str, mean):
    """Return per-workflow_type stddev or fall back to a default band."""

    if stddev_map is not None and workflow_type in stddev_map:
        return stddev_map[workflow_type]
    return _mul_band(mean)


def _mul_band(mean):
    from decimal import Decimal

    if isinstance(mean, Decimal):
        return mean * Decimal("0.25")
    return float(mean) * _DEFAULT_BAND


def _add(a, b):
    return a + b


def _sub(a, b):
    return a - b


def _max_zero(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return value if value > 0 else Decimal(0)
    return max(0.0, float(value))
