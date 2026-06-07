"""Property-based tests for runner assignment audit completeness.
--------------------------------------------------

*For any* runner assignment addition or removal, the system SHALL write
exactly one audit event (``dept_ssh_runner_assigned`` or
``dept_ssh_runner_unassigned``). *For any* runner selection by
``runner_resolver``, the audit event ``ssh_runner_selected`` SHALL
contain ``dept_id``, ``runner_id``, and ``selection_reason``.

This test module verifies the audit completeness invariants by:

(a) Simulating assignment reconciliation (add/remove runners) and
    asserting that exactly one audit event is emitted per change.
(b) Simulating runner selection and asserting the ``ssh_runner_selected``
    event payload contains all required fields.

The tests use in-memory stubs for the database and audit sink to
isolate the audit logic from infrastructure concerns.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# In-memory audit sink stub
# ---------------------------------------------------------------------------


@dataclass
class AuditEventRecord:
    """Captured audit event for assertion."""

    action: str
    dept_id: str | None
    resource: str
    payload: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class InMemoryAuditSink:
    """In-memory audit sink that captures all written events."""

    def __init__(self) -> None:
        self.events: list[AuditEventRecord] = []

    async def write(self, event: Any) -> None:
        """Capture an audit event."""
        self.events.append(
            AuditEventRecord(
                action=event.action,
                dept_id=event.dept_id,
                resource=event.resource,
                payload=event.payload or {},
                timestamp=event.timestamp,
            )
        )

    def clear(self) -> None:
        self.events.clear()

    def filter_by_action(self, action: str) -> list[AuditEventRecord]:
        return [e for e in self.events if e.action == action]


# ---------------------------------------------------------------------------
# Assignment reconciliation logic (extracted from ssh_runners.py)
# ---------------------------------------------------------------------------


async def reconcile_assignments(
    *,
    dept_id: str,
    current_runner_ids: set[str],
    desired_runner_ids: set[str],
    audit_sink: InMemoryAuditSink,
    actor_id: str = "admin-user",
) -> dict[str, Any]:
    """Reconcile runner assignments and emit audit events.

    This mirrors the logic in
    ``services/admin-dashboard-api/src/routers/ssh_runners.py``
    ``update_dept_ssh_runners`` endpoint.

    For each runner added: emits ``dept_ssh_runner_assigned``.
    For each runner removed: emits ``dept_ssh_runner_unassigned``.
    """
    to_add = desired_runner_ids - current_runner_ids
    to_remove = current_runner_ids - desired_runner_ids

    now = datetime.now(timezone.utc)

    # Emit audit events for additions
    for runner_id in sorted(to_add):
        from audit_logger import AuditEvent

        event = AuditEvent(
            actor_id=actor_id,
            actor_role="admin",
            dept_id=dept_id,
            action="dept_ssh_runner_assigned",
            resource=f"department:{dept_id}/runner:{runner_id}",
            result="ok",
            timestamp=now,
            payload={
                "dept_id": dept_id,
                "runner_id": runner_id,
                "assigned_at": now.isoformat(),
            },
        )
        await audit_sink.write(event)

    # Emit audit events for removals
    for runner_id in sorted(to_remove):
        from audit_logger import AuditEvent

        event = AuditEvent(
            actor_id=actor_id,
            actor_role="admin",
            dept_id=dept_id,
            action="dept_ssh_runner_unassigned",
            resource=f"department:{dept_id}/runner:{runner_id}",
            result="ok",
            timestamp=now,
            payload={
                "dept_id": dept_id,
                "runner_id": runner_id,
                "unassigned_at": now.isoformat(),
            },
        )
        await audit_sink.write(event)

    return {
        "added": sorted(to_add),
        "removed": sorted(to_remove),
    }


# ---------------------------------------------------------------------------
# Runner selection audit logic (extracted from runner_resolver.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunnerCandidate:
    """A runner candidate for selection."""

    runner_id: str
    host: str
    port: int
    username: str
    vault_path: str
    active_count: int
    priority: int


async def select_runner_with_audit(
    *,
    dept_id: str,
    candidates: list[RunnerCandidate],
    audit_sink: InMemoryAuditSink,
) -> dict[str, Any] | None:
    """Select the least-busy runner and emit audit event.

    This mirrors the logic in
    ``workers/execution-runner-worker/src/activities/runner_resolver.py``
    ``resolve_runner`` activity.

    Selection algorithm:
    1. Sort by active_count ASC, then priority ASC.
    2. Select the first runner.
    3. Emit ``ssh_runner_selected`` audit event with required fields.

    Returns None if no candidates are available.
    """
    if not candidates:
        return None

    # Sort by least-busy (active_count ASC), then priority ASC
    sorted_candidates = sorted(
        candidates, key=lambda r: (r.active_count, r.priority)
    )
    selected = sorted_candidates[0]

    # Determine selection reason
    selection_reason = "least_busy" if len(candidates) > 1 else "only_one"

# Emit audit event
    from audit_logger import AuditEvent

    now = datetime.now(timezone.utc)
    event = AuditEvent(
        actor_id="execution-runner-worker",
        actor_role="system",
        dept_id=dept_id,
        action="ssh_runner_selected",
        resource=f"runner_resolver:{dept_id}",
        result="ok",
        timestamp=now,
        payload={
            "dept_id": dept_id,
            "runner_id": selected.runner_id,
            "selection_reason": selection_reason,
        },
    )
    await audit_sink.write(event)

    return {
        "runner_id": selected.runner_id,
        "selection_reason": selection_reason,
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Strategy for valid runner IDs
runner_id_strategy = st.from_regex(r"^[a-z][a-z0-9-]{1,20}$", fullmatch=True)

#: Strategy for valid department IDs
dept_id_strategy = st.from_regex(r"^[a-z][a-z0-9-]{1,16}$", fullmatch=True)


@st.composite
def _assignment_change_strategy(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a random assignment change scenario.

    Produces a dept_id, a set of current runner assignments, and a set
    of desired runner assignments. The sets may overlap (no change for
    those runners), differ (additions/removals), or be disjoint.
    """
    dept_id = draw(dept_id_strategy)

    # Generate a pool of available runner IDs
    all_runners = draw(
        st.lists(
            runner_id_strategy,
            min_size=1,
            max_size=8,
            unique=True,
        )
    )

    # Current assignments: subset of all runners
    current = draw(
        st.frozensets(
            st.sampled_from(all_runners),
            min_size=0,
            max_size=len(all_runners),
        )
    )

    # Desired assignments: subset of all runners
    desired = draw(
        st.frozensets(
            st.sampled_from(all_runners),
            min_size=0,
            max_size=len(all_runners),
        )
    )

    return {
        "dept_id": dept_id,
        "current_runner_ids": set(current),
        "desired_runner_ids": set(desired),
    }


