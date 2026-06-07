"""Tests for AgentRunnerWorkflow saga compensation.

Saga compensation determinism and idempotence.

For any execution history ``H = [a_1, a_2, ..., a_k]`` of completed
side-effecting activities (recorded in workflow state via append-only
list) followed by a failure at activity ``a_{k+1}``, the saga
compensation function ``compensate(H)`` SHALL satisfy *all* of:

1. **Reverse order** - the inverse for ``a_k`` runs first, then
   ``a_{k-1}``, ..., then ``a_1``.
2. **Inverse-only** - only recorded actions whose name is present in the
   inverse table get an inverse call (read-only ops such as
   ``jira_get_issue`` have no inverse and are skipped).
3. **Idempotence** - ``compensate(compensate(H)) == compensate(H)`` in
   the sense that the second pass operates on an empty list (the
   workflow drops the history after compensation runs) and is a no-op,
   so the *total* multiset of inverse invocations across both passes is
   identical to the first pass alone.
4. **Determinism** - the order of inverse invocations is a pure
   function of ``H``; running compensation twice on the same input
   produces byte-identical invocation logs (no clock, no random).
5. **Empty history** - ``compensate([])`` is a no-op.

The activity inverses are mocked (synchronous instrumented callables)
so this suite does *not* require a Temporal worker, MinIO, Bitbucket,
or any other external dependency.

"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without first installing the worker package.
#
# The agent-runner-worker ships its source under ``src/`` and is consumed
# via ``sys.path`` injection (mirrors the unit-test pattern in
# ``tests/unit/test_artifact_activity.py``). We avoid importing
# ``temporalio`` or any activity module here because the helper under
# test is a pure function - no Temporal runtime is required.
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from workflows.compensation import (  # noqa: E402  - sys.path bootstrap above
    CompensableAction,
    compensate_actions_sync,
    plan_compensation,
)


# ---------------------------------------------------------------------------
# Domain fixtures: P0 inverse table
# ---------------------------------------------------------------------------

# The P0 saga has *two* compensable forward actions; every other
# recorded action has no inverse and must be skipped.
_FORWARD_ACTIONS_WITH_INVERSE: tuple[str, ...] = (
    "bitbucket_create_branch",
    "artifact_upload",
)

# Forward actions that may appear in the recorded history but MUST NOT
# trigger any inverse call (no rollback in P0).
_FORWARD_ACTIONS_WITHOUT_INVERSE: tuple[str, ...] = (
    "bitbucket_commit_via_git",
    "bitbucket_open_pr",
    "confluence_create_page",
    "confluence_update_page",
    "llm_generate_code",
    "llm_generate_doc",
    "llm_review_code",
    "llm_research",
    "jira_add_comment",
    "opencode_generate_code",
)

_ALL_FORWARD_ACTIONS: tuple[str, ...] = (
    _FORWARD_ACTIONS_WITH_INVERSE + _FORWARD_ACTIONS_WITHOUT_INVERSE
)


def _build_instrumented_inverse_table() -> tuple[
    dict[str, Callable[..., None]],
    list[tuple[str, tuple[Any, ...], Mapping[str, Any]]],
]:
    """Return ``(inverse_table, call_log)``.

    The inverse table maps each compensable forward action to a
    synchronous instrumented closure that appends a record to
    ``call_log`` on every invocation. Tests can then assert against the
    log without touching mock-library internals.
    """

    call_log: list[tuple[str, tuple[Any, ...], Mapping[str, Any]]] = []

    def _make_inverse(name: str) -> Callable[..., None]:
        def _inverse(*args: Any, **kwargs: Any) -> None:
            call_log.append((name, args, dict(kwargs)))

        return _inverse

    inverse_table = {
        name: _make_inverse(name) for name in _FORWARD_ACTIONS_WITH_INVERSE
    }
    return inverse_table, call_log


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Action *names* - drawn from the union of "has inverse" and "no inverse"
# pools so the property tests cover both branches of the compensation
# logic. We include a sprinkling of unknown names as well to ensure the
# helper silently skips anything the workflow forgets to register.
_UNKNOWN_NAMES = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz_"),
    min_size=1,
    max_size=20,
).filter(lambda s: s not in _ALL_FORWARD_ACTIONS)

_ACTION_NAMES = st.one_of(
    st.sampled_from(_ALL_FORWARD_ACTIONS),
    _UNKNOWN_NAMES,
)

# Inverse-call payloads - kept simple but heterogeneous so we cover the
# real-world shapes (``bitbucket_delete_branch(repo, branch, dept_id)``
# uses positional args; ``artifact_delete(bucket, key)`` likewise).
_PAYLOAD_SCALAR = st.one_of(
    st.text(max_size=40),
    st.integers(min_value=-(2**31), max_value=2**31 - 1),
    st.booleans(),
    st.none(),
)

_INVERSE_ARGS = st.lists(_PAYLOAD_SCALAR, max_size=4).map(tuple)
_INVERSE_KWARGS = st.dictionaries(
    keys=st.text(
        alphabet=st.sampled_from(
            "abcdefghijklmnopqrstuvwxyz_"
        ),
        min_size=1,
        max_size=8,
    ),
    values=_PAYLOAD_SCALAR,
    max_size=3,
)


@st.composite
def _compensable_actions(draw: st.DrawFn) -> CompensableAction:
    return CompensableAction(
        name=draw(_ACTION_NAMES),
        inverse_args=draw(_INVERSE_ARGS),
        inverse_kwargs=draw(_INVERSE_KWARGS),
    )


# Histories of length 0..16. We keep the upper bound modest because each
# example exercises the full plan + execute pipeline; Hypothesis's
# default search budget (~100 examples) provides plenty of coverage.
_HISTORIES = st.lists(_compensable_actions(), min_size=0, max_size=16)


# ---------------------------------------------------------------------------
# Reverse order
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(history=_HISTORIES)
def test_compensation_invokes_inverses_in_reverse_order(
    history: list[CompensableAction],
) -> None:
    """Inverses run in the reverse order of recording.

    For any randomly generated history ``H``, the sequence of inverse
    invocations equals ``[a for a in reversed(H) if a has inverse]``.
    """

    inverse_table, call_log = _build_instrumented_inverse_table()

    invocations = compensate_actions_sync(history, inverse_table)

    expected_names = [
        a.name for a in reversed(history) if a.name in inverse_table
    ]
    invoked_names = [name for (name, _args, _kwargs) in call_log]

    # The instrumented log and the helper's returned invocation list must
    # agree (cross-check that no inverse was lost or duplicated).
    assert invoked_names == [name for (name, _, _) in invocations]
    # And both must equal the expected reverse-filter projection.
    assert invoked_names == expected_names


# ---------------------------------------------------------------------------
# Inverse-only (no rollback for unrecorded ops)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(history=_HISTORIES)
def test_only_recorded_actions_with_inverse_get_called(
    history: list[CompensableAction],
) -> None:
    """No inverse is invoked for actions absent from the inverse table.

    The workflow records read-only ops (``jira_get_issue``) and ops
    without a P0 inverse (``bitbucket_commit_via_git``,
    ``confluence_create_page``, ...) in the same history list. The
    compensation helper MUST silently skip them.
    """

    inverse_table, call_log = _build_instrumented_inverse_table()

    compensate_actions_sync(history, inverse_table)

    # Every invoked name must be a key of the inverse table (i.e. one of
    # the P0 compensable actions). And every recorded action that *is*
    # compensable must appear exactly once in the call log.
    invoked_names = [name for (name, _, _) in call_log]
    for name in invoked_names:
        assert name in inverse_table, (
            f"unexpected inverse call for {name!r} - not in inverse table"
        )

    # Number of invocations equals number of compensable entries in H.
    expected_count = sum(1 for a in history if a.name in inverse_table)
    assert len(invoked_names) == expected_count


# ---------------------------------------------------------------------------
# Idempotence (re-running compensation is a no-op)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(history=_HISTORIES)
def test_compensation_is_idempotent(
    history: list[CompensableAction],
) -> None:
    """``compensate(compensate(H)) == compensate(H)``.

    After the first compensation pass, the workflow drops the recorded
    history (replaces it with the empty list). A second pass therefore
    operates on ``[]`` and is a no-op. The *total* invocation log
    across both passes must equal the first pass's log alone.
    """

    inverse_table, call_log = _build_instrumented_inverse_table()

    first_pass = compensate_actions_sync(history, inverse_table)
    log_after_first = list(call_log)

    # Workflow contract: history is consumed; second pass sees ``[]``.
    second_pass = compensate_actions_sync([], inverse_table)

    assert second_pass == []
    # The instrumented log MUST be unchanged by the second pass.
    assert call_log == log_after_first
    # And the first pass's invocation list must equal the log.
    assert first_pass == log_after_first


# ---------------------------------------------------------------------------
# Determinism (pure function of H)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(history=_HISTORIES)
def test_compensation_is_deterministic(
    history: list[CompensableAction],
) -> None:
    """Two independent runs over the same history produce identical logs.

    No random shuffling, no wallclock-dependent ordering, no hidden
    state may leak into the inverse-invocation order or argument
    forwarding.
    """

    inverse_table_a, log_a = _build_instrumented_inverse_table()
    inverse_table_b, log_b = _build_instrumented_inverse_table()

    inv_a = compensate_actions_sync(history, inverse_table_a)
    inv_b = compensate_actions_sync(history, inverse_table_b)

    assert inv_a == inv_b
    assert log_a == log_b

    # Also sanity-check the planner is itself deterministic.
    plan_a = plan_compensation(history, inverse_table_a)
    plan_b = plan_compensation(history, inverse_table_b)
    assert plan_a == plan_b


# ---------------------------------------------------------------------------
# Empty history is a no-op
# ---------------------------------------------------------------------------


def test_empty_history_is_a_noop() -> None:
    """``compensate([])`` invokes no inverse and returns ``[]``.

    Example-based: the property is unconditional so a single assertion
    suffices. Kept as its own test (rather than an extra Hypothesis
    case) so the failure mode is unambiguous in CI output.
    """

    inverse_table, call_log = _build_instrumented_inverse_table()

    invocations = compensate_actions_sync([], inverse_table)

    assert invocations == []
    assert call_log == []
    assert plan_compensation([], inverse_table) == []


# ---------------------------------------------------------------------------
# Anchor example: realistic code-change history
# ---------------------------------------------------------------------------


def test_realistic_code_change_history_compensates_in_p0_order() -> None:
    """Anchor example mirroring the ``code_change_with_test`` flow.

    Forward order:
        bitbucket_create_branch
        opencode_generate_code     (no inverse)
        bitbucket_commit_via_git   (no inverse)
        bitbucket_open_pr          (no inverse in P0)
        artifact_upload

    Compensation order (reverse, inverse-only):
        artifact_delete             artifact_upload
        bitbucket_delete_branch     bitbucket_create_branch
    """

    history = [
        CompensableAction(
            name="bitbucket_create_branch",
            inverse_args=(
                # repo, branch, dept_id - order matches activity signature
                {"workspace": "acme", "repo": "service-x"},
                "ai/PROJ-123/iter-1",
                "dept-eng",
            ),
            inverse_kwargs={},
        ),
        CompensableAction(name="opencode_generate_code"),
        CompensableAction(name="bitbucket_commit_via_git"),
        CompensableAction(name="bitbucket_open_pr"),
        CompensableAction(
            name="artifact_upload",
            inverse_args=("ai-runs", "artifacts/PROJ-123/iter-1/diff.patch"),
            inverse_kwargs={},
        ),
    ]

    inverse_table, call_log = _build_instrumented_inverse_table()
    compensate_actions_sync(history, inverse_table)

    assert [name for (name, _, _) in call_log] == [
        "artifact_upload",
        "bitbucket_create_branch",
    ]
    # And the inverse arguments are forwarded verbatim from the recorded
    # action to the inverse callable.
    assert call_log[0][1] == (
        "ai-runs",
        "artifacts/PROJ-123/iter-1/diff.patch",
    )
    assert call_log[1][1] == (
        {"workspace": "acme", "repo": "service-x"},
        "ai/PROJ-123/iter-1",
        "dept-eng",
    )


# ---------------------------------------------------------------------------
# Cross-check: history containing only no-inverse ops is a no-op
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    history=st.lists(
        st.builds(
            CompensableAction,
            name=st.sampled_from(_FORWARD_ACTIONS_WITHOUT_INVERSE),
            inverse_args=_INVERSE_ARGS,
            inverse_kwargs=_INVERSE_KWARGS,
        ),
        min_size=0,
        max_size=10,
    )
)
def test_history_with_only_non_compensable_actions_is_a_noop(
    history: list[CompensableAction],
) -> None:
    """A history made entirely of no-inverse actions triggers no calls.

    This is a stronger statement than the inverse-only case: even when every
    entry in ``H`` is a *recorded* action, if none of them have an
    inverse the compensation MUST be a complete no-op.
    """

    # Hypothesis is allowed to generate the empty list here; we exclude
    # it because the dedicated empty-history test already covers it and
    # it would otherwise duplicate that assertion.
    assume(len(history) > 0)

    inverse_table, call_log = _build_instrumented_inverse_table()

    invocations = compensate_actions_sync(history, inverse_table)

    assert invocations == []
    assert call_log == []


if __name__ == "__main__":  # pragma: no cover  - convenience entry point
    sys.exit(pytest.main([__file__, "-v"]))
