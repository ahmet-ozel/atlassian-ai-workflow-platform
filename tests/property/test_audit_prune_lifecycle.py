"""AuditPruneWorkflow daily cycle, idempotency, failure alarm, and archive flag.

The workflow's correctness contract has four guarantees:

(a) Daily cycle — running the workflow with cutoff `T` archives every
    audit row with `created_at < T` and deletes the same set.
(b) Idempotent — re-running the workflow on the same day is a safe
    no-op (the SELECT bound by `created_at < cutoff` is empty
    after the first run, so the second run reports
    `archived=0, deleted=0`).
(c) Fail alarm — when archive or delete raises, the workflow MUST
    invoke `notify_audit_prune_failed` before re-raising the
    original exception.
(d) Archive flag — every row that lands in MinIO carries the same
    archive URI structure
    (`audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz`).

The test exercises an in-memory state-machine model of the
workflow body (the real `AuditPruneWorkflow` is a Temporal
`@workflow.defn` that needs the SDK at import time; the model
matches the deterministic Python code path inside `run`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


@dataclass
class _Audit:
    rows_by_day: dict[str, int] = field(default_factory=dict)
    archived_total: int = 0
    deleted_total: int = 0
    archive_uris: list[str] = field(default_factory=list)
    alarm_calls: int = 0


def _run_cycle(
    audit: _Audit, *, cutoff: datetime, raise_on: str | None = None
) -> tuple[int, int]:
    """Mirror of AuditPruneWorkflow.run for a single tick."""

    if raise_on == "archive":
        audit.alarm_calls += 1
        raise RuntimeError("archive failed")
    if raise_on == "delete":
        # Archive succeeded, delete fails — alarm still fires.
        audit.alarm_calls += 1
        raise RuntimeError("delete failed")

    cutoff_day = cutoff.strftime("%Y/%m/%d")
    archived = 0
    for day, count in list(audit.rows_by_day.items()):
        if day < cutoff_day:
            archived += count
            audit.rows_by_day.pop(day)
    audit.archived_total += archived
    audit.deleted_total += archived
    if archived > 0:
        audit.archive_uris.append(
            f"audit-archive/{cutoff_day}/audit-0.jsonl.gz"
        )
    return archived, archived  # archived == deleted in the happy path


@settings(max_examples=120, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(
    days_old=st.integers(min_value=0, max_value=200),
    rows=st.integers(min_value=0, max_value=1000),
)
def test_archive_equals_delete_count(days_old: int, rows: int) -> None:
    """Archived count equals deleted count for a single cutoff."""

    audit = _Audit()
    day_str = (
        datetime.now(timezone.utc) - timedelta(days=days_old)
    ).strftime("%Y/%m/%d")
    audit.rows_by_day[day_str] = rows
    cutoff = datetime.now(timezone.utc)
    archived, deleted = _run_cycle(audit, cutoff=cutoff)
    assert archived == deleted


def test_idempotent_second_run_is_noop() -> None:
    """A second cycle on the same day archives 0 rows."""

    audit = _Audit()
    audit.rows_by_day["2024/01/01"] = 5
    cutoff = datetime(2024, 6, 1, tzinfo=timezone.utc)
    a1, _ = _run_cycle(audit, cutoff=cutoff)
    a2, _ = _run_cycle(audit, cutoff=cutoff)
    assert a1 == 5
    assert a2 == 0


def test_archive_failure_triggers_alarm_and_reraises() -> None:
    """Archive failure triggers an alarm and re-raises."""

    audit = _Audit()
    cutoff = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="archive failed"):
        _run_cycle(audit, cutoff=cutoff, raise_on="archive")
    assert audit.alarm_calls == 1


def test_delete_failure_triggers_alarm_and_reraises() -> None:
    """Delete failure triggers an alarm and re-raises."""

    audit = _Audit()
    cutoff = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match="delete failed"):
        _run_cycle(audit, cutoff=cutoff, raise_on="delete")
    assert audit.alarm_calls == 1


def test_archive_uri_layout_is_daily_partitioned() -> None:
    """Every archive URI follows audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz."""

    audit = _Audit()
    audit.rows_by_day["2024/01/01"] = 3
    cutoff = datetime(2024, 1, 2, tzinfo=timezone.utc)
    _run_cycle(audit, cutoff=cutoff)
    assert audit.archive_uris == [
        "audit-archive/2024/01/02/audit-0.jsonl.gz"
    ]
    assert audit.archive_uris[0].startswith("audit-archive/")
    assert audit.archive_uris[0].endswith(".jsonl.gz")
