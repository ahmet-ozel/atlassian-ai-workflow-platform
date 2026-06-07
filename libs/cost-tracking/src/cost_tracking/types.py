"""Shared literal types for the cost-tracking lib.

Mirrors the ``CHECK`` constraints declared by ``20_ops.sql`` on the
``shared.cost_tracking`` table so a typo at the application layer
becomes a static-type error rather than a runtime ``IntegrityError``.

For convenience this module re-exports :class:`CostEntry` from
:mod:`cost_tracking.tracker` so call sites can import the row shape
and the tag literals from a single module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:  # pragma: no cover - re-export only
    from .tracker import CostEntry

__all__ = [
    "COST_TAGS",
    "CostEntry",
    "CostTag",
    "PROVIDER_NAMES",
    "ProviderName",
]


#: Mirrors ``chk_cost_tracking_provider`` in ``20_ops.sql``.
ProviderName = Literal["vllm", "openai", "anthropic"]


PROVIDER_NAMES: Final[frozenset[str]] = frozenset(
    {"vllm", "openai", "anthropic"}
)


#: Mirrors ``chk_cost_tracking_cost_tag`` in ``20_ops.sql``.
#:
#: * ``"production"`` - bills against the dept budget (R5.5).
#: * ``"sandbox"`` - admin-dashboard prompt sandbox runs (R2.4); never
#:   counted by ``BudgetCapPolicy._usage(...)``.
#: * ``"probe"`` - connectivity probe LLM calls; never counted.
CostTag = Literal["production", "sandbox", "probe"]


COST_TAGS: Final[frozenset[str]] = frozenset(
    {"production", "sandbox", "probe"}
)



def __getattr__(name: str):
    """Lazy re-export of :class:`CostEntry` to avoid an import cycle.

    ``CostEntry`` lives in :mod:`cost_tracking.tracker` because that's
    where the row-shape and the validation rules naturally co-locate.
    Older call sites (and the property tests) still import it from
    :mod:`cost_tracking.types`; this module-level ``__getattr__``
    surfaces the symbol on demand without forcing a circular import
    at module load time.
    """

    if name == "CostEntry":
        from .tracker import CostEntry

        return CostEntry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
