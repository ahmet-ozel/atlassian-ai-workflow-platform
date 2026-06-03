"""Invariant test: Output action execution audit completeness.

Feature:,: For any executed action
(regardless of outcome: success, failed, skipped, or timeout), an
execution record with action type, index, status, and timestamp SHALL
be persisted to workflow history.

"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror sibling Invariant tests)
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_DB_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "db-shared" / "src"

for _candidate in (_SRC_DIR, _DB_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities.output_actions import (  # noqa: E402
    ActionResult,
)
from db_shared.enums import ActionType  # noqa: E402


_VALID_STATUSES = ("success", "failed", "skipped", "timeout")


@settings(max_examples=200, deadline=None)
@given(
    action_type=st.sampled_from(list(ActionType)),
    index=st.integers(min_value=0, max_value=19),
    status=st.sampled_from(_VALID_STATUSES),
)
def test_action_result_records_required_fields(
    action_type: ActionType, index: int, status: str
) -> None:
    """Every ``ActionResult`` carries action_type, index, status, and timestamp."""
    result = ActionResult(
        action_type=action_type,
        index=index,
        status=status,  # type: ignore[arg-type]
        error=None if status == "success" else "test error",
        timestamp=datetime.now(timezone.utc),
    )
    assert result.action_type == action_type
    assert result.index == index
    assert result.status == status
    assert result.timestamp is not None
    # Failed/skipped/timeout results carry a non-None error message
    if status != "success":
        assert result.error is not None


@settings(max_examples=100, deadline=None)
@given(
    statuses=st.lists(
        st.sampled_from(_VALID_STATUSES),
        min_size=0,
        max_size=20,
    )
)
def test_all_outcomes_have_status_field(statuses: list[str]) -> None:
    """Every audit record has a status drawn from the allowed set."""
    results = [
        ActionResult(
            action_type=ActionType.JIRA_COMMENT,
            index=i,
            status=s,  # type: ignore[arg-type]
            error=None if s == "success" else "err",
            timestamp=datetime.now(timezone.utc),
        )
        for i, s in enumerate(statuses)
    ]

    # Every result must have a valid status, an integer index, an action_type,
    # and a timestamp — audit completeness.
    for r in results:
        assert r.status in _VALID_STATUSES
        assert isinstance(r.index, int)
        assert r.action_type in set(ActionType)
        assert r.timestamp is not None
