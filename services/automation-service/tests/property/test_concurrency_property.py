"""Property test: Per-department concurrency limit enforcement.

# Feature: platform-gap-fill
# Property 17: Concurrency Limit Enforcement (Q19)
# Validates: Requirements 19.1, 19.2 (with R19.3 cross-check)

For every ``(max_concurrent, current_count)`` pair, ``check_dept_concurrency``
behaviour satisfies the following invariants:

(A) **None-cap allow (R19.3 cross-check)** — ``max_concurrent is None`` →
    the check passes regardless of ``current_count``. The returned
    :class:`ConcurrencyCheckResult` has ``max_allowed is None`` and
    ``current == observed_count``.

(B) **Under-cap allow (R19.1)** — ``current_count < max_concurrent`` →
    the check returns a :class:`ConcurrencyCheckResult` with
    ``current == observed_count`` and ``max_allowed == max_concurrent``;
    no exception is raised.

(C) **At-or-over-cap reject (R19.2)** — ``current_count >= max_concurrent``
    (and ``max`` is not ``None``) → :class:`ConcurrencyLimitExceeded` is
    raised. The exception carries ``.current == observed_count``,
    ``.max_allowed == max_concurrent``, and ``.dept_id`` matching the
    caller-provided dept.

(D) **N+1 boundary transition** — for any ``max ∈ [1, 50]``, the
    ``(max - 1)`` check passes and the ``max`` check rejects. This is
    the explicit "departman aynı anda çalıştırabileceği N+1. workflow'u
    reddedilmeli" wording from R19.2.

(E) **Source label propagation** — the ``source`` field of the
    success result and of the exception is ``"temporal"`` when the
    Visibility client answers without raising, and ``"postgres"`` when
    the Visibility client raises (fallback) or is ``None``.

(F) **``extract_max_concurrent`` defensive parsing** — only positive
    integers (excluding ``bool``, which is a Python ``int`` subclass)
    map to themselves; everything else (``None``, missing key, ``0``,
    negatives, ``bool``, ``float``, ``str``, non-dict) maps to ``None``.

The fakes are kept symmetric with ``tests/unit/test_concurrency.py``
(``_FakePool`` + ``_FakeTemporalVisibility``) so a regression caught
here will reproduce against the unit-test fixture set as well.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — matches tests/unit/test_concurrency.py so the
# top-level ``concurrency`` module (i.e. ``src/concurrency.py``) imports
# cleanly without first installing the service wheel.
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from concurrency import (  # noqa: E402
    ConcurrencyCheckResult,
    ConcurrencyLimitExceeded,
    check_dept_concurrency,
    count_active_workflows,
    extract_max_concurrent,
)

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# ---------------------------------------------------------------------------
# Fakes — reused / duplicated from tests/unit/test_concurrency.py.
#
# We duplicate rather than import because property tests should remain
# self-contained: a refactor of the unit-test fixtures must not silently
# weaken the property surface. The shapes are identical so any drift is
# obvious in code review.
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, count: int) -> None:
        self._count = count
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        self.queries.append((query, args))
        return {"n": self._count}


class _FakePoolCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakePool:
    def __init__(self, count: int) -> None:
        self._conn = _FakeConn(count)

    def acquire(self) -> _FakePoolCtx:
        return _FakePoolCtx(self._conn)


class _FakeTemporalVisibility:
    """Mirrors the production duck-type for Visibility client.

    ``count`` is what ``count_workflows`` answers when ``raises`` is
    ``None``; otherwise ``raises`` is propagated and the gate is forced
    to engage the Postgres fallback.
    """

    def __init__(
        self, *, count: int = 0, raises: Exception | None = None
    ) -> None:
        self._count = count
        self._raises = raises
        self.queries: list[str | None] = []

    async def count_workflows(self, query: str | None = None) -> Any:
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises

        class _Result:
            def __init__(self, count: int) -> None:
                self.count = count

        return _Result(self._count)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Per the spec: max ∈ [1, 50], count ∈ [0, 100]. The wider count
# range deliberately overlaps with the cap so both branches (allow /
# reject) are exercised on every run.
_MAX_INT = st.integers(min_value=1, max_value=50)
_COUNT_INT = st.integers(min_value=0, max_value=100)
_DEPT_ID = st.from_regex(r"[a-z][a-z0-9-]{1,30}", fullmatch=True)


# ---------------------------------------------------------------------------
# Property A — None cap is a silent allow regardless of count (R19.3 anchor)
# ---------------------------------------------------------------------------


class TestNoneCapAllows:
    """**Property 17A: ``max_concurrent is None`` → silent allow.**

    Validates: Requirements 19.3 (cross-check feeding R19.1, R19.2).
    """

    @_PROFILE
    @given(current_count=_COUNT_INT, dept_id=_DEPT_ID)
    def test_none_max_never_raises(
        self,
        current_count: int,
        dept_id: str,
    ) -> None:
        pool = _FakePool(count=current_count)

        result = asyncio.run(
            check_dept_concurrency(
                dept_id, None, db=pool, temporal=None
            )
        )

        assert isinstance(result, ConcurrencyCheckResult)
        assert result.max_allowed is None
        assert result.current == current_count
        assert result.dept_id == dept_id


# ---------------------------------------------------------------------------
# Property B — current < max → allow (R19.1)
# ---------------------------------------------------------------------------


class TestUnderCapAllows:
    """**Property 17B: ``current < max`` → silent allow (R19.1).**"""

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        delta=st.integers(min_value=1, max_value=50),
        dept_id=_DEPT_ID,
    )
    def test_under_cap_returns_result(
        self,
        max_concurrent: int,
        delta: int,
        dept_id: str,
    ) -> None:
        # Constrain count to be strictly below the cap.
        current_count = max(0, max_concurrent - delta)
        assert current_count < max_concurrent  # invariant guard

        pool = _FakePool(count=current_count)

        result = asyncio.run(
            check_dept_concurrency(
                dept_id, max_concurrent, db=pool, temporal=None
            )
        )

        assert isinstance(result, ConcurrencyCheckResult)
        assert result.current == current_count
        assert result.max_allowed == max_concurrent
        assert result.dept_id == dept_id


# ---------------------------------------------------------------------------
# Property C — current >= max → reject (R19.2)
# ---------------------------------------------------------------------------


class TestAtOrOverCapRejects:
    """**Property 17C: ``current >= max`` → ConcurrencyLimitExceeded (R19.2).**"""

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        over=st.integers(min_value=0, max_value=50),
        dept_id=_DEPT_ID,
    )
    def test_at_or_over_cap_raises(
        self,
        max_concurrent: int,
        over: int,
        dept_id: str,
    ) -> None:
        current_count = max_concurrent + over  # >= cap
        assert current_count >= max_concurrent  # invariant guard

        pool = _FakePool(count=current_count)

        with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
            asyncio.run(
                check_dept_concurrency(
                    dept_id, max_concurrent, db=pool, temporal=None
                )
            )

        err = exc_info.value
        assert err.dept_id == dept_id
        assert err.current == current_count, (
            f"Expected exc.current={current_count}, got {err.current}"
        )
        assert err.max_allowed == max_concurrent, (
            f"Expected exc.max_allowed={max_concurrent}, got {err.max_allowed}"
        )


# ---------------------------------------------------------------------------
# Property D — N+1 boundary transition (R19.2 verbatim wording)
# ---------------------------------------------------------------------------


class TestNPlusOneBoundary:
    """**Property 17D: starting one more when current = max-1 passes,
    then re-checking with current = max rejects.**

    This is the literal "N+1 workflow start → 429" wording in
    ``tasks.md`` task 19.2 and Requirements 19.2.
    """

    @_PROFILE
    @given(max_concurrent=_MAX_INT, dept_id=_DEPT_ID)
    def test_max_minus_one_allows_then_max_rejects(
        self,
        max_concurrent: int,
        dept_id: str,
    ) -> None:
        # Step 1: count = max - 1 → must allow.
        below_pool = _FakePool(count=max_concurrent - 1)
        result = asyncio.run(
            check_dept_concurrency(
                dept_id, max_concurrent, db=below_pool, temporal=None
            )
        )
        assert result.current == max_concurrent - 1
        assert result.max_allowed == max_concurrent

        # Step 2: workflow N+1 starts → count is now max → must reject.
        at_pool = _FakePool(count=max_concurrent)
        with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
            asyncio.run(
                check_dept_concurrency(
                    dept_id, max_concurrent, db=at_pool, temporal=None
                )
            )
        assert exc_info.value.current == max_concurrent
        assert exc_info.value.max_allowed == max_concurrent

    @_PROFILE
    @given(max_concurrent=_MAX_INT)
    def test_strict_inequality_at_boundary(
        self,
        max_concurrent: int,
    ) -> None:
        """The comparison is ``>=`` not ``>`` — ``current == max`` rejects."""
        pool = _FakePool(count=max_concurrent)
        with pytest.raises(ConcurrencyLimitExceeded):
            asyncio.run(
                check_dept_concurrency(
                    "boundary-dept",
                    max_concurrent,
                    db=pool,
                    temporal=None,
                )
            )


# ---------------------------------------------------------------------------
# Property E — Source label is consistent with which counter answered
# ---------------------------------------------------------------------------


class TestSourceLabelPropagation:
    """**Property 17E: ``source`` label tracks which counter answered.**

    - Visibility client present and *does not* raise → ``"temporal"``.
    - Visibility client raises → fallback to Postgres → ``"postgres"``.
    - Visibility client absent (``None``) → ``"postgres"``.

    The label is propagated identically on the success path
    (``ConcurrencyCheckResult.source``) and on the reject path
    (``ConcurrencyLimitExceeded.source``).
    """

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        temporal_count=st.integers(min_value=0, max_value=100),
        pg_count=st.integers(min_value=0, max_value=100),
        dept_id=_DEPT_ID,
    )
    def test_temporal_answers_yields_temporal_source(
        self,
        max_concurrent: int,
        temporal_count: int,
        pg_count: int,
        dept_id: str,
    ) -> None:
        """When Temporal answers, the result must carry source='temporal'."""
        pool = _FakePool(count=pg_count)
        temporal = _FakeTemporalVisibility(count=temporal_count)

        if temporal_count >= max_concurrent:
            with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
                asyncio.run(
                    check_dept_concurrency(
                        dept_id,
                        max_concurrent,
                        db=pool,
                        temporal=temporal,
                    )
                )
            assert exc_info.value.source == "temporal"
            assert exc_info.value.current == temporal_count
        else:
            result = asyncio.run(
                check_dept_concurrency(
                    dept_id, max_concurrent, db=pool, temporal=temporal
                )
            )
            assert result.source == "temporal"
            assert result.current == temporal_count

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        pg_count=st.integers(min_value=0, max_value=100),
        dept_id=_DEPT_ID,
    )
    def test_temporal_failure_yields_postgres_source(
        self,
        max_concurrent: int,
        pg_count: int,
        dept_id: str,
    ) -> None:
        """When the Visibility client raises, source falls back to 'postgres'."""
        pool = _FakePool(count=pg_count)
        temporal = _FakeTemporalVisibility(
            raises=RuntimeError("visibility rpc unavailable")
        )

        if pg_count >= max_concurrent:
            with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
                asyncio.run(
                    check_dept_concurrency(
                        dept_id,
                        max_concurrent,
                        db=pool,
                        temporal=temporal,
                    )
                )
            assert exc_info.value.source == "postgres"
            assert exc_info.value.current == pg_count
        else:
            result = asyncio.run(
                check_dept_concurrency(
                    dept_id, max_concurrent, db=pool, temporal=temporal
                )
            )
            assert result.source == "postgres"
            assert result.current == pg_count

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        pg_count=st.integers(min_value=0, max_value=100),
        dept_id=_DEPT_ID,
    )
    def test_no_temporal_yields_postgres_source(
        self,
        max_concurrent: int,
        pg_count: int,
        dept_id: str,
    ) -> None:
        """When ``temporal`` is None, source is always 'postgres'."""
        pool = _FakePool(count=pg_count)
        # Cross-check via count_active_workflows directly.
        count, source = asyncio.run(
            count_active_workflows(
                dept_id=dept_id, db=pool, temporal=None
            )
        )
        assert count == pg_count
        assert source == "postgres"

        # Re-issue through check_dept_concurrency — must agree.
        if pg_count >= max_concurrent:
            with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
                asyncio.run(
                    check_dept_concurrency(
                        dept_id,
                        max_concurrent,
                        db=_FakePool(count=pg_count),
                        temporal=None,
                    )
                )
            assert exc_info.value.source == "postgres"
        else:
            result = asyncio.run(
                check_dept_concurrency(
                    dept_id,
                    max_concurrent,
                    db=_FakePool(count=pg_count),
                    temporal=None,
                )
            )
            assert result.source == "postgres"


# ---------------------------------------------------------------------------
# Property F — extract_max_concurrent rejects non-positive-int inputs
# ---------------------------------------------------------------------------


class TestExtractMaxConcurrentParsing:
    """**Property 17F: ``extract_max_concurrent`` is strict about types.**

    Only positive integers (excluding ``bool``) round-trip; everything
    else maps to ``None``. This guards the dispatcher's gate input —
    a corrupt config row must default to "no cap" rather than crash.
    """

    @_PROFILE
    @given(value=st.integers(min_value=1, max_value=10_000))
    def test_positive_int_roundtrip(self, value: int) -> None:
        assert extract_max_concurrent({"max_concurrent_workflows": value}) == value

    @_PROFILE
    @given(value=st.integers(max_value=0))
    def test_non_positive_int_returns_none(self, value: int) -> None:
        # ``0`` and any negative integer must be coerced to None.
        assert extract_max_concurrent({"max_concurrent_workflows": value}) is None

    @_PROFILE
    @given(value=st.booleans())
    def test_bool_returns_none(self, value: bool) -> None:
        # ``bool`` is an ``int`` subclass — the helper must reject it
        # explicitly (otherwise ``True`` would silently cap at 1).
        assert extract_max_concurrent({"max_concurrent_workflows": value}) is None

    @_PROFILE
    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    def test_float_returns_none(self, value: float) -> None:
        assert extract_max_concurrent({"max_concurrent_workflows": value}) is None

    @_PROFILE
    @given(value=st.text())
    def test_str_returns_none(self, value: str) -> None:
        assert extract_max_concurrent({"max_concurrent_workflows": value}) is None

    @_PROFILE
    @given(
        value=st.one_of(
            st.none(),
            st.lists(st.integers()),
            st.tuples(st.integers()),
            st.sets(st.integers()),
        )
    )
    def test_collections_and_none_return_none(self, value: Any) -> None:
        assert extract_max_concurrent({"max_concurrent_workflows": value}) is None

    def test_missing_key_returns_none(self) -> None:
        assert extract_max_concurrent({}) is None
        assert extract_max_concurrent({"some_other_key": 5}) is None

    @_PROFILE
    @given(value=st.one_of(st.text(), st.integers(), st.floats(allow_nan=False)))
    def test_non_dict_top_level_returns_none(self, value: Any) -> None:
        # Anything other than a ``dict`` must be treated as "no config"
        # so the gate defaults to "absent" (silent allow).
        assert extract_max_concurrent(value) is None

    def test_none_top_level_returns_none(self) -> None:
        assert extract_max_concurrent(None) is None


# ---------------------------------------------------------------------------
# Cross-property — combined (max, count) sweep
# ---------------------------------------------------------------------------


class TestCombinedAllowRejectSweep:
    """**Combined Property 17 (Q19): for every (max, count) pair, behaviour
    is exactly partitioned by ``current >= max``.**

    Validates: Requirements 19.1, 19.2.
    """

    @_PROFILE
    @given(
        max_concurrent=_MAX_INT,
        current_count=_COUNT_INT,
        dept_id=_DEPT_ID,
    )
    def test_partition_by_inequality(
        self,
        max_concurrent: int,
        current_count: int,
        dept_id: str,
    ) -> None:
        pool = _FakePool(count=current_count)

        if current_count >= max_concurrent:
            with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
                asyncio.run(
                    check_dept_concurrency(
                        dept_id,
                        max_concurrent,
                        db=pool,
                        temporal=None,
                    )
                )
            assert exc_info.value.current == current_count
            assert exc_info.value.max_allowed == max_concurrent
            assert exc_info.value.dept_id == dept_id
        else:
            result = asyncio.run(
                check_dept_concurrency(
                    dept_id, max_concurrent, db=pool, temporal=None
                )
            )
            assert result.current == current_count
            assert result.max_allowed == max_concurrent
            assert result.dept_id == dept_id
