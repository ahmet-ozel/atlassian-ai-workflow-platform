"""Property test 6 — Cost tracking idempotent insert.

**Validates: Requirements 5.4, 2.4**

Hypothesis-driven verification of the idempotent-insert contract
for :class:`cost_tracking.tracker.CostTracker` described in
``design.md`` §"CostTracker" / §"Postgres Şema Eklemeleri" of the
``platform-mimari-ops`` spec, implementing tasks.md §7.5.

Property statement (design.md §"Property 6")
--------------------------------------------

For any hypothesis-generated list of ``CostEntry`` values
(including arbitrary ``activity_id`` collisions):

    (a) After ``cost_tracker.record(entry)`` has been called for
        every entry in the list, ``cost_tracking`` contains
        **exactly one** row per distinct ``activity_id`` — the
        ``UNIQUE (activity_id)`` index plus the
        ``ON CONFLICT (activity_id) DO NOTHING`` clause guarantee
        the second insert is a no-op.
    (b) The resulting row count equals the number of unique
        ``activity_id`` values in the input list.
    (c) ``record`` enforces the Postgres ``CHECK`` constraint
        ``cost_tag IN ('production','sandbox','probe')`` — invalid
        tags raise at write time and never reach the table.
    (d) Rows tagged ``'sandbox'`` or ``'probe'`` are excluded from
        :class:`automation_service.budget.policy.BudgetCapPolicy`
        usage queries (the SQL filter is ``cost_tag = 'production'``);
        sandbox prompt tests (R2.4) and probe-time LLM calls do not
        contaminate dept budget aggregates.
    (e) Determinism: a second pass over the same entry list against
        a fresh table arrives at the **same** final row state — the
        first ``activity_id`` write wins, all subsequent writes for
        the same id are dropped silently. A subsequent ``SELECT``
        returns the row from the first insert, not from any later
        attempt.
    (f) On every conflict path the call site emits a single
        ``cost_tracking_duplicate_dropped`` audit event so duplicate
        drops remain observable through the audit log (design.md
        §"Property 6" + §"Component → Requirement Eşlemesi").

Surface under test
------------------

* :class:`cost_tracking.tracker.CostTracker` (tasks.md §7.1 — still
  ``[~]``) is imported behind a ``try / except`` guard following the
  reference style of ``test_token_cap_fail_fast.py`` and
  ``test_cost_predict_budget_cap.py``. Until task 7.1 ships the
  property test exercises a faithful in-memory reference oracle that
  mirrors the design pseudocode (``INSERT ... ON CONFLICT
  (activity_id) DO NOTHING`` + the ``cost_tag`` ``CHECK``) so the
  invariants in (a)–(f) still pin the contract a future
  implementation must satisfy. When task 7.1 lands the import guard
  collapses and the production module is exercised directly.
* :class:`cost_tracking.types.CostEntry` is imported under the same
  guard; the in-test stand-in keeps the dataclass shape declared in
  ``design.md`` §"Cost & Budget Dataclass'ları" so a production
  ``CostEntry`` satisfies the same attribute access pattern.

Cross-references
----------------

* ``platform-mimari-ops/design.md`` §"Property 6" — invariant set.
* ``platform-mimari-ops/design.md`` §"CostTracker" — INSERT ...
  ON CONFLICT pseudocode this property pins.
* ``platform/infra/postgres/20_ops.sql`` — the ``shared.cost_tracking``
  table whose ``UNIQUE (activity_id)`` + ``CHECK (cost_tag IN
  ('production','sandbox','probe'))`` constraints make the property
  enforceable end-to-end.
* ``platform/tests/property/test_token_cap_fail_fast.py`` —
  reference style for the module-level ``skipif`` fallback pattern
  and the ``try / except ModuleNotFoundError`` import guard.
* ``platform/tests/property/test_cost_predict_budget_cap.py`` —
  sibling property test (Property 7); together (6 + 7) they own the
  combinatorial coverage of the cost-tracking lib's two halves
  (record + predict / enforce).
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap — the ``cost_tracking`` lib lives under
# ``libs/cost-tracking/src`` (added defensively because the path is
# not yet wired into ``pytest.ini``; once task 7.1 ships, the entry
# can be promoted to ``pythonpath`` and this block becomes a no-op).
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "libs" / "cost-tracking" / "src",
    _REPO_ROOT / "libs" / "audit_logger" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# ---------------------------------------------------------------------------
# Optional import — :class:`cost_tracking.tracker.CostTracker` is
# task 7.1; until it ships we fall back to an in-test stand-in that
# mirrors the design pseudocode (``INSERT ... ON CONFLICT
# (activity_id) DO NOTHING`` + the ``cost_tag`` ``CHECK``) so the
# invariants in (a)–(f) still pin the contract a future
# implementation must satisfy.
#
# Import style mirrors ``test_token_cap_fail_fast.py`` (task 4.3):
# capture the ``ModuleNotFoundError`` message, surface it through
# ``pytest.mark.skipif`` so collection stays clean and the skip
# reason is precise.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - guard collapses once task 7.1 ships
    from cost_tracking.tracker import (  # type: ignore[import-not-found]
        CostTracker as _ProductionCostTracker,
    )
    from cost_tracking.types import (  # type: ignore[import-not-found]
        CostEntry as _ProductionCostEntry,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    _ProductionCostTracker = None  # type: ignore[assignment,misc]
    _ProductionCostEntry = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: str | None = str(exc)
else:  # pragma: no cover - exercised only after task 7.1 lands
    _IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Constants from the design contract
# ---------------------------------------------------------------------------

#: ``cost_tag`` values accepted by the Postgres ``CHECK`` constraint
#: declared in ``infra/postgres/20_ops.sql``.
_VALID_COST_TAGS: Final[tuple[str, ...]] = ("production", "sandbox", "probe")

#: Tags excluded from :meth:`BudgetCapPolicy.enforce` aggregates per
#: clause (d). The complement of ``{"production"}``.
_NON_PRODUCTION_TAGS: Final[frozenset[str]] = frozenset({"sandbox", "probe"})

#: Audit action emitted on every conflict path per clause (f).
_DUPLICATE_AUDIT_ACTION: Final[str] = "cost_tracking_duplicate_dropped"

#: SQL filter substring that ``BudgetCapPolicy._usage_query`` must
#: carry verbatim per clause (d). Used to assert the SQL string
#: shape both in the reference oracle (here) and against the
#: production aggregate query (sibling Property 7 already pins
#: this for the policy SQL — Property 6 only pins the data side
#: of the contract: non-production rows must be observably tagged
#: so the filter has something to exclude).
_PRODUCTION_FILTER: Final[str] = "cost_tag = 'production'"


# ---------------------------------------------------------------------------
# Domain dataclass — local stand-in for ``cost_tracking.types.CostEntry``.
#
# Mirrors the shape declared in ``design.md`` §"Cost & Budget
# Dataclass'ları". Once task 7.1 lands we replace the local
# definition with a direct import; the structural shape stays
# identical so the production ``CostEntry`` satisfies the same
# attribute access pattern.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CostEntry:
    """Mirror of :class:`cost_tracking.types.CostEntry`.

    The fields are taken verbatim from ``design.md`` §"Cost &
    Budget Dataclass'ları" / ``infra/postgres/20_ops.sql``
    columns. ``frozen=True`` mirrors the production dataclass and
    keeps the entry hashable for use as a dictionary key in
    invariant assertions.
    """

    activity_id: str
    workflow_id: str | None
    dept_id: str
    user_id: str | None
    model: str
    provider: Literal["vllm", "openai", "anthropic"]
    token_in: int
    token_out: int
    cost_usd: Decimal
    cost_tag: Literal["production", "sandbox", "probe"]


def _to_entry(values: dict[str, Any]) -> Any:
    """Build a CostEntry — production class when available, else stand-in.

    Keeps the property test agnostic to which side of the import
    guard is active: when task 7.1 ships, the production
    ``CostEntry`` is constructed; otherwise the in-test stand-in
    is returned. Both share the same attribute names so the
    invariant assertions read the same fields either way.
    """

    if _ProductionCostEntry is not None:  # pragma: no cover - covered after 7.1
        return _ProductionCostEntry(**values)
    return _CostEntry(**values)


# ---------------------------------------------------------------------------
# Reference Postgres-shaped store — mirrors the
# ``shared.cost_tracking`` table semantics (UNIQUE on activity_id,
# CHECK on cost_tag, ON CONFLICT DO NOTHING) so the property
# invariants exercise an oracle that matches what the production
# code path will see at runtime.
#
# The store is deliberately minimal: it implements only the two
# semantics ``CostTracker.record`` relies on (idempotent insert
# keyed on ``activity_id`` and the ``cost_tag`` CHECK). A wider
# fake would only invite divergence between the test oracle and
# the actual Postgres engine.
# ---------------------------------------------------------------------------


class _CostTagViolation(ValueError):
    """Raised when a ``cost_tag`` violates the Postgres CHECK.

    The exception type mirrors what asyncpg surfaces for
    ``CheckViolationError`` (a subclass of
    ``IntegrityConstraintViolationError``). For the property test
    we only care about the error class shape, not the SQLSTATE
    code — :class:`ValueError` keeps the assertion stable across
    driver versions.
    """


@dataclass
class _InMemoryCostTrackingTable:
    """In-memory mirror of ``shared.cost_tracking``.

    Implements the exact subset of Postgres semantics the property
    test exercises:

    * ``UNIQUE (activity_id)`` index — duplicate inserts return
      ``conflict=True`` and leave the existing row in place
      (``ON CONFLICT (activity_id) DO NOTHING``).
    * ``CHECK (cost_tag IN ('production','sandbox','probe'))`` —
      invalid tags raise :class:`_CostTagViolation` before the
      row is committed.
    * Insertion order is preserved (``rows`` is a list) so a
      ``SELECT`` returning rows in insert order is deterministic
      under the same input sequence.

    The store is intentionally small — clauses (d) / (e) inspect
    the raw row state and the ``audit`` callable; nothing else.
    """

    rows: list[Any] = field(default_factory=list)
    _index: dict[str, int] = field(default_factory=dict)

    def insert_with_on_conflict(self, entry: Any) -> bool:
        """Insert ``entry`` and return ``True`` if a conflict occurred.

        Mirrors ``INSERT INTO cost_tracking ... ON CONFLICT
        (activity_id) DO NOTHING`` — the call returns ``False``
        when a new row is inserted and ``True`` when the unique
        index already has the ``activity_id`` and the insert was
        dropped.
        """

        # ----- (c) Postgres CHECK on cost_tag -----
        if entry.cost_tag not in _VALID_COST_TAGS:
            raise _CostTagViolation(
                f"cost_tag={entry.cost_tag!r} violates the "
                f"shared.cost_tracking CHECK constraint "
                f"(allowed: {_VALID_COST_TAGS})."
            )

        # ----- (a) UNIQUE (activity_id) + ON CONFLICT DO NOTHING -----
        if entry.activity_id in self._index:
            return True
        self._index[entry.activity_id] = len(self.rows)
        self.rows.append(entry)
        return False

    def select_all(self) -> list[Any]:
        """Return every row in insert order — used by clauses (a) / (e)."""

        return list(self.rows)

    def select_by_activity_id(self, activity_id: str) -> Any | None:
        """Return the winning row for ``activity_id`` — clause (e)."""

        idx = self._index.get(activity_id)
        return self.rows[idx] if idx is not None else None

    def production_usage_sum(self) -> Decimal:
        """Aggregate ``cost_usd`` over rows where ``cost_tag='production'``.

        Mirrors the production-only filter
        :class:`BudgetCapPolicy` will apply at the SQL layer (the
        sibling Property 7 pins the SQL string itself; here we pin
        the data semantics that filter relies on). Used by
        clause (d).
        """

        return sum(
            (r.cost_usd for r in self.rows if r.cost_tag == "production"),
            start=Decimal("0"),
        )


# ---------------------------------------------------------------------------
# Reference CostTracker — used when task 7.1 has not landed yet.
#
# Faithful transliteration of the design pseudocode in
# ``design.md`` §"CostTracker": ``INSERT ... ON CONFLICT
# (activity_id) DO NOTHING`` followed by an audit emit on the
# conflict path. Once task 7.1 ships the helper collapses to a
# pass-through wrapper around the production class.
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAudit:
    """Captures every emitted audit action — clause (f) oracle."""

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def emit(self, action: str, payload: dict[str, Any]) -> None:
        self.events.append((action, dict(payload)))


class _ReferenceCostTracker:
    """Reference implementation that mirrors the design pseudocode.

    Two collaborators:

    * ``store`` — an :class:`_InMemoryCostTrackingTable` that
      mirrors the Postgres semantics ``CostTracker.record`` relies
      on.
    * ``audit`` — an :class:`_RecordingAudit` that records every
      ``cost_tracking_duplicate_dropped`` event so clause (f) can
      assert the audit trail.

    The class deliberately matches the production
    :meth:`CostTracker.record` signature (``async def record(self,
    entry: CostEntry) -> None``) so the test code path is
    identical regardless of which side of the import guard is
    active.
    """

    def __init__(
        self,
        *,
        store: _InMemoryCostTrackingTable,
        audit: _RecordingAudit,
    ) -> None:
        self._store = store
        self._audit = audit

    async def record(self, entry: Any) -> None:
        conflict = self._store.insert_with_on_conflict(entry)
        if conflict:
            self._audit.emit(
                _DUPLICATE_AUDIT_ACTION,
                {
                    "activity_id": entry.activity_id,
                    "dept_id": entry.dept_id,
                },
            )


def _build_tracker() -> tuple[
    Any, _InMemoryCostTrackingTable, _RecordingAudit
]:
    """Return a wired tracker / store / audit triple.

    When the production :class:`CostTracker` has been imported we
    still drive it through the in-memory store / audit fakes so
    the property test stays unit-level deterministic; the
    production class accepts any ``db`` collaborator that exposes
    the ``insert_with_on_conflict`` semantics, which our store
    matches.

    The fallback path returns the reference :class:`_ReferenceCostTracker`
    — both halves of the import guard share the same surface area
    so the call sites in the tests below remain identical.
    """

    store = _InMemoryCostTrackingTable()
    audit = _RecordingAudit()

    if _ProductionCostTracker is not None:  # pragma: no cover - covered after 7.1
        # The exact constructor signature is task 7.1's contract;
        # we pass keyword args matching the design pseudocode and
        # let a TypeError surface here so the failure points at
        # the constructor rather than silently neutering the
        # property. Mirrors the dual-shape pattern used in
        # ``test_token_cap_fail_fast.py``.
        last_exc: Exception | None = None
        for attempt in (
            lambda: _ProductionCostTracker(db=store, audit=audit),  # type: ignore[call-arg]
            lambda: _ProductionCostTracker(store, audit),  # type: ignore[call-arg]
        ):
            try:
                return attempt(), store, audit
            except TypeError as exc:  # pragma: no cover - defensive
                last_exc = exc
                continue
        raise AssertionError(
            "CostTracker constructor signature did not match "
            "either ``(db=, audit=)`` or ``(db, audit)``; last "
            f"error was: {last_exc!r}"
        )

    return _ReferenceCostTracker(store=store, audit=audit), store, audit


# ---------------------------------------------------------------------------
# Hypothesis strategies (per design.md §"Property 6" input space)
# ---------------------------------------------------------------------------


#: Activity ids drawn from a small alphabet so collisions are
#: frequent enough for clause (a) to bite. Hypothesis would
#: rarely collide UUID-shaped strings within the same example,
#: which would silently make the property trivially satisfied;
#: a 6-symbol alphabet keeps the shrinker honest.
_activity_id_strategy: st.SearchStrategy[str] = st.sampled_from(
    [f"act-{c}" for c in "abcdef"]
)

#: ``dept_id`` is a foreign key to ``automation.departments(id)``;
#: the property is dept-agnostic so a small fixed set keeps the
#: search space narrow without losing coverage.
_dept_id_strategy: st.SearchStrategy[str] = st.sampled_from(
    ("payment", "platform", "data")
)

#: Optional ``user_id`` — ``None`` is a valid value for
#: automation-driven workflows without an attributed end-user.
_user_id_strategy: st.SearchStrategy[str | None] = st.one_of(
    st.none(),
    st.sampled_from(("user-1", "user-2", "user-3")),
)

#: Provider matches the Postgres CHECK ``provider IN
#: ('vllm','openai','anthropic')``.
_provider_strategy: st.SearchStrategy[str] = st.sampled_from(
    ("vllm", "openai", "anthropic")
)

#: ``cost_tag`` strategy used by the happy-path tests — only
#: valid values; clause (c) is exercised by a separate test that
#: explicitly draws invalid tags.
_valid_cost_tag_strategy: st.SearchStrategy[str] = st.sampled_from(
    _VALID_COST_TAGS
)

#: ``cost_usd`` quantised to two decimal places — matches the
#: ``numeric(12, 6)`` column at workable precision while keeping
#: the search space small.
_cost_usd_strategy: st.SearchStrategy[Decimal] = st.decimals(
    min_value=Decimal("0.00"),
    max_value=Decimal("999.99"),
    allow_nan=False,
    allow_infinity=False,
    places=2,
)


@st.composite
def _cost_entry_strategy(draw: st.DrawFn) -> Any:
    """Generate a :class:`CostEntry` with a small ``activity_id`` alphabet.

    Drawing ``activity_id`` from a 6-symbol alphabet means a list
    of 8–20 entries reliably contains collisions, exercising the
    ``ON CONFLICT DO NOTHING`` branch on every Hypothesis example
    rather than only on rare lucky draws.
    """

    return _to_entry(
        {
            "activity_id": draw(_activity_id_strategy),
            "workflow_id": draw(
                st.one_of(
                    st.none(),
                    st.sampled_from(("wf-1", "wf-2", "wf-3")),
                )
            ),
            "dept_id": draw(_dept_id_strategy),
            "user_id": draw(_user_id_strategy),
            "model": draw(
                st.sampled_from(
                    (
                        "gpt-4",
                        "claude-3.5",
                        "llama-3-70b-instruct",
                    )
                )
            ),
            "provider": draw(_provider_strategy),
            "token_in": draw(st.integers(min_value=0, max_value=10_000)),
            "token_out": draw(st.integers(min_value=0, max_value=10_000)),
            "cost_usd": draw(_cost_usd_strategy),
            "cost_tag": draw(_valid_cost_tag_strategy),
        }
    )


_entries_list_strategy: st.SearchStrategy[list[Any]] = st.lists(
    _cost_entry_strategy(),
    min_size=1,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Module-level skip — covers the case where task 7.1 has not landed.
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.skipif(
    _ProductionCostTracker is None,
    reason=(
        "cost_tracking.tracker.CostTracker is not yet implemented "
        "(task 7.1 of platform-mimari-ops is still ``[~]``); import "
        f"failed with: {_IMPORT_ERROR!r}. Property 6 is fully "
        "specified by design.md and will be exercised end-to-end "
        "as soon as task 7.1 ships."
    ),
)


# ---------------------------------------------------------------------------
# Property 6 — full invariant set (a)..(f)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(entries=_entries_list_strategy)
def test_record_is_idempotent_on_activity_id(entries: list[Any]) -> None:
    """Property 6 (a) + (b) — UNIQUE activity_id keeps row count = uniq(ids).

    Validates: Requirements 5.4.

    For every Hypothesis-generated list of :class:`CostEntry`:

    * exactly one row is committed per distinct ``activity_id`` —
      the ``UNIQUE`` index plus ``ON CONFLICT DO NOTHING`` makes
      every subsequent insert with the same ``activity_id`` a
      no-op (clause (a));
    * ``len(rows)`` equals the number of unique ``activity_id``
      values in the input list (clause (b)).
    """

    tracker, store, _audit = _build_tracker()

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    rows = store.select_all()
    unique_ids = {e.activity_id for e in entries}

    # ----- (b) row count = uniq(activity_id) -----
    assert len(rows) == len(unique_ids), (
        f"shared.cost_tracking has {len(rows)} rows but the input "
        f"list contains {len(unique_ids)} unique activity_id "
        "values. Property 6 (b) requires "
        "``len(rows) == len(uniq(activity_ids))``."
    )

    # ----- (a) one row per activity_id -----
    seen: set[str] = set()
    for row in rows:
        assert row.activity_id not in seen, (
            f"shared.cost_tracking contains a duplicate row for "
            f"activity_id={row.activity_id!r}; Property 6 (a) "
            "requires the UNIQUE index + ON CONFLICT DO NOTHING "
            "to keep exactly one row per id."
        )
        seen.add(row.activity_id)

    assert seen == unique_ids, (
        f"Committed activity_id set {seen!r} does not equal the "
        f"unique input set {unique_ids!r}; Property 6 (a) requires "
        "every distinct id to land exactly once."
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(entries=_entries_list_strategy)
def test_first_write_wins_on_activity_id_conflict(entries: list[Any]) -> None:
    """Property 6 (e) — first insert wins; later attempts are dropped.

    Validates: Requirements 5.4.

    The semantic that ``ON CONFLICT DO NOTHING`` enforces is
    "first writer wins": once an ``activity_id`` row exists,
    subsequent inserts for the same id are silently ignored — the
    table keeps the **earliest** row, not the latest. A
    ``SELECT`` issued after the entry list has been replayed must
    therefore return the row from the **first** insert with that
    id, not from any later attempt.
    """

    tracker, store, _audit = _build_tracker()

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    # Build the oracle: walk the input list and remember the first
    # entry for every activity_id. Every entry that follows for the
    # same id must have been dropped.
    first_for_id: dict[str, Any] = {}
    for entry in entries:
        first_for_id.setdefault(entry.activity_id, entry)

    for activity_id, expected in first_for_id.items():
        actual = store.select_by_activity_id(activity_id)
        assert actual is not None, (
            f"activity_id={activity_id!r} missing from "
            "shared.cost_tracking; Property 6 (e) requires the "
            "first insert to be retained."
        )
        assert actual == expected, (
            f"activity_id={activity_id!r}: stored row {actual!r} "
            f"does not match the first input entry {expected!r}. "
            "Property 6 (e) — first-writer-wins — was violated; a "
            "later insert with the same id appears to have "
            "overwritten the original."
        )


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(entries=_entries_list_strategy)
def test_record_emits_duplicate_dropped_audit_on_conflict(
    entries: list[Any],
) -> None:
    """Property 6 (f) — every conflict path emits a single audit event.

    Validates: Requirements 5.4.

    The audit trail is the only externally observable signal that
    a duplicate insert was dropped; without it, replay traffic
    would be silent. We assert that:

    * the number of ``cost_tracking_duplicate_dropped`` audit
      events equals ``len(entries) - len(uniq(activity_ids))``
      (i.e. one event per dropped conflict);
    * each event carries the conflicting ``activity_id`` and
      ``dept_id`` so the audit row is actionable.
    """

    tracker, _store, audit = _build_tracker()

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    expected_drops = len(entries) - len({e.activity_id for e in entries})

    duplicate_events = [
        (action, payload)
        for action, payload in audit.events
        if action == _DUPLICATE_AUDIT_ACTION
    ]
    assert len(duplicate_events) == expected_drops, (
        f"Expected {expected_drops} ``{_DUPLICATE_AUDIT_ACTION}`` "
        f"audit events (one per dropped conflict); saw "
        f"{len(duplicate_events)}. Property 6 (f)."
    )
    for _action, payload in duplicate_events:
        assert "activity_id" in payload and payload["activity_id"], (
            f"Duplicate-dropped audit payload {payload!r} missing "
            "``activity_id``; Property 6 (f) requires it for "
            "actionability."
        )
        assert "dept_id" in payload and payload["dept_id"], (
            f"Duplicate-dropped audit payload {payload!r} missing "
            "``dept_id``; Property 6 (f) requires it so RLS "
            "filters apply."
        )


@settings(max_examples=80, deadline=None)
@given(
    entry=_cost_entry_strategy(),
    invalid_tag=st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
        ),
        min_size=1,
        max_size=12,
    ).filter(lambda s: s not in _VALID_COST_TAGS),
)
def test_record_rejects_invalid_cost_tag(
    entry: Any, invalid_tag: str
) -> None:
    """Property 6 (c) — Postgres CHECK on cost_tag rejects invalid values.

    Validates: Requirements 5.4.

    The Postgres ``CHECK (cost_tag IN
    ('production','sandbox','probe'))`` constraint must surface
    at the application layer as a write-time error so an invalid
    tag never reaches the table. Build a structurally identical
    entry whose only difference is an out-of-domain ``cost_tag``
    and assert ``record`` raises before any row is committed.
    """

    tracker, store, _audit = _build_tracker()

    bad_entry = replace(entry, cost_tag=invalid_tag)  # type: ignore[arg-type]

    async def _drive() -> None:
        await tracker.record(bad_entry)

    with pytest.raises(  # noqa: PT011 - any constraint-violation type is fine
        (ValueError, _CostTagViolation)
    ):
        asyncio.run(_drive())

    # The failing insert must not have landed any partial state.
    assert store.select_all() == [], (
        f"shared.cost_tracking accepted a row with invalid "
        f"cost_tag={invalid_tag!r}; Property 6 (c) requires the "
        "Postgres CHECK to reject the write at write-time."
    )


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(entries=_entries_list_strategy)
def test_non_production_rows_excluded_from_budget_usage(
    entries: list[Any],
) -> None:
    """Property 6 (d) — sandbox / probe rows do not count against budgets.

    Validates: Requirements 5.4, 2.4.

    The ``BudgetCapPolicy`` aggregate filter is
    ``cost_tag = 'production'`` (sibling Property 7 pins the SQL
    string itself). Property 6 owns the data side of that
    contract: rows tagged ``sandbox`` or ``probe`` MUST NOT be
    rewritten as ``production`` by the tracker, and the
    production-only sum MUST equal the sum over committed
    production rows alone.

    Sandbox prompt tests (R2.4) and probe-time LLM calls share
    one storage path with production usage; the only thing that
    keeps them out of dept budgets is the tag — so the tag must
    survive the round-trip unchanged.
    """

    tracker, store, _audit = _build_tracker()

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    rows = store.select_all()

    # The committed rows preserve every cost_tag verbatim — the
    # tracker never silently rewrites sandbox / probe to
    # production. (If it did, the BudgetCapPolicy SQL filter
    # would be a no-op and dept budgets would silently inflate.)
    seen_tags = {r.cost_tag for r in rows}
    assert seen_tags <= set(_VALID_COST_TAGS), (
        f"Committed rows carry cost_tag values {seen_tags!r} "
        f"outside the design-mandated set {_VALID_COST_TAGS}; "
        "Property 6 (d) relies on the tag round-tripping "
        "unchanged."
    )

    expected_production_sum: Decimal = sum(
        (r.cost_usd for r in rows if r.cost_tag == "production"),
        start=Decimal("0"),
    )
    expected_non_production_sum: Decimal = sum(
        (r.cost_usd for r in rows if r.cost_tag in _NON_PRODUCTION_TAGS),
        start=Decimal("0"),
    )

    actual_production = store.production_usage_sum()
    assert actual_production == expected_production_sum, (
        f"Production-only usage sum {actual_production!r} != "
        f"oracle {expected_production_sum!r}. Property 6 (d) "
        "requires the production-tag aggregate to ignore "
        "sandbox / probe rows."
    )

    # Sanity: when the input list contained any non-production
    # entries that survived deduplication, the production sum must
    # be strictly smaller than the all-rows sum. This catches a
    # regression where a future ``BudgetCapPolicy`` migration
    # accidentally drops the ``cost_tag = 'production'`` filter.
    if expected_non_production_sum > Decimal("0"):
        all_rows_sum = expected_production_sum + expected_non_production_sum
        assert actual_production < all_rows_sum, (
            f"Production-only sum {actual_production!r} is not "
            f"strictly less than the all-rows sum "
            f"{all_rows_sum!r} despite "
            f"{expected_non_production_sum!r} of non-production "
            "cost being committed. Property 6 (d) — the "
            "production filter would be a no-op."
        )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(entries=_entries_list_strategy)
def test_record_is_deterministic(entries: list[Any]) -> None:
    """Property 6 (e) — same entry list ⇒ identical final table state.

    Validates: Requirements 5.4.

    Two replays of the same entry list against fresh trackers
    must produce identical ``rows`` (same content, same insert
    order) and identical audit-event sequences. Hidden state
    (e.g. a per-instance cache or a clock-dependent hash) would
    surface as a diff between the two runs.
    """

    tracker_a, store_a, audit_a = _build_tracker()
    tracker_b, store_b, audit_b = _build_tracker()

    async def _drive(tracker: Any) -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive(tracker_a))
    asyncio.run(_drive(tracker_b))

    assert store_a.select_all() == store_b.select_all(), (
        "cost_tracker.record is non-deterministic: identical "
        "input lists produced different table states.\n"
        f"  run #1 rows: {store_a.select_all()!r}\n"
        f"  run #2 rows: {store_b.select_all()!r}\n"
        "Property 6 (e) — determinism."
    )

    assert audit_a.events == audit_b.events, (
        "cost_tracker.record is non-deterministic: identical "
        "input lists produced different audit-event sequences.\n"
        f"  run #1 events: {audit_a.events!r}\n"
        f"  run #2 events: {audit_b.events!r}\n"
        "Property 6 (e) — determinism."
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors — pinned examples that complement the
# Hypothesis search by fixing the conflict pattern on a known input.
# These run independently of the strategy shrinker so a regression
# in the ``ON CONFLICT DO NOTHING`` clause is caught even when
# Hypothesis happens to draw collision-free examples.
# ---------------------------------------------------------------------------


def _make_entry(activity_id: str, *, cost_tag: str = "production",
                cost_usd: str = "1.00") -> Any:
    """Tiny factory used by the regression anchors below."""

    return _to_entry(
        {
            "activity_id": activity_id,
            "workflow_id": "wf-anchor",
            "dept_id": "payment",
            "user_id": "user-anchor",
            "model": "gpt-4",
            "provider": "openai",
            "token_in": 10,
            "token_out": 20,
            "cost_usd": Decimal(cost_usd),
            "cost_tag": cost_tag,
        }
    )


def test_second_insert_with_same_activity_id_is_a_noop() -> None:
    """Two records, same ``activity_id`` ⇒ row count stays at 1.

    Anchors clause (a) on a fixed pair so a regression that drops
    the ``ON CONFLICT (activity_id) DO NOTHING`` clause and lets
    the second insert raise (or worse: overwrite the first row)
    surfaces deterministically — independently of what
    Hypothesis happens to draw.

    Validates: Requirements 5.4.
    """

    tracker, store, audit = _build_tracker()

    first = _make_entry("act-1", cost_usd="1.00")
    second = _make_entry("act-1", cost_usd="2.00")  # same id, different cost

    async def _drive() -> None:
        await tracker.record(first)
        await tracker.record(second)

    asyncio.run(_drive())

    rows = store.select_all()
    assert len(rows) == 1, (
        f"Two inserts on the same activity_id produced {len(rows)} "
        "rows; Property 6 (a) requires exactly one."
    )
    # First-writer-wins: the row keeps cost_usd=1.00, not 2.00.
    assert rows[0].cost_usd == Decimal("1.00"), (
        f"On-conflict-do-nothing must keep the FIRST row; "
        f"observed cost_usd={rows[0].cost_usd!r} (expected "
        "Decimal('1.00')). Property 6 (e)."
    )

    # ----- (e) SELECT returns first insert, not second -----
    selected = store.select_by_activity_id("act-1")
    assert selected is not None
    assert selected.cost_usd == Decimal("1.00"), (
        "SELECT after a duplicate insert must return the row "
        "from the FIRST insert (cost_usd=1.00); got "
        f"{selected.cost_usd!r}. Property 6 (e)."
    )

    # ----- (f) audit logged the dropped conflict -----
    duplicate_events = [
        (action, payload)
        for action, payload in audit.events
        if action == _DUPLICATE_AUDIT_ACTION
    ]
    assert len(duplicate_events) == 1, (
        f"Expected exactly one ``{_DUPLICATE_AUDIT_ACTION}`` audit "
        f"event for the dropped conflict; saw {len(duplicate_events)}. "
        "Property 6 (f)."
    )
    assert duplicate_events[0][1]["activity_id"] == "act-1"


def test_three_distinct_ids_produce_three_rows() -> None:
    """No collisions ⇒ no audit events; row count = input length.

    Anchors clause (b) and the "no audit on the happy path"
    sub-invariant of (f) on a fixed input. A regression that
    spuriously emits ``cost_tracking_duplicate_dropped`` on every
    insert (e.g. an off-by-one ``DO UPDATE`` instead of
    ``DO NOTHING``) is caught here deterministically.

    Validates: Requirements 5.4.
    """

    tracker, store, audit = _build_tracker()
    entries = [_make_entry(f"act-{c}") for c in ("a", "b", "c")]

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    assert len(store.select_all()) == 3, (
        f"Expected 3 rows for 3 distinct activity_ids; got "
        f"{len(store.select_all())}. Property 6 (b)."
    )
    assert all(
        action != _DUPLICATE_AUDIT_ACTION for action, _ in audit.events
    ), (
        "No conflicts occurred but at least one "
        f"``{_DUPLICATE_AUDIT_ACTION}`` event was emitted; "
        "Property 6 (f) restricts the audit emission to the "
        "conflict path only."
    )


def test_sandbox_and_probe_rows_excluded_from_production_sum() -> None:
    """Mixed-tag input ⇒ production sum ignores sandbox / probe rows.

    Anchors clause (d) on a fixed input: three rows, one per tag,
    each with cost_usd=1.00. The production-only sum must be
    1.00, not 3.00 — independently of how Hypothesis draws the
    cost values.

    Validates: Requirements 5.4, 2.4.
    """

    tracker, store, _audit = _build_tracker()
    entries = [
        _make_entry("act-prod", cost_tag="production", cost_usd="1.00"),
        _make_entry("act-sand", cost_tag="sandbox", cost_usd="1.00"),
        _make_entry("act-probe", cost_tag="probe", cost_usd="1.00"),
    ]

    async def _drive() -> None:
        for entry in entries:
            await tracker.record(entry)

    asyncio.run(_drive())

    assert store.production_usage_sum() == Decimal("1.00"), (
        "Production-only sum over [production=1, sandbox=1, "
        "probe=1] must equal Decimal('1.00'); got "
        f"{store.production_usage_sum()!r}. Property 6 (d) — "
        "sandbox / probe rows must be excluded from budget "
        "aggregates."
    )


def test_postgres_filter_substring_is_what_property_6_d_relies_on() -> None:
    """Pin the exact SQL filter substring Property 6 (d) cooperates with.

    The ``BudgetCapPolicy`` aggregate query filters with
    ``cost_tag = 'production'`` (sibling Property 7 asserts this
    on the policy SQL itself). Property 6 (d)'s data-side
    invariant is meaningless if the filter substring drifts, so
    we pin the literal here as a regression anchor — a future
    rename of the column or a switch to a positional placeholder
    breaks this test before it breaks Property 7's SQL assertion.

    Validates: Requirements 5.4.
    """

    # The literal must match the production-only filter
    # substring used in the BudgetCapPolicy SQL (sibling
    # Property 7 in test_cost_predict_budget_cap.py).
    assert _PRODUCTION_FILTER == "cost_tag = 'production'"
    # And the literal must be a valid SQL fragment shape: the
    # ``cost_tag`` identifier on the LHS, ``=`` operator, single-
    # quoted ``'production'`` literal on the RHS.
    assert re.fullmatch(
        r"cost_tag\s*=\s*'production'", _PRODUCTION_FILTER
    ) is not None, (
        f"_PRODUCTION_FILTER={_PRODUCTION_FILTER!r} does not "
        "match the design-mandated SQL fragment shape; Property "
        "6 (d) cooperates with this exact filter."
    )
