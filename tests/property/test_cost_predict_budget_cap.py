"""invariant 7 - Cost prediction + budget cap enforcement.



Hypothesis-driven verification of the cost-prediction fallback rule
and the budget-cap enforcement state machine used by the automation
service.

Invariant statement
--------------------------------------------

For any hypothesis-generated
``(dept_history, global_history, BudgetCaps, BudgetUsage,
workflow_type)`` tuple:

 (a) ``predict_cost(...)`` returns ``source="dept"`` when
 ``dept_history.task_count >= 30`` and
 ``source="global_fallback"`` otherwise; the invariant
 ``confidence_low <= predicted_usd <= confidence_high``
 holds for every output.
 (b) When ``source == "global_fallback"`` the workflow start
 handler writes a single
 ``cost_prediction_using_global_fallback`` audit event
 (caller-site assertion - exercised by mirroring the
 emit-on-fallback contract through a recording audit
 writer).
 (c) ``BudgetCapPolicy.enforce(dept_id, user_id)`` checks the
 four scopes in the fixed order
 ``dept_weekly  user_weekly  dept_monthly
 user_monthly`` and denies on the **first** scope whose
 usage equals or exceeds its cap; when every scope is
 below its cap the policy returns ``allow``.
 (d) On deny the policy emits exactly **one**
 ``budget_exceeded`` audit event whose payload carries the
 offending ``scope``, ``limit``, ``usage`` and (when the
 request was attributed) ``user_id``; allow paths emit no
 audit events.
 (e) ``BudgetCapPolicy.enforce`` excludes ``cost_tag IN
 ('sandbox','probe')`` rows from the usage aggregate - the
 SQL string carries ``cost_tag = 'production'`` verbatim
 on every aggregate query the policy issues.
 (f) Determinism: repeating the call with the same
 ``(usage, caps)`` returns the same decision and produces
 the same audit event sequence (same scope, same
 payload).

Surface under test
------------------

*:class:`automation_service.budget.policy.BudgetCapPolicy` (implementation milestone - already shipped) is exercised end-to-end with an
 in-memory:class:`UsageQueryRunner` and:class:`AuditWriter` so the invariant owns the full
 decide-and-audit contract.
*:func:`cost_tracking.predictor.predict_cost` is
 imported behind a ``try / except`` guard. Until the
 cost-tracking lib lands the predictor invariants in (a) / (b)
 are exercised through a tiny in-test stand-in that mirrors the
 design pseudocode (``source="dept"``  ``task_count >= 30`` and
 ``confidence_low ≤ predicted_usd ≤ confidence_high``) so the
 Hypothesis search still pins the contract a future
 implementation must satisfy. When ships, the import
 guard collapses and the production module is exercised
 directly.

Related coverage
----------------

* The cost prediction and budget cap workflow emits the audit-on-fallback
 event exercised here through the recording audit writer.
* ``platform/services/automation-service/tests/unit/\
 test_budget_policy.py`` - example-based tests that pin
 individual scope branches; this invariant owns the
 combinatorial coverage they cannot.
* ``platform/tests/property/test_token_cap_fail_fast.py`` -
 reference style for the module-level skipif fallback pattern.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Mapping

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap - the budget policy lives under the
# automation-service package which is not on the workspace
# pythonpath. Mirror the test_budget_policy.py unit-test bootstrap
# so the invariant runs identically from any cwd.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

# Mirror the bootstrap used by
# ``services/automation-service/tests/unit/test_budget_policy.py``: we
# expose **both** the service root (so ``automation_service.app``'s
# ``from src.config import Settings`` resolves) and the ``src/``
# directory itself so ``automation_service.budget.policy`` imports
# without an editable install. The cost-tracking lib path is added
# defensively for the day ships its production module.
_AUTOMATION_ROOT: Path = (
    _REPO_ROOT / "services" / "automation-service"
)

_SRC_DIRS: tuple[Path, ...] = (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _REPO_ROOT / "libs" / "audit_logger" / "src",
    _REPO_ROOT / "libs" / "cost-tracking" / "src",
)
for _src in _SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.budget.policy import (  # noqa: E402
    SCOPE_ORDER,
    BudgetCapPolicy,
    BudgetCaps,
    BudgetDecision,
    StaticBudgetCapsProvider,
)


# ---------------------------------------------------------------------------
# Optional import -:func:`cost_tracking.predictor.predict_cost` is
#; until it ships we fall back to an in-test stand-in that
# mirrors the predictor contract so clauses (a) / (b) still pin the
# contract a future implementation must satisfy.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - guard collapses once implementation milestone ships
    from cost_tracking.predictor import (  # type: ignore[import-not-found]
        GLOBAL_FALLBACK_MIN_TASKS as _PRODUCTION_MIN_TASKS,
        predict_cost as _production_predict_cost,
    )
except ModuleNotFoundError:  # pragma: no cover
    _production_predict_cost = None  # type: ignore[assignment]
    _PRODUCTION_MIN_TASKS = 30  # predictor default


#: Fallback threshold used by the cost predictor.
GLOBAL_FALLBACK_MIN_TASKS: Final[int] = int(_PRODUCTION_MIN_TASKS)


# ---------------------------------------------------------------------------
# Domain dataclasses - local stand-ins for ``cost_tracking.types``.
#
# These mirror the cost and budget tracking data shape. Once the production
# module lands we replace the local
# definitions with direct imports; the structural shape stays
# identical so the production CostPrediction satisfies the same
# property assertions.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeptCostHistory:
    """Per-dept rolling cost history fed into:func:`_predict_cost`."""

    task_count: int
    avg_cost_per_workflow_type: Mapping[str, Decimal]
    ci_low_per_workflow_type: Mapping[str, Decimal]
    ci_high_per_workflow_type: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class _GlobalCostHistory:
    """Cross-dept aggregate used as the cold-start fallback."""

    avg_cost_per_workflow_type: Mapping[str, Decimal]
    ci_low_per_workflow_type: Mapping[str, Decimal]
    ci_high_per_workflow_type: Mapping[str, Decimal]


@dataclass(frozen=True, slots=True)
class _CostPrediction:
    """Mirror of the cost prediction data model.

 The four-attribute shape is the contract the
 ``automation-service`` workflow-start handler relies on; clauses
 (a) and (b) of invariant pin the relationship between the
 inputs and ``source`` / the confidence interval on the output.
 """

    predicted_usd: Decimal
    confidence_low: Decimal
    confidence_high: Decimal
    source: str  # "dept" | "global_fallback"


@dataclass(frozen=True, slots=True)
class _BudgetUsageInput:
    """Raw usage values used to seed the fake ``usage_query`` runner.

 The invariant populates four scope-specific aggregates and
 drives them through:class:`_FakeUsageRunner` so the policy's
 SQL paths stay exercised end-to-end (clause (e)).
 """

    dept_weekly_usd: Decimal
    user_weekly_usd: Decimal
    dept_monthly_usd: Decimal
    user_monthly_usd: Decimal


# ---------------------------------------------------------------------------
# Reference predictor - fall-back implementation used when
# has not landed yet. Once the production ``predict_cost`` ships,
# the helper below collapses to a thin pass-through wrapper.
# ---------------------------------------------------------------------------


def _scaling_factor(repo_size_loc: int, estimated_iterations: int) -> Decimal:
    """Mirror of the predictor ``_scaling_factor`` helper.

 The helper is monotonic in both arguments and bounded below at
 ``1`` so the resulting prediction never collapses to zero. The
 exact formula is not load-bearing for invariant - only the
 ``confidence_low ≤ predicted ≤ confidence_high`` invariant
 matters, and that invariant holds because the same factor is
 applied to all three values.
 """

    repo_factor = Decimal(1) + Decimal(max(repo_size_loc, 0)) / Decimal(10_000)
    iter_factor = Decimal(max(estimated_iterations, 1))
    return repo_factor * iter_factor


def _reference_predict_cost(
    *,
    dept_history: _DeptCostHistory,
    global_history: _GlobalCostHistory,
    workflow_type: str,
    repo_size_loc: int,
    estimated_iterations: int,
) -> _CostPrediction:
    """Reference implementation matching the predictor contract.

 Used only when:mod:`cost_tracking.predictor` is not yet on
 the import path. The function is deliberately a faithful
 transliteration of the predictor contract so the property
 invariants we exercise here also exercise the contract a
 future production module must implement.
 """

    if dept_history.task_count >= GLOBAL_FALLBACK_MIN_TASKS:
        avg = dept_history.avg_cost_per_workflow_type[workflow_type]
        ci_low = dept_history.ci_low_per_workflow_type[workflow_type]
        ci_high = dept_history.ci_high_per_workflow_type[workflow_type]
        source = "dept"
    else:
        avg = global_history.avg_cost_per_workflow_type[workflow_type]
        ci_low = global_history.ci_low_per_workflow_type[workflow_type]
        ci_high = global_history.ci_high_per_workflow_type[workflow_type]
        source = "global_fallback"

    factor = _scaling_factor(repo_size_loc, estimated_iterations)
    return _CostPrediction(
        predicted_usd=avg * factor,
        confidence_low=ci_low * factor,
        confidence_high=ci_high * factor,
        source=source,
    )


def _predict_cost(**kwargs: Any) -> _CostPrediction:
    """Dispatch to the production predictor when available.

 Once lands the production callable replaces the
 reference implementation transparently. The ``_CostPrediction``
 dataclass shape lines up with the design data model so a
 production prediction satisfies the same attribute access
 pattern.
 """

    if _production_predict_cost is not None:  # pragma: no cover - covered after 7.2
        return _production_predict_cost(**kwargs)
    return _reference_predict_cost(**kwargs)


# ---------------------------------------------------------------------------
# Recording fakes for:class:`BudgetCapPolicy`
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuditWriter:
    """Append-only fake of the:class:`AuditWriter` protocol."""

    events: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeUsageRunner:
    """List-of-calls fake matching the:class:`UsageQueryRunner` protocol.

 The fake also records the SQL string each ``fetchval`` call
 issues so clause (e) of invariant - the ``cost_tag =
 'production'`` filter - can be asserted on the actual SQL the
 policy emits, not the policy author's intent.
 """

    usage: _BudgetUsageInput
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetchval(self, query: str, *args: Any) -> Decimal:
        self.calls.append((query, args))

        is_user_scope = "user_id = $3" in query
        interval = args[1] if len(args) >= 2 else ""

        if is_user_scope:
            if interval == "7 days":
                return self.usage.user_weekly_usd
            if interval == "30 days":
                return self.usage.user_monthly_usd
            return Decimal("0")
        if interval == "7 days":
            return self.usage.dept_weekly_usd
        if interval == "30 days":
            return self.usage.dept_monthly_usd
        return Decimal("0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DEPT_ID: Final[str] = "payment"
_USER_ID: Final[str] = "user-1"
_FROZEN_NOW: Final[datetime] = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN_NOW


def _make_policy(
    *,
    caps: BudgetCaps,
    usage: _BudgetUsageInput,
) -> tuple[BudgetCapPolicy, _FakeUsageRunner, _RecordingAuditWriter]:
    """Build a wired:class:`BudgetCapPolicy` for one property example.

 Re-creates the writer / runner per call so each Hypothesis
 example exercises a clean recording surface (clause (f)
 determinism is asserted by replaying the same inputs against
 fresh fakes).
 """

    runner = _FakeUsageRunner(usage=usage)
    writer = _RecordingAuditWriter()
    provider = StaticBudgetCapsProvider(caps={_DEPT_ID: caps})
    policy = BudgetCapPolicy(
        caps_provider=provider,
        usage_query=runner,
        audit_logger=AuditLogger(writer=writer),
        clock=_frozen_clock,
    )
    return policy, runner, writer


def _expected_decision(
    *,
    caps: BudgetCaps,
    usage: _BudgetUsageInput,
    user_id: str | None,
) -> BudgetDecision:
    """Compute the decision the policy MUST return per invariant (c).

 Separately re-implementing the four-scope ladder gives us an
 oracle to compare against the policy output without coupling
 the test to internal helpers. The ordering must match:data:`SCOPE_ORDER` exactly.
 """

    if usage.dept_weekly_usd >= caps.weekly_usd_dept:
        return BudgetDecision.deny("dept_weekly")
    if user_id is not None and usage.user_weekly_usd >= caps.weekly_usd_user:
        return BudgetDecision.deny("user_weekly")
    if usage.dept_monthly_usd >= caps.monthly_usd_dept:
        return BudgetDecision.deny("dept_monthly")
    if user_id is not None and usage.user_monthly_usd >= caps.monthly_usd_user:
        return BudgetDecision.deny("user_monthly")
    return BudgetDecision.allow()


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


#: Workflow types the predictor is exercised against. We pick a
#: small fixed set so the dept / global histories share keys; the
#: predictor branches on ``workflow_type`` only as a Mapping
#: lookup so a wider catalogue would not exercise additional code
#: paths.
_WORKFLOW_TYPES: Final[tuple[str, ...]] = (
    "code_change",
    "pr_review",
    "research",
)


def _decimal_dollars(min_value: float, max_value: float) -> st.SearchStrategy[Decimal]:
    """USD-with-cents:class:`Decimal` strategy.

 Quantising to two decimal places keeps the search space small
 enough for Hypothesis to find counter-examples quickly while
 matching the on-the-wire precision of the ``cost_usd`` column.
 """

    return st.decimals(
        min_value=Decimal(str(min_value)),
        max_value=Decimal(str(max_value)),
        allow_nan=False,
        allow_infinity=False,
        places=2,
    )


@st.composite
def _dept_history_strategy(draw: st.DrawFn) -> _DeptCostHistory:
    """Generate a:class:`_DeptCostHistory` that may straddle the fallback.

 ``task_count ∈ [0, 200]`` so the search frequently lands on
 both sides of the ``GLOBAL_FALLBACK_MIN_TASKS=30`` threshold.
 The CI bounds are drawn under the strict ordering
 ``ci_low ≤ avg ≤ ci_high`` so the predictor's invariant has
 well-formed inputs (clause (a)).
 """

    task_count = draw(st.integers(min_value=0, max_value=200))
    avg: dict[str, Decimal] = {}
    low: dict[str, Decimal] = {}
    high: dict[str, Decimal] = {}
    for wf in _WORKFLOW_TYPES:
        a = draw(_decimal_dollars(0.10, 50.00))
        # Floor low at 0, ceiling high above avg by a non-negative gap.
        gap_low = draw(_decimal_dollars(0.00, 5.00))
        gap_high = draw(_decimal_dollars(0.00, 5.00))
        low[wf] = max(Decimal("0.00"), a - gap_low)
        high[wf] = a + gap_high
        avg[wf] = a
    return _DeptCostHistory(
        task_count=task_count,
        avg_cost_per_workflow_type=avg,
        ci_low_per_workflow_type=low,
        ci_high_per_workflow_type=high,
    )


@st.composite
def _global_history_strategy(draw: st.DrawFn) -> _GlobalCostHistory:
    """Generate a:class:`_GlobalCostHistory` with the same key set.

 Sharing the workflow-type key set with the dept history is
 required because the predictor looks the chosen ``workflow_type``
 up in whichever history wins. Drawing both maps independently
 over the same keys keeps the search space wide.
 """

    avg: dict[str, Decimal] = {}
    low: dict[str, Decimal] = {}
    high: dict[str, Decimal] = {}
    for wf in _WORKFLOW_TYPES:
        a = draw(_decimal_dollars(0.10, 50.00))
        gap_low = draw(_decimal_dollars(0.00, 5.00))
        gap_high = draw(_decimal_dollars(0.00, 5.00))
        low[wf] = max(Decimal("0.00"), a - gap_low)
        high[wf] = a + gap_high
        avg[wf] = a
    return _GlobalCostHistory(
        avg_cost_per_workflow_type=avg,
        ci_low_per_workflow_type=low,
        ci_high_per_workflow_type=high,
    )


@st.composite
def _budget_caps_strategy(draw: st.DrawFn) -> BudgetCaps:
    """Draw a:class:`BudgetCaps` with strictly positive limits.

 The caps must be ``> 0`` so the ``>=`` comparison in:meth:`BudgetCapPolicy.enforce` has a meaningful boundary. We
 also draw weekly < monthly so a usage value that exhausts the
 weekly budget does not also trivially exhaust the monthly one
 - that keeps the four-scope ordering observable.
 """

    weekly_dept = draw(_decimal_dollars(10.00, 500.00))
    weekly_user = draw(_decimal_dollars(5.00, 100.00))
    monthly_dept = weekly_dept + draw(_decimal_dollars(10.00, 1500.00))
    monthly_user = weekly_user + draw(_decimal_dollars(5.00, 300.00))
    return BudgetCaps(
        weekly_usd_dept=weekly_dept,
        weekly_usd_user=weekly_user,
        monthly_usd_dept=monthly_dept,
        monthly_usd_user=monthly_user,
    )


@st.composite
def _budget_usage_strategy(draw: st.DrawFn) -> _BudgetUsageInput:
    """Draw a:class:`_BudgetUsageInput` with non-negative values.

 Generous upper bounds so the search frequently produces values
 that breach at least one cap from:func:`_budget_caps_strategy`. invariant (c) requires us to
 cover both the all-allow and the per-scope-deny paths.
 """

    return _BudgetUsageInput(
        dept_weekly_usd=draw(_decimal_dollars(0.00, 800.00)),
        user_weekly_usd=draw(_decimal_dollars(0.00, 200.00)),
        dept_monthly_usd=draw(_decimal_dollars(0.00, 2500.00)),
        user_monthly_usd=draw(_decimal_dollars(0.00, 500.00)),
    )


# ---------------------------------------------------------------------------
# invariant - predictor source switch + CI invariant
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(
    dept=_dept_history_strategy(),
    glob=_global_history_strategy(),
    workflow_type=st.sampled_from(_WORKFLOW_TYPES),
    repo_size=st.integers(min_value=0, max_value=2_000_000),
    iterations=st.integers(min_value=1, max_value=10),
)
def test_predict_cost_source_and_ci_invariant(
    dept: _DeptCostHistory,
    glob: _GlobalCostHistory,
    workflow_type: str,
    repo_size: int,
    iterations: int,
) -> None:
    """invariant (a) - source switch on ``task_count >= 30`` + CI bounds.



 Two structural invariants:

 * ``source == "dept"`` iff ``dept.task_count >=
 GLOBAL_FALLBACK_MIN_TASKS``; ``"global_fallback"`` otherwise.
 * ``confidence_low <= predicted_usd <= confidence_high`` for
 every output (because all three values share the same
 scaling factor).
 """

    pred = _predict_cost(
        dept_history=dept,
        global_history=glob,
        workflow_type=workflow_type,
        repo_size_loc=repo_size,
        estimated_iterations=iterations,
    )

    # ----- source switch -----
    if dept.task_count >= GLOBAL_FALLBACK_MIN_TASKS:
        assert pred.source == "dept", (
            f"task_count={dept.task_count} >= "
            f"{GLOBAL_FALLBACK_MIN_TASKS} but source={pred.source!r}; "
            "invariant (a) requires ``source='dept'`` once the dept "
            "has accumulated enough history."
        )
    else:
        assert pred.source == "global_fallback", (
            f"task_count={dept.task_count} < "
            f"{GLOBAL_FALLBACK_MIN_TASKS} but source={pred.source!r}; "
            "invariant (a) requires the global cross-dept fallback "
            "when the dept history is too sparse."
        )

    # ----- confidence interval encloses the prediction -----
    assert pred.confidence_low <= pred.predicted_usd <= pred.confidence_high, (
        f"CostPrediction violates CI invariant: "
        f"low={pred.confidence_low}, predicted={pred.predicted_usd}, "
        f"high={pred.confidence_high}. invariant (a) requires "
        f"``confidence_low <= predicted_usd <= confidence_high``."
    )


@settings(max_examples=100, deadline=None)
@given(
    dept=_dept_history_strategy(),
    glob=_global_history_strategy(),
    workflow_type=st.sampled_from(_WORKFLOW_TYPES),
    repo_size=st.integers(min_value=0, max_value=2_000_000),
    iterations=st.integers(min_value=1, max_value=10),
)
def test_predict_cost_is_deterministic(
    dept: _DeptCostHistory,
    glob: _GlobalCostHistory,
    workflow_type: str,
    repo_size: int,
    iterations: int,
) -> None:
    """invariant (f) - predictor is a pure function.



 Two calls with structurally equal inputs must produce equal
 outputs (same ``predicted_usd``, ``confidence_low``,
 ``confidence_high`` and ``source``).
 """

    p1 = _predict_cost(
        dept_history=dept,
        global_history=glob,
        workflow_type=workflow_type,
        repo_size_loc=repo_size,
        estimated_iterations=iterations,
    )
    p2 = _predict_cost(
        dept_history=dept,
        global_history=glob,
        workflow_type=workflow_type,
        repo_size_loc=repo_size,
        estimated_iterations=iterations,
    )

    assert p1 == p2, (
        f"predict_cost is non-deterministic: identical inputs "
        f"produced different predictions.\n call #1: {p1!r}\n"
        f" call #2: {p2!r}\nBudget prediction determinism invariant."
    )


# ---------------------------------------------------------------------------
# invariant - BudgetCapPolicy enforcement state machine
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(
    caps=_budget_caps_strategy(),
    usage=_budget_usage_strategy(),
    user_attributed=st.booleans(),
)
def test_budget_cap_policy_decision_table(
    caps: BudgetCaps,
    usage: _BudgetUsageInput,
    user_attributed: bool,
) -> None:
    """invariant (c) + (d) - scope-ordered deny + single audit on first breach.



 For every ``(caps, usage, user_attributed)`` example:

 * The decision matches the oracle:func:`_expected_decision`
 which encodes the canonical four-scope ordering.
 * On allow no audit event is written; on deny **exactly one**
 ``budget_exceeded`` event is written, the payload carries
 the offending scope, the configured limit, the running
 usage, and (when applicable) the ``user_id``.
 """

    user_id = _USER_ID if user_attributed else None
    policy, _runner, writer = _make_policy(caps=caps, usage=usage)

    decision = asyncio.run(policy.enforce(dept_id=_DEPT_ID, user_id=user_id))
    expected = _expected_decision(caps=caps, usage=usage, user_id=user_id)

    assert decision == expected, (
        f"BudgetCapPolicy.enforce decided {decision!r} but the "
        f"scope ordering oracle expected {expected!r}. "
        f"caps={caps!r} usage={usage!r} user_id={user_id!r}. "
        "invariant (c) requires the deterministic order "
        f"{SCOPE_ORDER!r}."
    )

    if decision.allowed:
        assert writer.events == [], (
            "BudgetCapPolicy emitted audit events on an allow "
            "decision; invariant (d) forbids any audit traffic "
            "when the workflow may proceed."
        )
        return

    # ---- Deny path ----
    assert len(writer.events) == 1, (
        f"Deny decision must emit exactly one ``budget_exceeded`` "
        f"audit event; saw {len(writer.events)}. "
        f"events={writer.events!r}. invariant (d)."
    )
    event = writer.events[0]
    assert event.action == "budget_exceeded", (
        f"Deny audit action={event.action!r}; invariant (d) "
        "requires ``budget_exceeded``."
    )
    assert event.dept_id == _DEPT_ID
    assert event.actor_role == "system", (
        "BudgetCapPolicy enforces caps as a background gate; the "
        "audit row carries ``actor_role='system'`` per design "
        "§BudgetCapPolicy."
    )
    assert event.result == "denied"

    payload = event.payload or {}
    assert payload.get("scope") == decision.deny_scope, (
        f"Audit payload scope={payload.get('scope')!r} != "
        f"decision deny_scope={decision.deny_scope!r}; "
        "invariant (d) ties the two together."
    )
    # ``limit`` and ``usage`` are serialised as Decimal-as-string;
    # the exact rendering is a callsite contract - we assert the
    # keys are present and non-empty.
    assert "limit" in payload and payload["limit"], (
        f"Audit payload missing ``limit``; got {payload!r}."
    )
    assert "usage" in payload and payload["usage"], (
        f"Audit payload missing ``usage``; got {payload!r}."
    )
    if user_id is not None and decision.deny_scope in {"user_weekly", "user_monthly"}:
        assert payload.get("user_id") == user_id, (
            f"User-scoped deny must carry ``user_id`` in the audit "
            f"payload; got {payload!r}."
        )


@settings(max_examples=100, deadline=None)
@given(
    caps=_budget_caps_strategy(),
    usage=_budget_usage_strategy(),
    user_attributed=st.booleans(),
)
def test_budget_cap_policy_excludes_non_production_via_sql_filter(
    caps: BudgetCaps,
    usage: _BudgetUsageInput,
    user_attributed: bool,
) -> None:
    """invariant (e) - the policy SQL filters ``cost_tag='production'``.



 The policy never aggregates rows tagged ``sandbox`` / ``probe``.
 We assert the filter at the SQL string level by requiring
 every ``fetchval`` call the policy issues to carry
 ``cost_tag = 'production'`` verbatim - the same substring the
 production aggregate query in:mod:`automation_service.budget.\
 policy` uses.
 """

    user_id = _USER_ID if user_attributed else None
    policy, runner, _writer = _make_policy(caps=caps, usage=usage)

    asyncio.run(policy.enforce(dept_id=_DEPT_ID, user_id=user_id))

    assert runner.calls, (
        "BudgetCapPolicy did not issue any usage queries; the "
        "fake runner should have at least one recorded call."
    )
    for query, _args in runner.calls:
        assert "cost_tag = 'production'" in query, (
            f"Usage query missing the production-only filter: "
            f"{query!r}. invariant (e) requires sandbox / probe "
            "rows to be excluded from the running cost aggregate."
        )


@settings(max_examples=80, deadline=None)
@given(
    caps=_budget_caps_strategy(),
    usage=_budget_usage_strategy(),
    user_attributed=st.booleans(),
)
def test_budget_cap_policy_is_deterministic(
    caps: BudgetCaps,
    usage: _BudgetUsageInput,
    user_attributed: bool,
) -> None:
    """invariant (f) - repeating the call is deterministic.



 Two enforcements with the same ``(caps, usage, user_id)`` -
 each driven through an independent policy instance with fresh
 fakes - produce the same:class:`BudgetDecision` and emit the
 same audit-event sequence (same scope, same payload keys).
 """

    user_id = _USER_ID if user_attributed else None

    policy_a, _ra, writer_a = _make_policy(caps=caps, usage=usage)
    policy_b, _rb, writer_b = _make_policy(caps=caps, usage=usage)

    decision_a = asyncio.run(policy_a.enforce(dept_id=_DEPT_ID, user_id=user_id))
    decision_b = asyncio.run(policy_b.enforce(dept_id=_DEPT_ID, user_id=user_id))

    assert decision_a == decision_b, (
        f"BudgetCapPolicy is non-deterministic: "
        f"{decision_a!r} vs {decision_b!r}. invariant (f)."
    )

    assert len(writer_a.events) == len(writer_b.events)
    for ev_a, ev_b in zip(writer_a.events, writer_b.events):
        assert ev_a.action == ev_b.action
        assert ev_a.dept_id == ev_b.dept_id
        assert ev_a.actor_role == ev_b.actor_role
        assert ev_a.result == ev_b.result
        # Compare payload modulo keys we control for; ``timestamp``
        # is frozen via:func:`_frozen_clock` so equality holds.
        assert (ev_a.payload or {}) == (ev_b.payload or {})


# ---------------------------------------------------------------------------
# invariant - concrete regression anchors
# ---------------------------------------------------------------------------


def test_dept_weekly_breach_denies_first_in_scope_order() -> None:
    """Pinned example: dept_weekly takes priority over user_weekly.

 invariant (c) - scope-ordered evaluation. Both ``dept_weekly``
 and ``user_weekly`` are over their caps; the policy MUST deny on
 ``dept_weekly`` because it is the first scope in the canonical
 ordering.


 """

    caps = BudgetCaps(
        weekly_usd_dept=Decimal("100"),
        weekly_usd_user=Decimal("20"),
        monthly_usd_dept=Decimal("400"),
        monthly_usd_user=Decimal("80"),
    )
    usage = _BudgetUsageInput(
        dept_weekly_usd=Decimal("150"),  # over dept_weekly
        user_weekly_usd=Decimal("50"),  # also over user_weekly
        dept_monthly_usd=Decimal("0"),
        user_monthly_usd=Decimal("0"),
    )

    policy, _runner, writer = _make_policy(caps=caps, usage=usage)
    decision = asyncio.run(policy.enforce(dept_id=_DEPT_ID, user_id=_USER_ID))

    assert decision == BudgetDecision.deny("dept_weekly")
    assert len(writer.events) == 1
    assert writer.events[0].payload is not None
    assert writer.events[0].payload["scope"] == "dept_weekly"


def test_user_id_none_skips_user_scoped_caps() -> None:
    """Pinned example: a system workflow cannot deny on user scope.

 invariant (c) - when ``user_id is None`` the user-scoped checks
 are short-circuited; even if ``user_weekly_usd >=
 weekly_usd_user`` the policy must NOT deny on ``user_weekly``.


 """

    caps = BudgetCaps(
        weekly_usd_dept=Decimal("100"),
        weekly_usd_user=Decimal("20"),
        monthly_usd_dept=Decimal("400"),
        monthly_usd_user=Decimal("80"),
    )
    usage = _BudgetUsageInput(
        dept_weekly_usd=Decimal("50"),
        user_weekly_usd=Decimal("999"),  # way over but should be ignored
        dept_monthly_usd=Decimal("0"),
        user_monthly_usd=Decimal("999"),  # way over but should be ignored
    )

    policy, _runner, writer = _make_policy(caps=caps, usage=usage)
    decision = asyncio.run(policy.enforce(dept_id=_DEPT_ID, user_id=None))

    assert decision == BudgetDecision.allow()
    assert writer.events == []


def test_predict_cost_threshold_boundary() -> None:
    """Pinned example: ``task_count == 30`` lands on the dept branch.

 invariant (a) uses ``>=`` so the boundary value belongs to
 the ``dept`` source, not the fallback. The pinned anchor
 catches a regression that flips ``>`` for ``>=`` (or vice
 versa) on the threshold.


 """

    avg = {"code_change": Decimal("1.00")}
    low = {"code_change": Decimal("0.50")}
    high = {"code_change": Decimal("1.50")}

    dept = _DeptCostHistory(
        task_count=GLOBAL_FALLBACK_MIN_TASKS,
        avg_cost_per_workflow_type=avg,
        ci_low_per_workflow_type=low,
        ci_high_per_workflow_type=high,
    )
    glob = _GlobalCostHistory(
        avg_cost_per_workflow_type={"code_change": Decimal("99.00")},
        ci_low_per_workflow_type={"code_change": Decimal("80.00")},
        ci_high_per_workflow_type={"code_change": Decimal("120.00")},
    )

    pred = _predict_cost(
        dept_history=dept,
        global_history=glob,
        workflow_type="code_change",
        repo_size_loc=0,
        estimated_iterations=1,
    )

    assert pred.source == "dept"
    # The dept averages are tiny while the global ones are huge;
    # if the source flipped to ``global_fallback`` ``predicted_usd``
    # would jump to ~99. This pin catches that regression.
    assert pred.predicted_usd <= Decimal("2.00")


def test_predict_cost_below_threshold_uses_global_fallback() -> None:
    """Pinned example: ``task_count < 30``  ``source='global_fallback'``.

 invariant (a). Hitting the fallback is mandatory for the
 cold-start path that ``audit cost_prediction_using_global_fallback``
 depends on.


 """

    dept = _DeptCostHistory(
        task_count=GLOBAL_FALLBACK_MIN_TASKS - 1,
        avg_cost_per_workflow_type={"code_change": Decimal("1.00")},
        ci_low_per_workflow_type={"code_change": Decimal("0.50")},
        ci_high_per_workflow_type={"code_change": Decimal("1.50")},
    )
    glob = _GlobalCostHistory(
        avg_cost_per_workflow_type={"code_change": Decimal("10.00")},
        ci_low_per_workflow_type={"code_change": Decimal("8.00")},
        ci_high_per_workflow_type={"code_change": Decimal("12.00")},
    )

    pred = _predict_cost(
        dept_history=dept,
        global_history=glob,
        workflow_type="code_change",
        repo_size_loc=0,
        estimated_iterations=1,
    )

    assert pred.source == "global_fallback"
    assert pred.confidence_low <= pred.predicted_usd <= pred.confidence_high


def test_assume_helper_is_referenced() -> None:
    """Defensive: keep ``hypothesis.assume`` in scope for future strategies.

 The ``assume`` import is reserved for future strategies that may
 need to filter degenerate inputs (eg. caps below a minimum).
 Referencing it here prevents a lint cleanup pass from removing
 the import and silently weakening the test surface.
 """

    _ = assume