@st.composite
def _runner_candidates_strategy(draw: st.DrawFn) -> dict[str, Any]:
    """Generate a random runner selection scenario.

    Produces a dept_id and a list of runner candidates with varying
    active workflow counts and priorities.
    """
    dept_id = draw(dept_id_strategy)

    num_candidates = draw(st.integers(min_value=1, max_value=6))
    runner_ids = draw(
        st.lists(
            runner_id_strategy,
            min_size=num_candidates,
            max_size=num_candidates,
            unique=True,
        )
    )

    candidates = []
    for rid in runner_ids:
        candidates.append(
            RunnerCandidate(
                runner_id=rid,
                host=draw(
                    st.from_regex(r"^[a-z][a-z0-9.-]{1,20}$", fullmatch=True)
                ),
                port=draw(st.integers(min_value=1, max_value=65535)),
                username=draw(
                    st.from_regex(r"^[a-z][a-z0-9-]{1,12}$", fullmatch=True)
                ),
                vault_path=f"vault:ssh/runners/{rid}/active",
                active_count=draw(st.integers(min_value=0, max_value=50)),
                priority=draw(st.integers(min_value=1, max_value=1000)),
            )
        )

    return {
        "dept_id": dept_id,
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# Assignment audit event count invariant
# ---------------------------------------------------------------------------


class TestAssignmentAuditCompleteness:
    """Each assignment add/remove produces exactly one audit event.

    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_assignment_change_strategy())
    def test_exactly_one_audit_event_per_assignment_change(
        self, scenario: dict[str, Any]
    ) -> None:
        """For any set of current and desired runner assignments, the
        reconciliation logic emits exactly one ``dept_ssh_runner_assigned``
        event per added runner and exactly one
        ``dept_ssh_runner_unassigned`` event per removed runner.
        """
        dept_id = scenario["dept_id"]
        current = scenario["current_runner_ids"]
        desired = scenario["desired_runner_ids"]

        expected_additions = desired - current
        expected_removals = current - desired

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            reconcile_assignments(
                dept_id=dept_id,
                current_runner_ids=current,
                desired_runner_ids=desired,
                audit_sink=audit_sink,
            )
        )

        # Count audit events by type
        assigned_events = audit_sink.filter_by_action("dept_ssh_runner_assigned")
        unassigned_events = audit_sink.filter_by_action("dept_ssh_runner_unassigned")

        # Exactly one event per addition
        assert len(assigned_events) == len(expected_additions), (
            f"Expected {len(expected_additions)} assigned events, "
            f"got {len(assigned_events)}. "
            f"Additions: {expected_additions}"
        )

        # Exactly one event per removal
        assert len(unassigned_events) == len(expected_removals), (
            f"Expected {len(expected_removals)} unassigned events, "
            f"got {len(unassigned_events)}. "
            f"Removals: {expected_removals}"
        )

        # Total events = additions + removals (no extra events)
        total_expected = len(expected_additions) + len(expected_removals)
        assert len(audit_sink.events) == total_expected, (
            f"Expected {total_expected} total audit events, "
            f"got {len(audit_sink.events)}"
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_assignment_change_strategy())
    def test_assigned_events_contain_correct_runner_ids(
        self, scenario: dict[str, Any]
    ) -> None:
        """Each ``dept_ssh_runner_assigned`` event payload contains the
        correct ``dept_id`` and ``runner_id`` for the added runner.
        """
        dept_id = scenario["dept_id"]
        current = scenario["current_runner_ids"]
        desired = scenario["desired_runner_ids"]

        expected_additions = desired - current

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            reconcile_assignments(
                dept_id=dept_id,
                current_runner_ids=current,
                desired_runner_ids=desired,
                audit_sink=audit_sink,
            )
        )

        assigned_events = audit_sink.filter_by_action("dept_ssh_runner_assigned")
        assigned_runner_ids = {e.payload["runner_id"] for e in assigned_events}

        # Every added runner has exactly one corresponding event
        assert assigned_runner_ids == expected_additions, (
            f"Assigned event runner_ids {assigned_runner_ids} != "
            f"expected additions {expected_additions}"
        )

        # Each event has the correct dept_id
        for event in assigned_events:
            assert event.dept_id == dept_id
            assert event.payload["dept_id"] == dept_id
            assert "assigned_at" in event.payload

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_assignment_change_strategy())
    def test_unassigned_events_contain_correct_runner_ids(
        self, scenario: dict[str, Any]
    ) -> None:
        """Each ``dept_ssh_runner_unassigned`` event payload contains the
        correct ``dept_id`` and ``runner_id`` for the removed runner.
        """
        dept_id = scenario["dept_id"]
        current = scenario["current_runner_ids"]
        desired = scenario["desired_runner_ids"]

        expected_removals = current - desired

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            reconcile_assignments(
                dept_id=dept_id,
                current_runner_ids=current,
                desired_runner_ids=desired,
                audit_sink=audit_sink,
            )
        )

        unassigned_events = audit_sink.filter_by_action("dept_ssh_runner_unassigned")
        unassigned_runner_ids = {e.payload["runner_id"] for e in unassigned_events}

        # Every removed runner has exactly one corresponding event
        assert unassigned_runner_ids == expected_removals, (
            f"Unassigned event runner_ids {unassigned_runner_ids} != "
            f"expected removals {expected_removals}"
        )

        # Each event has the correct dept_id
        for event in unassigned_events:
            assert event.dept_id == dept_id
            assert event.payload["dept_id"] == dept_id
            assert "unassigned_at" in event.payload

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_assignment_change_strategy())
    def test_no_audit_events_when_no_changes(
        self, scenario: dict[str, Any]
    ) -> None:
        """When current and desired assignments are identical, no audit
        events are emitted.
        """
        dept_id = scenario["dept_id"]
        current = scenario["current_runner_ids"]

        # Use same set for both current and desired  no changes
        audit_sink = InMemoryAuditSink()

        asyncio.run(
            reconcile_assignments(
                dept_id=dept_id,
                current_runner_ids=current,
                desired_runner_ids=current,  # same as current
                audit_sink=audit_sink,
            )
        )

        assert len(audit_sink.events) == 0, (
            f"Expected 0 audit events when no changes, "
            f"got {len(audit_sink.events)}"
        )


# ---------------------------------------------------------------------------
# Runner selection audit required fields
# ---------------------------------------------------------------------------


class TestRunnerSelectionAuditCompleteness:
    """``ssh_runner_selected`` event contains all required fields.

    """

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_runner_candidates_strategy())
    def test_ssh_runner_selected_contains_required_fields(
        self, scenario: dict[str, Any]
    ) -> None:
        """For any runner selection, the ``ssh_runner_selected`` audit event
        SHALL contain ``dept_id``, ``runner_id``, and ``selection_reason``
        in its payload.
        """
        dept_id = scenario["dept_id"]
        candidates = scenario["candidates"]

        audit_sink = InMemoryAuditSink()

        result = asyncio.run(
            select_runner_with_audit(
                dept_id=dept_id,
                candidates=candidates,
                audit_sink=audit_sink,
            )
        )

        # Selection should succeed (candidates is non-empty)
        assert result is not None

        # Exactly one ssh_runner_selected event
        selected_events = audit_sink.filter_by_action("ssh_runner_selected")
        assert len(selected_events) == 1, (
            f"Expected exactly 1 ssh_runner_selected event, "
            f"got {len(selected_events)}"
        )

        event = selected_events[0]

        # Required fields in payload
        assert "dept_id" in event.payload, (
            "ssh_runner_selected event missing 'dept_id' in payload"
        )
        assert "runner_id" in event.payload, (
            "ssh_runner_selected event missing 'runner_id' in payload"
        )
        assert "selection_reason" in event.payload, (
            "ssh_runner_selected event missing 'selection_reason' in payload"
        )

        # Values are correct
        assert event.payload["dept_id"] == dept_id
        assert event.payload["runner_id"] == result["runner_id"]
        assert event.payload["selection_reason"] in ("least_busy", "only_one")

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_runner_candidates_strategy())
    def test_selection_reason_only_one_when_single_candidate(
        self, scenario: dict[str, Any]
    ) -> None:
        """When there is exactly one candidate, ``selection_reason`` SHALL
        be ``"only_one"``. When there are multiple candidates,
        ``selection_reason`` SHALL be ``"least_busy"``.
        """
        dept_id = scenario["dept_id"]
        candidates = scenario["candidates"]

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            select_runner_with_audit(
                dept_id=dept_id,
                candidates=candidates,
                audit_sink=audit_sink,
            )
        )

        selected_events = audit_sink.filter_by_action("ssh_runner_selected")
        assert len(selected_events) == 1

        event = selected_events[0]
        expected_reason = "only_one" if len(candidates) == 1 else "least_busy"
        assert event.payload["selection_reason"] == expected_reason, (
            f"Expected selection_reason='{expected_reason}' for "
            f"{len(candidates)} candidates, "
            f"got '{event.payload['selection_reason']}'"
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_runner_candidates_strategy())
    def test_selected_runner_matches_least_busy_algorithm(
        self, scenario: dict[str, Any]
    ) -> None:
        """The ``runner_id`` in the audit event matches the runner selected
        by the least-busy algorithm (minimum active_count, tiebreak by
        priority).
        """
        dept_id = scenario["dept_id"]
        candidates = scenario["candidates"]

        # Compute expected selection independently
        sorted_candidates = sorted(
            candidates, key=lambda r: (r.active_count, r.priority)
        )
        expected_runner_id = sorted_candidates[0].runner_id

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            select_runner_with_audit(
                dept_id=dept_id,
                candidates=candidates,
                audit_sink=audit_sink,
            )
        )

        selected_events = audit_sink.filter_by_action("ssh_runner_selected")
        assert len(selected_events) == 1

        event = selected_events[0]
        assert event.payload["runner_id"] == expected_runner_id, (
            f"Expected runner_id='{expected_runner_id}', "
            f"got '{event.payload['runner_id']}'"
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(dept_id=dept_id_strategy)
    def test_no_audit_event_when_no_candidates(
        self, dept_id: str
    ) -> None:
        """When there are no candidates (empty list), no
        ``ssh_runner_selected`` event is emitted and the function
        returns None.
        """
        audit_sink = InMemoryAuditSink()

        result = asyncio.run(
            select_runner_with_audit(
                dept_id=dept_id,
                candidates=[],
                audit_sink=audit_sink,
            )
        )

        assert result is None
        assert len(audit_sink.events) == 0, (
            f"Expected 0 audit events for empty candidates, "
            f"got {len(audit_sink.events)}"
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(scenario=_runner_candidates_strategy())
    def test_audit_event_dept_id_matches_request(
        self, scenario: dict[str, Any]
    ) -> None:
        """The ``dept_id`` field on the audit event itself (not just in
        payload) matches the department for which the runner was resolved.
        """
        dept_id = scenario["dept_id"]
        candidates = scenario["candidates"]

        audit_sink = InMemoryAuditSink()

        asyncio.run(
            select_runner_with_audit(
                dept_id=dept_id,
                candidates=candidates,
                audit_sink=audit_sink,
            )
        )

        selected_events = audit_sink.filter_by_action("ssh_runner_selected")
        assert len(selected_events) == 1

        event = selected_events[0]
        assert event.dept_id == dept_id, (
            f"Audit event dept_id='{event.dept_id}' != "
            f"request dept_id='{dept_id}'"
        )
