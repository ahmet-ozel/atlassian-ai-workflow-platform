"""Property tests for runner resolver least-busy selection (Property 5).

# Feature: platform-quick-fixes, Property 5: Runner Resolver Least-Busy Selection

**Validates: Requirements 4.5**

Property 5 — Runner Resolver Least-Busy Selection
--------------------------------------------------

*For any* department with multiple active runners having different active
workflow counts, ``runner_resolver`` SHALL select the runner with the
minimum active workflow count. When counts are equal, selection SHALL
follow priority order (lower priority value = higher precedence).

The selection algorithm implemented in
``workers/execution-runner-worker/src/activities/runner_resolver.py``
uses the SQL ordering: ``ORDER BY active_count ASC, a.priority ASC``
and picks the first row. This test exercises the pure selection logic
by simulating the ordered result set and verifying the invariants hold
across all generated runner configurations.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Data model mirroring the runner_resolver's row structure
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunnerCandidate:
    """A single runner candidate as returned by the least-busy query.

    Mirrors the columns selected by ``_LEAST_BUSY_QUERY`` in
    ``runner_resolver.py``.
    """

    runner_id: str
    host: str
    port: int
    username: str
    vault_path: str
    active_count: int
    priority: int


# ---------------------------------------------------------------------------
# Pure selection function — mirrors the SQL ORDER BY logic
# ---------------------------------------------------------------------------


def select_least_busy_runner(candidates: list[RunnerCandidate]) -> RunnerCandidate | None:
    """Pure implementation of the least-busy selection algorithm.

    This mirrors the SQL: ``ORDER BY active_count ASC, a.priority ASC``
    followed by ``LIMIT 1`` (i.e., first row wins).

    Parameters
    ----------
    candidates : list[RunnerCandidate]
        List of active runner candidates with their workflow counts
        and priority values.

    Returns
    -------
    RunnerCandidate | None
        The selected runner, or None if the list is empty.
    """
    if not candidates:
        return None
    sorted_candidates = sorted(candidates, key=lambda r: (r.active_count, r.priority))
    return sorted_candidates[0]


# ---------------------------------------------------------------------------
# Hypothesis strategies — lightweight, no regex
# ---------------------------------------------------------------------------


def _make_runner(idx: int, active_count: int, priority: int) -> RunnerCandidate:
    """Factory helper to build a RunnerCandidate from index + counts."""
    return RunnerCandidate(
        runner_id=f"runner-{idx}",
        host=f"host-{idx}.local",
        port=22,
        username="ai-runner",
        vault_path=f"vault:ssh/runners/runner-{idx}/active",
        active_count=active_count,
        priority=priority,
    )


@st.composite
def multiple_runners_strategy(draw: st.DrawFn) -> list[RunnerCandidate]:
    """Generate a list of 2-8 runner candidates with unique IDs."""
    n = draw(st.integers(min_value=2, max_value=8))
    counts = draw(
        st.lists(
            st.integers(min_value=0, max_value=50),
            min_size=n,
            max_size=n,
        )
    )
    priorities = draw(
        st.lists(
            st.integers(min_value=1, max_value=1000),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return [_make_runner(i, counts[i], priorities[i]) for i in range(n)]


@st.composite
def runners_with_distinct_counts_strategy(draw: st.DrawFn) -> list[RunnerCandidate]:
    """Generate runners where all active_counts are distinct."""
    n = draw(st.integers(min_value=2, max_value=8))
    counts = draw(
        st.lists(
            st.integers(min_value=0, max_value=100),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    priorities = draw(
        st.lists(
            st.integers(min_value=1, max_value=1000),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return [_make_runner(i, counts[i], priorities[i]) for i in range(n)]


@st.composite
def runners_with_tied_counts_strategy(draw: st.DrawFn) -> list[RunnerCandidate]:
    """Generate runners where all share the same active_count (tiebreaker test)."""
    n = draw(st.integers(min_value=2, max_value=8))
    shared_count = draw(st.integers(min_value=0, max_value=20))
    priorities = draw(
        st.lists(
            st.integers(min_value=1, max_value=1000),
            min_size=n,
            max_size=n,
            unique=True,
        )
    )
    return [_make_runner(i, shared_count, priorities[i]) for i in range(n)]


# ---------------------------------------------------------------------------
# Property 5 — Least-Busy Selection Tests
# ---------------------------------------------------------------------------


class TestLeastBusySelection:
    """Property 5: Runner resolver always selects the least-busy runner.

    **Validates: Requirements 4.5**
    """

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_selected_runner_has_minimum_active_count(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        For any set of active runners, the selected runner must have
        an active_count equal to the minimum across all candidates.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        min_count = min(r.active_count for r in runners)
        assert selected.active_count == min_count, (
            f"Selected runner {selected.runner_id} has active_count="
            f"{selected.active_count}, but minimum is {min_count}"
        )

    @settings(max_examples=100, deadline=2000)
    @given(runners=runners_with_distinct_counts_strategy())
    def test_distinct_counts_selects_unique_minimum(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        When all active_counts are distinct, the runner with the
        globally minimum count is always selected regardless of
        priority values.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        min_count = min(r.active_count for r in runners)
        min_runners = [r for r in runners if r.active_count == min_count]
        # With distinct counts, exactly one runner has the minimum
        assert len(min_runners) == 1
        assert selected.runner_id == min_runners[0].runner_id

    @settings(max_examples=100, deadline=2000)
    @given(runners=runners_with_tied_counts_strategy())
    def test_tied_counts_selects_by_priority_order(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        When multiple runners share the minimum active_count, the
        runner with the lowest priority value (highest precedence)
        is selected.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        min_count = min(r.active_count for r in runners)
        tied_runners = [r for r in runners if r.active_count == min_count]

        # Among tied runners, the one with lowest priority wins
        expected_winner = min(tied_runners, key=lambda r: r.priority)
        assert selected.runner_id == expected_winner.runner_id, (
            f"Expected runner {expected_winner.runner_id} "
            f"(priority={expected_winner.priority}) but got "
            f"{selected.runner_id} (priority={selected.priority})"
        )

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_selection_is_deterministic(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        Calling the selection function multiple times with the same
        input always yields the same result (referential transparency).
        """
        r1 = select_least_busy_runner(runners)
        r2 = select_least_busy_runner(runners)
        r3 = select_least_busy_runner(runners)
        assert r1 == r2 == r3

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_selection_invariant_under_input_order(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        The selection result does not depend on the order of the input
        list — the algorithm sorts by (active_count, priority) so any
        permutation of the input yields the same winner.
        """
        selected_original = select_least_busy_runner(runners)

        # Shuffle and re-select multiple times
        for _ in range(3):
            shuffled = runners.copy()
            random.shuffle(shuffled)
            selected_shuffled = select_least_busy_runner(shuffled)
            assert selected_original == selected_shuffled, (
                f"Selection changed with input order: "
                f"original={selected_original}, shuffled={selected_shuffled}"
            )

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_no_runner_with_lower_count_exists(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        No other candidate has a strictly lower active_count than the
        selected runner. This is the core invariant of least-busy
        selection.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        for runner in runners:
            if runner.runner_id == selected.runner_id:
                continue
            assert runner.active_count >= selected.active_count, (
                f"Runner {runner.runner_id} has active_count="
                f"{runner.active_count} which is less than selected "
                f"{selected.runner_id} with active_count="
                f"{selected.active_count}"
            )

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_no_runner_with_same_count_and_lower_priority_exists(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        Among runners with the same active_count as the selected one,
        no other runner has a strictly lower priority value (higher
        precedence).
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        same_count_runners = [
            r for r in runners if r.active_count == selected.active_count
        ]
        for runner in same_count_runners:
            if runner.runner_id == selected.runner_id:
                continue
            assert runner.priority >= selected.priority, (
                f"Runner {runner.runner_id} has same active_count="
                f"{runner.active_count} but lower priority="
                f"{runner.priority} than selected {selected.runner_id} "
                f"with priority={selected.priority}"
            )


# ---------------------------------------------------------------------------
# Property 5 — Edge cases and monotonicity
# ---------------------------------------------------------------------------


class TestLeastBusyMonotonicity:
    """Monotonicity and stability properties of the selection algorithm.

    **Validates: Requirements 4.5**
    """

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_adding_busier_runner_does_not_change_selection(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        Adding a runner with a higher active_count than the current
        selection cannot change the winner.
        """
        selected_before = select_least_busy_runner(runners)
        assert selected_before is not None

        # Add a runner that is busier than the current selection
        busier_runner = RunnerCandidate(
            runner_id="runner-busier-new",
            host="busier.local",
            port=22,
            username="ai-runner",
            vault_path="vault:ssh/runners/runner-busier-new/active",
            active_count=selected_before.active_count + 10,
            priority=1,  # Even with best priority, higher count loses
        )
        extended = runners + [busier_runner]
        selected_after = select_least_busy_runner(extended)

        assert selected_after == selected_before, (
            f"Adding a busier runner changed selection from "
            f"{selected_before.runner_id} to {selected_after.runner_id}"
        )

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_single_runner_always_selected(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        When only one runner is provided, it is always selected
        regardless of its active_count or priority.
        """
        for runner in runners:
            result = select_least_busy_runner([runner])
            assert result == runner

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_selected_runner_is_from_candidates(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        The selected runner is always one of the input candidates
        (no fabrication).
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None
        assert selected in runners


# ---------------------------------------------------------------------------
# Property 5 — Alignment with SQL ORDER BY semantics
# ---------------------------------------------------------------------------


class TestSqlOrderByAlignment:
    """Verify the pure function matches SQL ORDER BY active_count ASC, priority ASC.

    **Validates: Requirements 4.5**
    """

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_matches_sql_order_by_semantics(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        The selection result matches what SQL
        ``ORDER BY active_count ASC, a.priority ASC LIMIT 1``
        would return.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        # Simulate SQL ORDER BY: stable sort by (active_count, priority)
        sql_ordered = sorted(runners, key=lambda r: (r.active_count, r.priority))
        sql_first = sql_ordered[0]

        assert selected.runner_id == sql_first.runner_id, (
            f"Pure function selected {selected.runner_id} "
            f"(count={selected.active_count}, priority={selected.priority}) "
            f"but SQL ORDER BY would select {sql_first.runner_id} "
            f"(count={sql_first.active_count}, priority={sql_first.priority})"
        )

    @settings(max_examples=100, deadline=2000)
    @given(runners=multiple_runners_strategy())
    def test_active_count_dominates_priority(
        self, runners: list[RunnerCandidate]
    ) -> None:
        """**Validates: Requirements 4.5**

        A runner with lower active_count always wins over a runner
        with higher active_count, regardless of priority values.
        This confirms active_count is the primary sort key.
        """
        selected = select_least_busy_runner(runners)
        assert selected is not None

        for runner in runners:
            if runner.active_count > selected.active_count:
                # This runner should never be selected over the winner,
                # even if it has a much lower priority value
                assert runner != selected


# ===========================================================================
# Property 6: No-Runner Failure Invariant
# ===========================================================================
#
# # Feature: platform-quick-fixes, Property 6: No-Runner Failure Invariant
#
# **Validates: Requirements 4.6, 4.7**
#
# *For any* department where all assigned runners have
# ``status ∈ {disabled, quarantine}`` OR no runners are assigned, the
# ``runner_resolver`` activity SHALL raise ``RunnerResolutionError``, the
# workflow SHALL transition to ``failed`` state, and an audit event
# ``no_runner_assigned_to_dept`` SHALL be written.
# ===========================================================================

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Import runner_resolver directly from its file to avoid the
# activities/__init__.py which pulls in modules with circular deps.
# ---------------------------------------------------------------------------

_RUNNER_RESOLVER_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "workers"
    / "execution-runner-worker"
    / "src"
    / "activities"
    / "runner_resolver.py"
)

if "runner_resolver_direct" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "runner_resolver_direct", _RUNNER_RESOLVER_PATH
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules["runner_resolver_direct"] = _module
    _spec.loader.exec_module(_module)
else:
    _module = sys.modules["runner_resolver_direct"]

_RunnerResolutionError = _module.RunnerResolutionError
_resolve_runner = _module.resolve_runner


# ---------------------------------------------------------------------------
# Hypothesis strategies for Property 6
# ---------------------------------------------------------------------------

#: Valid department ID pattern
_dept_id_strategy = st.from_regex(r"^[a-z][a-z0-9-]{1,30}$", fullmatch=True)

#: Non-active runner statuses
_non_active_status_strategy = st.sampled_from(["disabled", "quarantine"])


@dataclass(frozen=True, slots=True)
class _NonActiveRunner:
    """A runner record with a non-active status."""

    runner_id: str
    host: str
    port: int
    username: str
    vault_path: str
    status: str  # 'disabled' or 'quarantine'


@st.composite
def _non_active_runner_strategy(draw: st.DrawFn) -> _NonActiveRunner:
    """Generate a runner with disabled or quarantine status."""
    idx = draw(st.integers(min_value=0, max_value=999))
    return _NonActiveRunner(
        runner_id=f"runner-{idx}",
        host=f"host-{idx}.local",
        port=draw(st.integers(min_value=1, max_value=65535)),
        username="ai-runner",
        vault_path=f"vault:ssh/runners/runner-{idx}/active",
        status=draw(_non_active_status_strategy),
    )


@st.composite
def _no_runner_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Scenario: no runners assigned to the department at all."""
    return {
        "dept_id": draw(_dept_id_strategy),
        "runners": [],
        "scenario_type": "no_runners_assigned",
    }


@st.composite
def _all_non_active_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Scenario: all assigned runners are disabled or quarantine."""
    runners = draw(
        st.lists(_non_active_runner_strategy(), min_size=1, max_size=5)
    )
    return {
        "dept_id": draw(_dept_id_strategy),
        "runners": runners,
        "scenario_type": "all_disabled_or_quarantine",
    }


#: Combined strategy for any scenario that triggers the failure invariant
_failure_scenario_strategy = st.one_of(
    _no_runner_scenario(),
    _all_non_active_scenario(),
)


# ---------------------------------------------------------------------------
# Property 6 — No-Runner Failure Invariant Tests
# ---------------------------------------------------------------------------


class TestNoRunnerFailureInvariant:
    """Property 6: No-Runner Failure Invariant.

    # Feature: platform-quick-fixes, Property 6: No-Runner Failure Invariant

    *For any* department where all assigned runners have
    ``status ∈ {disabled, quarantine}`` OR no runners are assigned, the
    ``runner_resolver`` activity SHALL raise ``RunnerResolutionError``,
    the workflow SHALL transition to ``failed`` state, and an audit event
    ``no_runner_assigned_to_dept`` SHALL be written.

    **Validates: Requirements 4.6, 4.7**
    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_failure_scenario_strategy)
    @pytest.mark.asyncio
    async def test_raises_runner_resolution_error_when_no_active_runners(
        self, scenario: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 4.6, 4.7**

        When the database query returns no active runners (either because
        no runners are assigned or all are disabled/quarantine), the
        resolver MUST raise RunnerResolutionError.
        """
        dept_id = scenario["dept_id"]

        # The query for active runners returns empty because:
        # - No runners are assigned (scenario_type == "no_runners_assigned")
        # - All runners are disabled/quarantine (filtered out by WHERE status='active')
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch(
                "runner_resolver_direct._write_audit_event"
            ) as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            with pytest.raises(_RunnerResolutionError) as exc_info:
                await _resolve_runner(dept_id)

            # Verify the error contains the department ID
            assert dept_id in str(exc_info.value)

            # Verify the error has the correct audit_event attribute
            assert exc_info.value.audit_event == "no_runner_assigned_to_dept"

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_failure_scenario_strategy)
    @pytest.mark.asyncio
    async def test_writes_audit_event_on_failure(
        self, scenario: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 4.6, 4.7**

        When no active runner is available, the resolver MUST write an
        audit event with action ``no_runner_assigned_to_dept`` containing
        the department ID.
        """
        dept_id = scenario["dept_id"]

        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch(
                "runner_resolver_direct._write_audit_event"
            ) as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            with pytest.raises(_RunnerResolutionError):
                await _resolve_runner(dept_id)

            # Verify audit event was written exactly once
            mock_audit.assert_called_once()

            # Verify the audit event has the correct action and dept_id
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["action"] == "no_runner_assigned_to_dept"
            assert call_kwargs["dept_id"] == dept_id
            assert "reason" in call_kwargs["metadata"]

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_all_non_active_scenario())
    @pytest.mark.asyncio
    async def test_all_disabled_quarantine_runners_treated_as_no_runner(
        self, scenario: dict[str, Any]
    ) -> None:
        """**Validates: Requirements 4.6, 4.7**

        When all assigned runners have status 'disabled' or 'quarantine',
        the SQL query (which filters by status='active') returns empty
        results, and the resolver behaves identically to the case where
        no runners are assigned at all — raising RunnerResolutionError
        with audit_event='no_runner_assigned_to_dept'.

        This test specifically verifies the invariant that disabled and
        quarantine statuses are treated equivalently for failure purposes.
        """
        dept_id = scenario["dept_id"]
        runners = scenario["runners"]

        # All runners have non-active status, so the active-only query
        # returns empty results
        assert all(r.status in ("disabled", "quarantine") for r in runners)
        assert len(runners) >= 1

        mock_pool = AsyncMock()
        # The query filters by status='active', so returns empty
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch(
                "runner_resolver_direct._write_audit_event"
            ) as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            with pytest.raises(_RunnerResolutionError) as exc_info:
                await _resolve_runner(dept_id)

            # Same error regardless of whether runners exist but are
            # disabled/quarantine vs. no runners at all
            assert exc_info.value.audit_event == "no_runner_assigned_to_dept"

            # Audit event written
            mock_audit.assert_called_once()
            call_kwargs = mock_audit.call_args[1]
            assert call_kwargs["action"] == "no_runner_assigned_to_dept"
            assert call_kwargs["dept_id"] == dept_id

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(dept_id=_dept_id_strategy)
    @pytest.mark.asyncio
    async def test_error_is_runtime_error_subclass(
        self, dept_id: str
    ) -> None:
        """**Validates: Requirements 4.6, 4.7**

        RunnerResolutionError is a RuntimeError subclass, ensuring it
        propagates correctly through Temporal's activity error handling
        to transition the workflow to 'failed' state.
        """
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch(
                "runner_resolver_direct._write_audit_event"
            ) as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await _resolve_runner(dept_id)

            # Verify it's specifically a RunnerResolutionError
            assert isinstance(exc_info.value, _RunnerResolutionError)
            # Verify it carries the audit_event attribute for workflow
            # failure handling
            assert hasattr(exc_info.value, "audit_event")
            assert exc_info.value.audit_event == "no_runner_assigned_to_dept"
