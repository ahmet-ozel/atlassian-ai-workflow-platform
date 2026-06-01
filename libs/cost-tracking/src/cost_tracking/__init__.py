"""cost-tracking: idempotent cost insert + dept-vs-global predictor.

Public surface for ``platform-mimari-ops`` tasks 7.1 (CostTracker) and
7.2 (CostPredictor). The package mirrors design.md §`CostTracker` and
§`CostPredictor`:

* :class:`CostTracker` — async idempotent insert into
  ``shared.cost_tracking`` (R5.4, Property 6).
* :func:`predict_cost` — pure function returning a :class:`CostPrediction`
  with a clear ``source`` flag for the audit fallback contract
  (R5.6, R5.7, Property 7).
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
