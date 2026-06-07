"""Property-based tests for Runner Queue Status endpoint logic.

Background
----------

The ``GET /admin/runner/queue-status`` endpoint queries
``automation.execution_workspaces`` to compute:

* ``active_count`` - workspaces with ``status='running'``.
* ``queued_count`` - workspaces with ``status='queued'``.
* ``avg_wait_seconds`` - mean of ``started_at - queued_at`` for the
  last 10 completed workspaces (ordered by ``finished_at DESC``).
* ``max_concurrent_global`` - ``RUNNER_MAX_CONCURRENT`` env (default 5).
* ``by_dept`` - per-department active/queued breakdown with quota.

Strategy
--------

We build a fake asyncpg pool that returns pre-computed rows matching
the SQL queries in ``_fetch_queue_status``. Using Hypothesis we
generate random workspace fixtures (varying counts of running, queued,
and completed workspaces across multiple departments) and verify that
the endpoint logic produces mathematically correct results.

The tests cover:
(a) active_count correctly counts ``status='running'`` rows
(b) queued_count correctly counts ``status='queued'`` rows
(c) avg_wait_seconds is computed from the last 10 completed workspaces
(d) max_concurrent_global matches the RUNNER_MAX_CONCURRENT env
(e) by_dept breakdown is correct per department
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose the admin-dashboard-api source root and
# required shared libraries so we can import the router module.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SERVICE_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "services" / "admin-dashboard-api"
)

if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _PLATFORM_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from src.routers.runner_workspaces import (  # noqa: E402
    _fetch_queue_status,
    _RUNNER_MAX_CONCURRENT,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceRow:
    """Represents a row in automation.execution_workspaces."""

    dept_id: str
    status: str  # 'running', 'queued', 'completed'
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class FakeConnection:
    """Fake asyncpg connection that computes results from workspace rows."""

    def __init__(self, workspaces: list[WorkspaceRow]) -> None:
        self._workspaces = workspaces

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Simulate asyncpg fetchrow for the two queries in _fetch_queue_status."""
        if "active_count" in query and "queued_count" in query:
            # First query: counts of running and queued
            active = sum(1 for w in self._workspaces if w.status == "running")
            queued = sum(1 for w in self._workspaces if w.status == "queued")
            return {"active_count": active, "queued_count": queued}
        elif "avg_wait_seconds" in query:
            # Second query: average wait from last 10 completed
            completed = [
                w
                for w in self._workspaces
                if w.status == "completed"
                and w.started_at is not None
                and w.queued_at is not None
            ]
            # Sort by finished_at DESC, take last 10
            completed.sort(
                key=lambda w: w.finished_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            recent = completed[:10]
            if recent:
                total_wait = sum(
                    (w.started_at - w.queued_at).total_seconds() for w in recent
                )
                avg = total_wait / len(recent)
            else:
                avg = 0.0
            return {"avg_wait_seconds": avg}
        return None

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        """Simulate asyncpg fetch for the dept breakdown query."""
        # Per-department breakdown of running/queued
        dept_counts: dict[str, dict[str, int]] = {}
        for w in self._workspaces:
            if w.status in ("running", "queued"):
                if w.dept_id not in dept_counts:
                    dept_counts[w.dept_id] = {"active": 0, "queued": 0}
                if w.status == "running":
                    dept_counts[w.dept_id]["active"] += 1
                elif w.status == "queued":
                    dept_counts[w.dept_id]["queued"] += 1

        rows = []
        for dept_id in sorted(dept_counts.keys()):
            rows.append(
                {
                    "dept_id": dept_id,
                    "active": dept_counts[dept_id]["active"],
                    "queued": dept_counts[dept_id]["queued"],
                }
            )
        return rows


class FakePool:
    """Fake asyncpg pool that yields a FakeConnection."""

    def __init__(self, workspaces: list[WorkspaceRow]) -> None:
        self._workspaces = workspaces

    def acquire(self):
        """Return an async context manager yielding a FakeConnection."""
        return _FakeAcquireCtx(self._workspaces)


class _FakeAcquireCtx:
    """Async context manager for FakePool.acquire()."""

    def __init__(self, workspaces: list[WorkspaceRow]) -> None:
        self._conn = FakeConnection(workspaces)

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _fetch_status(pool: Any) -> dict[str, Any]:
    """Run the async router helper in a fresh event loop."""

    result: dict[str, Any] | None = None
    error: BaseException | None = None

    def _target() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(_fetch_queue_status(pool))
        except BaseException as exc:  # noqa: BLE001
            error = exc

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    if error is not None:
        raise error
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department IDs used in generated fixtures.
_DEPT_IDS = st.sampled_from(["payments", "platform", "devops", "data", "infra"])

#: Base time for generating timestamps.
_BASE_TIME = datetime(2024, 1, 1, tzinfo=timezone.utc)


@st.composite
def workspace_fixtures(draw: st.DrawFn) -> list[WorkspaceRow]:
    """Generate a list of workspace rows with varying statuses."""
    n_running = draw(st.integers(min_value=0, max_value=10))
    n_queued = draw(st.integers(min_value=0, max_value=10))
    n_completed = draw(st.integers(min_value=0, max_value=20))

    workspaces: list[WorkspaceRow] = []

    # Running workspaces
    for i in range(n_running):
        dept = draw(_DEPT_IDS)
        queued_at = _BASE_TIME + timedelta(minutes=draw(st.integers(0, 1000)))
        started_at = queued_at + timedelta(seconds=draw(st.integers(1, 600)))
        workspaces.append(
            WorkspaceRow(
                dept_id=dept,
                status="running",
                queued_at=queued_at,
                started_at=started_at,
            )
        )

    # Queued workspaces
    for i in range(n_queued):
        dept = draw(_DEPT_IDS)
        queued_at = _BASE_TIME + timedelta(minutes=draw(st.integers(0, 1000)))
        workspaces.append(
            WorkspaceRow(
                dept_id=dept,
                status="queued",
                queued_at=queued_at,
            )
        )

    # Completed workspaces
    for i in range(n_completed):
        dept = draw(_DEPT_IDS)
        queued_at = _BASE_TIME + timedelta(minutes=draw(st.integers(0, 1000)))
        wait_seconds = draw(st.integers(1, 3600))
        started_at = queued_at + timedelta(seconds=wait_seconds)
        duration = draw(st.integers(10, 7200))
        finished_at = started_at + timedelta(seconds=duration)
        workspaces.append(
            WorkspaceRow(
                dept_id=dept,
                status="completed",
                queued_at=queued_at,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    return workspaces


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestRunnerQueueStatusActiveCount:
    """active_count correctly counts status='running' rows."""

    @given(workspaces=workspace_fixtures())
    @settings(max_examples=50)
    def test_active_count_matches_running_workspaces(
        self, workspaces: list[WorkspaceRow]
    ) -> None:
        """active_count equals the number of 'running' workspaces."""
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        expected_active = sum(1 for w in workspaces if w.status == "running")
        assert result["active_count"] == expected_active


class TestRunnerQueueStatusQueuedCount:
    """queued_count correctly counts status='queued' rows."""

    @given(workspaces=workspace_fixtures())
    @settings(max_examples=50)
    def test_queued_count_matches_queued_workspaces(
        self, workspaces: list[WorkspaceRow]
    ) -> None:
        """queued_count equals the number of 'queued' workspaces."""
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        expected_queued = sum(1 for w in workspaces if w.status == "queued")
        assert result["queued_count"] == expected_queued


class TestRunnerQueueStatusAvgWait:
    """avg_wait_seconds is computed from the last 10 completed workspaces."""

    @given(workspaces=workspace_fixtures())
    @settings(max_examples=50)
    def test_avg_wait_seconds_mathematically_correct(
        self, workspaces: list[WorkspaceRow]
    ) -> None:
        """avg_wait_seconds matches manual computation from last 10 completed."""
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        # Replicate the computation
        completed = [
            w
            for w in workspaces
            if w.status == "completed"
            and w.started_at is not None
            and w.queued_at is not None
        ]
        completed.sort(
            key=lambda w: w.finished_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        recent = completed[:10]

        if recent:
            total_wait = sum(
                (w.started_at - w.queued_at).total_seconds() for w in recent
            )
            expected_avg = round(total_wait / len(recent), 2)
        else:
            expected_avg = 0.0

        assert result["avg_wait_seconds"] == expected_avg

    def test_avg_wait_zero_when_no_completed(self) -> None:
        """avg_wait_seconds is 0 when there are no completed workspaces."""
        workspaces = [
            WorkspaceRow(dept_id="payments", status="running"),
            WorkspaceRow(dept_id="platform", status="queued"),
        ]
        pool = FakePool(workspaces)
        result = _fetch_status(pool)
        assert result["avg_wait_seconds"] == 0.0

    def test_avg_wait_uses_only_last_10(self) -> None:
        """avg_wait_seconds uses only the 10 most recent completed workspaces."""
        # Create 15 completed workspaces with known wait times
        workspaces: list[WorkspaceRow] = []
        for i in range(15):
            queued_at = _BASE_TIME + timedelta(hours=i)
            # First 5 (oldest) have 100s wait, last 10 (newest) have 50s wait
            if i < 5:
                wait = 100
            else:
                wait = 50
            started_at = queued_at + timedelta(seconds=wait)
            finished_at = started_at + timedelta(minutes=10 + i)
            workspaces.append(
                WorkspaceRow(
                    dept_id="payments",
                    status="completed",
                    queued_at=queued_at,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )

        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        # The last 10 by finished_at DESC all have 50s wait
        assert result["avg_wait_seconds"] == 50.0


class TestRunnerQueueStatusMaxConcurrent:
    """max_concurrent_global matches the RUNNER_MAX_CONCURRENT env."""

    def test_max_concurrent_matches_env_default(self) -> None:
        """max_concurrent_global defaults to 5 (RUNNER_MAX_CONCURRENT)."""
        pool = FakePool([])
        result = _fetch_status(pool)
        assert result["max_concurrent_global"] == _RUNNER_MAX_CONCURRENT

    def test_max_concurrent_reflects_env_variable(self) -> None:
        """max_concurrent_global reflects the RUNNER_MAX_CONCURRENT env value."""
        pool = FakePool([])
        result = _fetch_status(pool)
        # The value is read at module import time, so it should match
        # the current module-level constant
        assert result["max_concurrent_global"] == _RUNNER_MAX_CONCURRENT
        assert isinstance(result["max_concurrent_global"], int)


class TestRunnerQueueStatusByDept:
    """by_dept breakdown is correct per department."""

    @given(workspaces=workspace_fixtures())
    @settings(max_examples=50)
    def test_by_dept_breakdown_correct(
        self, workspaces: list[WorkspaceRow]
    ) -> None:
        """by_dept active/queued counts match per-department totals."""
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        # Compute expected per-dept breakdown
        expected_depts: dict[str, dict[str, int]] = {}
        for w in workspaces:
            if w.status in ("running", "queued"):
                if w.dept_id not in expected_depts:
                    expected_depts[w.dept_id] = {"active": 0, "queued": 0}
                if w.status == "running":
                    expected_depts[w.dept_id]["active"] += 1
                elif w.status == "queued":
                    expected_depts[w.dept_id]["queued"] += 1

        by_dept = result["by_dept"]

        # Same number of departments
        assert len(by_dept) == len(expected_depts)

        # Each department's counts match
        for entry in by_dept:
            dept_id = entry["dept_id"]
            assert dept_id in expected_depts, (
                f"Unexpected dept_id '{dept_id}' in by_dept"
            )
            assert entry["active"] == expected_depts[dept_id]["active"]
            assert entry["queued"] == expected_depts[dept_id]["queued"]
            assert entry["quota"] == _RUNNER_MAX_CONCURRENT

    def test_by_dept_empty_when_no_active_workspaces(self) -> None:
        """by_dept is empty when there are no running or queued workspaces."""
        workspaces = [
            WorkspaceRow(
                dept_id="payments",
                status="completed",
                queued_at=_BASE_TIME,
                started_at=_BASE_TIME + timedelta(seconds=30),
                finished_at=_BASE_TIME + timedelta(minutes=5),
            ),
        ]
        pool = FakePool(workspaces)
        result = _fetch_status(pool)
        assert result["by_dept"] == []

    def test_by_dept_sorted_by_dept_id(self) -> None:
        """by_dept entries are sorted alphabetically by dept_id."""
        workspaces = [
            WorkspaceRow(dept_id="platform", status="running"),
            WorkspaceRow(dept_id="devops", status="queued"),
            WorkspaceRow(dept_id="payments", status="running"),
        ]
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        dept_ids = [entry["dept_id"] for entry in result["by_dept"]]
        assert dept_ids == sorted(dept_ids)

    def test_by_dept_quota_matches_max_concurrent(self) -> None:
        """Each dept entry's quota equals max_concurrent_global."""
        workspaces = [
            WorkspaceRow(dept_id="payments", status="running"),
            WorkspaceRow(dept_id="platform", status="queued"),
        ]
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        for entry in result["by_dept"]:
            assert entry["quota"] == result["max_concurrent_global"]


class TestRunnerQueueStatusResponseShape:
    """The response shape matches the queue-status schema."""

    def test_response_has_all_required_fields(self) -> None:
        """Response contains all required top-level fields."""
        pool = FakePool([])
        result = _fetch_status(pool)

        required_keys = {
            "active_count",
            "queued_count",
            "avg_wait_seconds",
            "max_concurrent_global",
            "by_dept",
        }
        assert set(result.keys()) == required_keys

    def test_response_types_correct(self) -> None:
        """Response field types are correct."""
        workspaces = [
            WorkspaceRow(dept_id="payments", status="running"),
            WorkspaceRow(dept_id="platform", status="queued"),
            WorkspaceRow(
                dept_id="devops",
                status="completed",
                queued_at=_BASE_TIME,
                started_at=_BASE_TIME + timedelta(seconds=45),
                finished_at=_BASE_TIME + timedelta(minutes=10),
            ),
        ]
        pool = FakePool(workspaces)
        result = _fetch_status(pool)

        assert isinstance(result["active_count"], int)
        assert isinstance(result["queued_count"], int)
        assert isinstance(result["avg_wait_seconds"], float)
        assert isinstance(result["max_concurrent_global"], int)
        assert isinstance(result["by_dept"], list)

    def test_missing_execution_workspace_table_returns_empty_shape(self) -> None:
        """Fresh installs without the queue table still render the dashboard."""

        class MissingTableConnection:
            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
                raise RuntimeError(
                    'relation "automation.execution_workspaces" does not exist'
                )

            async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
                return []

        class MissingTablePool:
            def acquire(self) -> Any:
                return _MissingTableAcquireCtx()

        class _MissingTableAcquireCtx:
            async def __aenter__(self) -> MissingTableConnection:
                return MissingTableConnection()

            async def __aexit__(self, *args: Any) -> None:
                pass

        result = _fetch_status(MissingTablePool())

        assert result == {
            "active_count": 0,
            "queued_count": 0,
            "avg_wait_seconds": 0.0,
            "max_concurrent_global": _RUNNER_MAX_CONCURRENT,
            "by_dept": [],
        }
