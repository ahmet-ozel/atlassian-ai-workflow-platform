"""Property tests for cleanup decision truth table and replay TTL post-state.

Pure infrastructural invariants - cleanup decision and replay TTL
post-state.

This module covers two independent pure-function invariants from
the documented design:

1. **Cleanup decision truth table.** ``should_cleanup`` from
   ``temporal_shared.helpers`` matches the documented truth table
   over the cartesian product of ``policy ∈ {"always", "on_success",
   "never"}`` and arbitrary integer exit codes.

2. **Replay TTL post-state.** ``cleanup_expired`` from
   ``decision.replay`` deletes exactly the rows whose ``expires_at < now``;
   equivalently, the post-state contains exactly
   ``{(h, ea) ∈ rows : ea >= now}``. The operation is idempotent:
   running the cleanup twice yields the same final state.

The replay implementation talks to an asyncpg pool, so this file ships
an **in-memory fake pool** that mirrors the SQL semantics needed by
``cleanup_expired``. The fake is intentionally small and deterministic
so Hypothesis can exercise it under property tests without a real
PostgreSQL instance.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors sibling property tests + unit tests)
# ---------------------------------------------------------------------------

#: ``automation-service/src`` so ``decision.replay`` imports cleanly when
#: pytest is invoked from any working directory.
_AUTOMATION_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

#: ``temporal-shared`` source for ``should_cleanup``. The workspace
#: ``pytest.ini`` already injects this onto ``pythonpath`` for runs
#: anchored at ``platform/``, but we add it defensively so the file
#: also works when invoked via ``pytest <file>`` directly.
_TEMPORAL_SHARED_SRC = (
    Path(__file__).resolve().parents[4] / "libs" / "temporal-shared" / "src"
)
if (
    _TEMPORAL_SHARED_SRC.is_dir()
    and str(_TEMPORAL_SHARED_SRC) not in sys.path
):
    sys.path.insert(0, str(_TEMPORAL_SHARED_SRC))

from decision.replay import cleanup_expired  # noqa: E402
from temporal_shared.helpers import should_cleanup  # noqa: E402

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

#: Bounded example count + generous deadline so slower CI machines don't
#: flake. The pure cleanup tests are O(1); the replay TTL tests scan a
#: bounded list (max 30 rows) so 200 examples completes in a few seconds.
_PROFILE = settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ===========================================================================
# Strategies
# ===========================================================================

# --- Cleanup decision strategies -------------------------------------------

#: The three valid cleanup policies recognised by ``should_cleanup``.
_CLEANUP_POLICY = st.sampled_from(["always", "on_success", "never"])

#: POSIX-style exit codes - including negative (e.g. signal-terminated
#: on POSIX, where ``exit_code = -signal``). The truth table is defined
#: for *any* integer, so we sample widely.
_EXIT_CODE = st.integers(min_value=-128, max_value=255)

#: Non-zero exit codes - used by the on_success/non-zero leg.
_NONZERO_EXIT_CODE = _EXIT_CODE.filter(lambda x: x != 0)

#: Strings that are *not* one of the three valid policies. Hypothesis
#: feeds these into ``should_cleanup`` to confirm it raises ``ValueError``.
_INVALID_POLICY = st.text(min_size=0, max_size=20).filter(
    lambda s: s not in {"always", "on_success", "never"}
)


# --- Replay TTL strategies -------------------------------------------------

#: A SHA-256 hex digest is exactly 64 lowercase hex characters. The
#: replay schema stores ``event_hash`` as the primary key, but for
#: post-state assertions we only need a stable, unique-able string;
#: Hypothesis ``unique_by`` deduplicates on the hash within a single
#: example so the in-memory fake's PK constraint is never violated.
_PAYLOAD_HASH = st.text(
    alphabet=st.sampled_from("0123456789abcdef"),
    min_size=64,
    max_size=64,
)

#: A bounded UTC datetime range. We pick an absolute window (2020
#: 2030) so all generated timestamps share a single tzinfo and direct
#: ``<`` / ``>=`` comparisons are well-defined.
_DT_MIN = datetime(2020, 1, 1, tzinfo=timezone.utc)
_DT_MAX = datetime(2030, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

_TIMESTAMP = st.datetimes(
    min_value=_DT_MIN.replace(tzinfo=None),
    max_value=_DT_MAX.replace(tzinfo=None),
).map(lambda dt: dt.replace(tzinfo=timezone.utc))


@st.composite
def _row(draw: st.DrawFn) -> tuple[str, datetime]:
    """A single ``(payload_hash, expires_at)`` row."""

    return (draw(_PAYLOAD_HASH), draw(_TIMESTAMP))


#: A list of ``(hash, expires_at)`` rows whose hashes are unique
#: (mirrors the PK constraint on ``automation.processed_events``).
_ROWS = st.lists(
    _row(),
    min_size=0,
    max_size=30,
    unique_by=lambda row: row[0],
)


# ===========================================================================
# In-memory fake asyncpg pool
# ===========================================================================


class _FakeConnection:
    """Minimal stand-in for an asyncpg ``Connection`` for cleanup_expired.

    Only ``execute`` is implemented and only the single ``DELETE`` shape
    used by ``decision.replay.cleanup_expired`` is honoured. Any other
    SQL raises ``NotImplementedError`` so accidental misuse is loud.
    """

    def __init__(self, store: dict[str, datetime]) -> None:
        self._store = store

    async def execute(self, query: str, *args: Any) -> str:
        normalised = " ".join(query.split()).lower()
        # The exact statement issued by cleanup_expired() is:
        # DELETE FROM automation.processed_events WHERE expires_at < $1
        if (
            "delete from automation.processed_events" in normalised
            and "where expires_at <" in normalised
        ):
            (cutoff,) = args
            assert isinstance(cutoff, datetime), (
                "cleanup_expired must pass a datetime as the cutoff"
            )
            expired = [
                h for h, ea in self._store.items() if ea < cutoff
            ]
            for h in expired:
                del self._store[h]
            # asyncpg returns a status string of the form "DELETE <n>".
            return f"DELETE {len(expired)}"
        raise NotImplementedError(
            f"FakeConnection only implements the cleanup_expired DELETE "
            f"shape; got: {query!r}"
        )


class _FakeAcquireContext:
    """Async-context-manager wrapper around ``_FakeConnection``."""

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakePool:
    """In-memory fake asyncpg ``Pool`` for cleanup_expired property tests.

    The pool wraps a single mutable ``dict[str, datetime]`` representing
    the ``automation.processed_events`` table keyed by ``event_hash``.
    Concurrency / connection multiplexing isn't required: the property
    tests run cleanup serially.
    """

    def __init__(self, rows: list[tuple[str, datetime]]) -> None:
        # ``dict`` enforces hash uniqueness - the same constraint as the
        # PK on ``automation.processed_events.event_hash``.
        self._store: dict[str, datetime] = dict(rows)

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(_FakeConnection(self._store))

    @property
    def snapshot(self) -> set[tuple[str, datetime]]:
        """Materialise the current table state as a set for assertions."""

        return {(h, ea) for h, ea in self._store.items()}


# ===========================================================================
# Cleanup decision truth table
# ===========================================================================


class TestCleanupDecisionTruthTable:
    """``should_cleanup`` matches the documented truth table.

    Truth table:

    +----------------+-----------------+-----------------+
    | policy         | exit_code == 0  | exit_code != 0  |
    +================+=================+=================+
    | ``always``     | True            | True            |
    +----------------+-----------------+-----------------+
    | ``on_success`` | True            | False           |
    +----------------+-----------------+-----------------+
    | ``never``      | False           | False           |
    +----------------+-----------------+-----------------+
    """

    @_PROFILE
    @given(exit_code=_EXIT_CODE)
    def test_always_returns_true_for_any_exit_code(
        self, exit_code: int
    ) -> None:
        """``("always", any)``  ``True``."""
        assert should_cleanup("always", exit_code) is True

    @_PROFILE
    @given(exit_code=_EXIT_CODE)
    def test_never_returns_false_for_any_exit_code(
        self, exit_code: int
    ) -> None:
        """``("never", any)``  ``False``."""
        assert should_cleanup("never", exit_code) is False

    def test_on_success_with_zero_exit_returns_true(self) -> None:
        """``("on_success", 0)``  ``True``."""
        assert should_cleanup("on_success", 0) is True

    @_PROFILE
    @given(exit_code=_NONZERO_EXIT_CODE)
    def test_on_success_with_nonzero_exit_returns_false(
        self, exit_code: int
    ) -> None:
        """``("on_success", != 0)``  ``False``."""
        assert should_cleanup("on_success", exit_code) is False

    @_PROFILE
    @given(policy=_CLEANUP_POLICY, exit_code=_EXIT_CODE)
    def test_truth_table_comprehensive(
        self, policy: str, exit_code: int
    ) -> None:
        """Comprehensive cartesian-product check across all valid policies
        and arbitrary exit codes.
        """
        result = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
        if policy == "always":
            assert result is True
        elif policy == "on_success":
            assert result is (exit_code == 0)
        elif policy == "never":
            assert result is False
        else:  # pragma: no cover - _CLEANUP_POLICY only emits the three
            pytest.fail(f"Unexpected policy: {policy!r}")

    @_PROFILE
    @given(policy=_INVALID_POLICY, exit_code=_EXIT_CODE)
    def test_invalid_policy_raises_value_error(
        self, policy: str, exit_code: int
    ) -> None:
        """Any non-canonical policy string must raise ``ValueError`` with
        the documented message prefix.
        """
        with pytest.raises(ValueError, match="Invalid cleanup policy"):
            should_cleanup(policy, exit_code)  # type: ignore[arg-type]

    @_PROFILE
    @given(policy=_CLEANUP_POLICY, exit_code=_EXIT_CODE)
    def test_deterministic(self, policy: str, exit_code: int) -> None:
        """``should_cleanup`` is a pure function: repeated invocations on
        identical inputs always return the same value.
        """
        r1 = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
        r2 = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
        r3 = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
        assert r1 is r2 is r3


# ===========================================================================
# Replay TTL post-state
# ===========================================================================


def _expected_post_state(
    rows: list[tuple[str, datetime]], now: datetime
) -> set[tuple[str, datetime]]:
    """Reference implementation: ``{(h, ea) ∈ rows : ea >= now}``."""

    return {(h, ea) for h, ea in rows if ea >= now}


class TestReplayTTLPostState:
    """``cleanup_expired`` deletes exactly the expired rows.

    The post-state of ``automation.processed_events`` after
    ``cleanup_expired(db, now=now)`` must equal
    ``{(h, ea) ∈ rows : ea >= now}``.
    """

    @_PROFILE
    @given(rows=_ROWS, now=_TIMESTAMP)
    @pytest.mark.asyncio
    async def test_post_state_matches_specification(
        self,
        rows: list[tuple[str, datetime]],
        now: datetime,
    ) -> None:
        """Hypothesis-generated ``(rows, now)`` pairs satisfy the post-state
        invariant: after one cleanup, the table contains exactly the
        non-expired rows.
        """
        pool = _FakePool(rows)
        await cleanup_expired(pool, now)  # type: ignore[arg-type]
        assert pool.snapshot == _expected_post_state(rows, now)

    @_PROFILE
    @given(rows=_ROWS, now=_TIMESTAMP)
    @pytest.mark.asyncio
    async def test_returned_count_equals_deleted_rows(
        self,
        rows: list[tuple[str, datetime]],
        now: datetime,
    ) -> None:
        """The integer returned by ``cleanup_expired`` equals the number
        of rows whose ``expires_at < now`` in the pre-state.
        """
        pool = _FakePool(rows)
        deleted = await cleanup_expired(pool, now)  # type: ignore[arg-type]
        expected_deleted = sum(1 for _, ea in rows if ea < now)
        assert deleted == expected_deleted

    @_PROFILE
    @given(rows=_ROWS, now=_TIMESTAMP)
    @pytest.mark.asyncio
    async def test_idempotent_second_call_is_noop(
        self,
        rows: list[tuple[str, datetime]],
        now: datetime,
    ) -> None:
        """Running ``cleanup_expired`` twice with the same ``now`` yields
        the same final state - and the second call deletes zero rows.
        """
        pool = _FakePool(rows)
        await cleanup_expired(pool, now)  # type: ignore[arg-type]
        first_state = pool.snapshot

        deleted_second = await cleanup_expired(pool, now)  # type: ignore[arg-type]
        second_state = pool.snapshot

        assert second_state == first_state
        assert deleted_second == 0
        assert second_state == _expected_post_state(rows, now)

    @_PROFILE
    @given(rows=_ROWS, now=_TIMESTAMP)
    @pytest.mark.asyncio
    async def test_post_state_is_subset_of_pre_state(
        self,
        rows: list[tuple[str, datetime]],
        now: datetime,
    ) -> None:
        """``cleanup_expired`` only deletes; it never inserts or mutates
        existing rows. The post-state is always a subset of the
        pre-state.
        """
        pre_state = {(h, ea) for h, ea in rows}
        pool = _FakePool(rows)
        await cleanup_expired(pool, now)  # type: ignore[arg-type]
        assert pool.snapshot.issubset(pre_state)

    @_PROFILE
    @given(rows=_ROWS, now=_TIMESTAMP)
    @pytest.mark.asyncio
    async def test_only_expired_rows_are_deleted(
        self,
        rows: list[tuple[str, datetime]],
        now: datetime,
    ) -> None:
        """Every row deleted has ``expires_at < now``; equivalently, every
        row whose ``expires_at >= now`` survives. This is the contrapositive
        of the post-state invariant and a useful redundant check.
        """
        pre_state = {(h, ea) for h, ea in rows}
        pool = _FakePool(rows)
        await cleanup_expired(pool, now)  # type: ignore[arg-type]
        post_state = pool.snapshot

        deleted = pre_state - post_state
        for _hash, expires_at in deleted:
            assert expires_at < now, (
                f"deleted row had expires_at={expires_at} >= now={now}"
            )

        survivors = post_state
        for _hash, expires_at in survivors:
            assert expires_at >= now, (
                f"surviving row had expires_at={expires_at} < now={now}"
            )

    @_PROFILE
    @given(rows=_ROWS)
    @pytest.mark.asyncio
    async def test_now_in_far_past_deletes_nothing(
        self,
        rows: list[tuple[str, datetime]],
    ) -> None:
        """When ``now`` is older than every ``expires_at``, no row is
        expired and the table is unchanged.
        """
        # Pick a ``now`` strictly less than every generated ``expires_at``.
        now = _DT_MIN - timedelta(days=1)
        pool = _FakePool(rows)
        deleted = await cleanup_expired(pool, now)  # type: ignore[arg-type]
        assert deleted == 0
        assert pool.snapshot == {(h, ea) for h, ea in rows}

    @_PROFILE
    @given(rows=_ROWS)
    @pytest.mark.asyncio
    async def test_now_in_far_future_deletes_everything(
        self,
        rows: list[tuple[str, datetime]],
    ) -> None:
        """When ``now`` is newer than every ``expires_at``, every row is
        expired and the table is empty afterwards.
        """
        now = _DT_MAX + timedelta(days=1)
        pool = _FakePool(rows)
        deleted = await cleanup_expired(pool, now)  # type: ignore[arg-type]
        assert deleted == len(rows)
        assert pool.snapshot == set()
