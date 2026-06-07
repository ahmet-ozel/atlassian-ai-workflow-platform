"""cost-tracking: idempotent cost insert + dept-vs-global predictor.

Public surface for ``CostTracker`` and ``CostPredictor``:

* :class:`CostTracker` - async idempotent insert into
  ``shared.cost_tracking``.
* :func:`predict_cost` - pure function returning a :class:`CostPrediction`
  with a clear ``source`` flag for the audit fallback contract.
"""

from .predictor import (
    CostPrediction,
    DeptCostHistory,
    GlobalCostHistory,
    GLOBAL_FALLBACK_MIN_TASKS,
    predict_cost,
)
from .tracker import CostEntry, CostTracker, CostTrackerStore
from .types import CostTag, ProviderName

__all__ = [
    "CostEntry",
    "CostPrediction",
    "CostTag",
    "CostTracker",
    "CostTrackerStore",
    "DeptCostHistory",
    "GlobalCostHistory",
    "GLOBAL_FALLBACK_MIN_TASKS",
    "ProviderName",
    "predict_cost",
]
