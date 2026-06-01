"""Property test: ``needs_info`` timeout parity across workers.

Spec: ``platform-real-usage-gaps`` — Property 1.

**Validates: Requirements 1.1, 1.2, 1.6, 1.7**

Background
----------

Two Temporal workers park on a ``wait_condition`` for the
``info_received`` (or sibling ``CommentAddedSignal``) signal when the
gateway / agent-runner detects ambiguous task metadata:

* ``automation-worker`` —
  :data:`automation_worker.workflows.automation_workflow._NEEDS_INFO_TIMEOUT`.
* ``agent-runner-worker`` —
  :data:`agent_runner.workflows.agent_runner_workflow.SIGNAL_WAIT_TIMEOUT`.

Before this spec the two constants disagreed: the gateway parked for
24 hours while the agent-runner parked for 7 days, and the Jira
``_format_needs_info_timeout_comment`` helper printed *"24 saat"*.
End-users who relied on the visible wording were burnt — the bot
"vazgeçti" before the printed deadline. R1.1 / R1.2 / R1.3 lock the
three values to a single source of truth (``timedelta(days=7)`` /
"7 gün"). R1.6 renames the legacy ``test_timeout_is_24_hours`` unit
test; R1.7 (this file) is the **CI parity test** that prevents drift.

Strategy
--------

The property is fully deterministic — the two constants are imported
at module scope and asserted equal in a parametrised matrix that
also pins the canonical value (``timedelta(days=7)``) and confirms
the Jira comment string carries the same wording.

No Hypothesis strategies are needed: the contract has exactly one
acceptable point in the value space (the constant), so a parametrised
example test is the right shape. Any drift surfaces immediately at
CI time.

The test file itself lives under ``platform/tests/property/`` (the
shared property suite) rather than under either worker's own test
tree because **its purpose is to bridge the two workers**: it must
import both modules at once, which neither worker's pytest config
allows in isolation.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap — expose both worker source roots so the
# constants and helpers can be imported without pip-installing each
# worker package. Mirrors the pattern in
# ``test_multi_iter_po_review.py`` and ``test_temporal_loop_cap.py``.
# ---------------------------------------------------------------------------

# tests/property/test_needs_info_timeout_parity.py → platform/
_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "workers" / "automation-worker" / "src",
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# noqa: E402 — imports must follow the ``sys.path`` bootstrap above.

from automation_worker.workflows.automation_workflow import (  # noqa: E402
    _NEEDS_INFO_TIMEOUT,
    _format_needs_info_timeout_comment,
)
from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    SIGNAL_WAIT_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Canonical value — single source of truth.
# ---------------------------------------------------------------------------

#: The canonical needs_info timeout value pinned by R1.1 / R1.2.
#: Any drift in either constant or the Jira comment string fails the
#: matrix below.
_CANONICAL_TIMEOUT: Final[timedelta] = timedelta(days=7)


# ---------------------------------------------------------------------------
# Property 1: needs_info Timeout Parity
# ---------------------------------------------------------------------------


class TestNeedsInfoTimeoutParity:
    """**Validates: Requirements 1.1, 1.2, 1.6, 1.7**

    The two workers' park-on-signal timeouts and the user-visible
    Jira comment string MUST agree on a single value
    (:data:`_CANONICAL_TIMEOUT` = ``timedelta(days=7)``).
    """

    def test_automation_worker_timeout_is_seven_days(self) -> None:
        # R1.1 — ``automation_worker._NEEDS_INFO_TIMEOUT`` MUST be
        # ``timedelta(days=7)``; the legacy ``timedelta(hours=24)``
        # value is removed.
        assert _NEEDS_INFO_TIMEOUT == _CANONICAL_TIMEOUT
        assert _NEEDS_INFO_TIMEOUT == timedelta(days=7)
        assert _NEEDS_INFO_TIMEOUT.total_seconds() == 7 * 24 * 60 * 60

    def test_agent_runner_signal_wait_timeout_is_seven_days(self) -> None:
        # R1.2 — ``agent_runner.SIGNAL_WAIT_TIMEOUT`` MUST be the same
        # value so the two workers do not disagree on how long they
        # wait for the user.
        assert SIGNAL_WAIT_TIMEOUT == _CANONICAL_TIMEOUT
        assert SIGNAL_WAIT_TIMEOUT == timedelta(days=7)

    def test_two_constants_are_equal(self) -> None:
        # The headline parity property — if either constant drifts in
        # isolation this assertion catches it before any other test.
        assert _NEEDS_INFO_TIMEOUT == SIGNAL_WAIT_TIMEOUT, (
            "needs_info timeout parity broken: "
            f"automation_worker._NEEDS_INFO_TIMEOUT={_NEEDS_INFO_TIMEOUT!r} "
            f"vs agent_runner.SIGNAL_WAIT_TIMEOUT={SIGNAL_WAIT_TIMEOUT!r}"
        )

    def test_jira_comment_mentions_seven_days(self) -> None:
        # R1.3 / R1.7 — the Jira comment posted when the wait expires
        # MUST mention "7 gün" so the user-visible wording matches the
        # actual constant. The legacy "24 saat" string is removed.
        body = _format_needs_info_timeout_comment()
        assert "7 gün" in body, (
            f"Jira timeout comment does not mention '7 gün'; got: {body!r}"
        )
        assert "24 saat" not in body, (
            "Legacy '24 saat' wording leaked back into the Jira "
            f"timeout comment; got: {body!r}"
        )

    def test_jira_comment_marks_issue_as_stale(self) -> None:
        # The terminal transition is ``stale`` (R4.5 of the upstream
        # gap-fill spec, preserved by R1.x); the comment surface must
        # reference it so operators can grep for the audit trail.
        body = _format_needs_info_timeout_comment()
        assert "stale" in body

    @pytest.mark.parametrize(
        ("constant_name", "value"),
        [
            ("automation_worker._NEEDS_INFO_TIMEOUT", _NEEDS_INFO_TIMEOUT),
            ("agent_runner.SIGNAL_WAIT_TIMEOUT", SIGNAL_WAIT_TIMEOUT),
        ],
    )
    def test_canonical_value_matrix(
        self, constant_name: str, value: timedelta
    ) -> None:
        # Parametrised version of the parity assertion that gives
        # operators a clear failure name when a single side drifts.
        assert value == _CANONICAL_TIMEOUT, (
            f"{constant_name}={value!r} drifted from canonical "
            f"{_CANONICAL_TIMEOUT!r} (timedelta(days=7))"
        )

    def test_jira_comment_days_count_matches_constant(self) -> None:
        # The integer rendered in the Turkish prose ("7 gün") must
        # equal ``_NEEDS_INFO_TIMEOUT.days``; this catches a future
        # change that bumps the constant but forgets to update the
        # comment template.
        body = _format_needs_info_timeout_comment()
        days = _NEEDS_INFO_TIMEOUT.days
        assert f"{days} gün" in body, (
            f"Jira comment does not mention '{days} gün' to match "
            f"_NEEDS_INFO_TIMEOUT.days={days}; got: {body!r}"
        )
