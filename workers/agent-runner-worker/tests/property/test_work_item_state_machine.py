"""Property tests for the ``automation.work_items`` status state machine.

**Property 9: work_item state-machine allowed edges**

**Validates: Requirements 5.10, 5.11, 11.5**

Per ``.kiro/specs/p0-critical-path/design.md`` §"Property 9", the
``automation.work_items.status`` column SHALL only ever transition along
edges of the directed graph ``G_status`` with vertex set
``{pending, running, completed, failed}`` and edge set:

    pending → running
    pending → failed
    running → completed
    running → failed

Plus self-loops (``s → s`` for every status, modelling idempotent
re-writes from retried activities).

Every other ordered pair is forbidden — most notably:

* ``pending → completed`` (must transit ``running`` first),
* any outgoing edge from a terminal state (``completed → *``,
  ``failed → *``),
* ``running → pending`` (the machine is monotone).

The function under test, :func:`validate_work_item_transition`, is a
pure helper extracted from the Temporal activity
:func:`update_work_item_status` so this property suite does *not* need
Postgres or a Temporal worker — the activity wraps the same validator
in a transactional ``UPDATE`` (see ``src/activities/work_item.py``).

The properties asserted below:

1. **Allowed edges** — every forward edge in the design's edge set is
   accepted by the validator (no false negatives).
2. **Self-loops** — every status transitions to itself without raising
   (idempotent write contract).
3. **Forbidden edges** — every other ordered pair drawn from the four
   valid statuses raises :class:`InvalidWorkItemTransition`.
4. **Sequence invariant** — for any randomly generated sequence of
   ``(from_status, to_status)`` pairs over the full 4×4 space, the
   validator's accept/reject decision agrees with the explicit
   allowed-edge predicate computed from the design specification.

Hypothesis sources the random ``(from, to)`` pairs from
``sampled_from(WORK_ITEM_STATUSES)`` so every example exercises an
input the schema's CHECK constraint would accept. We additionally
fuzz the validator with arbitrary text to confirm out-of-vocabulary
inputs are rejected as well (forward-compat: prevents future code from
quietly inserting a new status without updating the state machine).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without first installing the worker package.
#
# Mirrors the bootstrap pattern used in
# ``test_saga_compensation.py`` (sibling property test): the worker ships
# its source under ``src/`` and is consumed via ``sys.path`` injection.
# The imported module touches ``temporalio.activity`` at import time
# (the ``@activity.defn`` decorator) but does *not* require a Temporal
# runtime to load, so this is safe in a unit-test process.
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from activities.work_item import (  # noqa: E402  — sys.path bootstrap above
    WORK_ITEM_STATUSES,
    InvalidWorkItemTransition,
    is_valid_work_item_transition,
    validate_work_item_transition,
)


# ---------------------------------------------------------------------------
# Specification: the *exact* allowed-edge set from design.md §"Property 9"
# ---------------------------------------------------------------------------

#: Forward edges (excluding self-loops) — the four allowed non-trivial
#: transitions. Hard-coded here rather than imported from the
#: implementation so the test independently re-states the design
#: contract; if a future refactor accidentally relaxes the validator,
#: this set won't shift along with it and the property will fail.
_DESIGN_FORWARD_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        ("pending", "running"),
        ("pending", "failed"),
        ("running", "completed"),
        ("running", "failed"),
    }
)

#: All ordered pairs over the four canonical statuses. Used as the
#: sample space for the comprehensive sequence property below.
_ALL_STATUS_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (a, b) for a in sorted(WORK_ITEM_STATUSES) for b in sorted(WORK_ITEM_STATUSES)
)


def _is_allowed_edge_per_spec(from_status: str, to_status: str) -> bool:
    """Reference oracle that re-states the design contract verbatim.

    Returns ``True`` iff ``(from_status, to_status)`` is a self-loop on
    a valid status OR appears in :data:`_DESIGN_FORWARD_EDGES`.
    """

    if from_status not in WORK_ITEM_STATUSES:
        return False
    if to_status not in WORK_ITEM_STATUSES:
        return False
    if from_status == to_status:
        return True
    return (from_status, to_status) in _DESIGN_FORWARD_EDGES


# ---------------------------------------------------------------------------
# Sanity checks — these guard against regressions in the test fixtures
# themselves (so a bug in the spec mirror doesn't make the property pass
# vacuously).
# ---------------------------------------------------------------------------


def test_design_forward_edge_set_matches_documented_size() -> None:
    """The design enumerates exactly four forward (non-self-loop) edges.

    If a future spec revision adds or removes a transition, this guard
    surfaces the change at test-collection time rather than letting the
    property suite drift silently.
    """

    assert len(_DESIGN_FORWARD_EDGES) == 4
    assert WORK_ITEM_STATUSES == frozenset(
        {"pending", "running", "completed", "failed"}
    )


def test_all_status_pairs_covers_full_4x4_space() -> None:
    """The exhaustive pair enumeration must be the full 4 × 4 grid."""

    assert len(_ALL_STATUS_PAIRS) == 16


# ---------------------------------------------------------------------------
# Property 9.1 — every documented forward edge is accepted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    sorted(_DESIGN_FORWARD_EDGES),
)
def test_documented_forward_edges_are_accepted(
    from_status: str, to_status: str
) -> None:
    """Every edge enumerated in design §"Property 9" is accepted.

    The validator must not raise for any of:
        pending → running, pending → failed,
        running → completed, running → failed.
    """

    # Boolean predicate agrees with the spec.
    assert is_valid_work_item_transition(from_status, to_status) is True
    # And the raise-form is silent.
    validate_work_item_transition(from_status, to_status)


# ---------------------------------------------------------------------------
# Property 9.2 — every status accepts a self-loop (idempotent write)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", sorted(WORK_ITEM_STATUSES))
def test_self_loop_is_accepted_for_every_status(status: str) -> None:
    """``s → s`` is an allowed transition for every valid ``s``.

    Models the idempotent re-write contract: if a Temporal activity
    is retried after the row has already been advanced, re-issuing the
    same status MUST succeed without raising.
    """

    assert is_valid_work_item_transition(status, status) is True
    validate_work_item_transition(status, status)


# ---------------------------------------------------------------------------
# Property 9.3 — every other pair over the canonical status set is rejected
# ---------------------------------------------------------------------------


# All ordered pairs that are NOT self-loops and NOT in the forward edge
# set. We compute this once at module load so pytest can parametrise
# over the explicit list and report which forbidden pair, if any, was
# accidentally accepted.
_FORBIDDEN_STATUS_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (a, b)
    for (a, b) in _ALL_STATUS_PAIRS
    if a != b and (a, b) not in _DESIGN_FORWARD_EDGES
)


def test_forbidden_pair_set_has_expected_size() -> None:
    """The forbidden-pair list covers the full complement of the allowed set.

    16 total pairs - 4 self-loops - 4 forward edges = 8 forbidden.
    """

    assert len(_FORBIDDEN_STATUS_PAIRS) == 8


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    _FORBIDDEN_STATUS_PAIRS,
)
def test_forbidden_pairs_raise_invalid_work_item_transition(
    from_status: str, to_status: str
) -> None:
    """Every non-allowed pair from the canonical 4-status grid is rejected.

    Notable inclusions:
        - ``pending → completed`` (must transit ``running``),
        - ``completed → *`` and ``failed → *`` (terminal states),
        - ``running → pending`` (no backward transitions).
    """

    assert is_valid_work_item_transition(from_status, to_status) is False
    with pytest.raises(InvalidWorkItemTransition) as exc_info:
        validate_work_item_transition(from_status, to_status)

    # The exception MUST carry the offending pair verbatim so
    # downstream audit logs can reconstruct the violation.
    assert exc_info.value.from_status == from_status
    assert exc_info.value.to_status == to_status


# ---------------------------------------------------------------------------
# Property 9.4 — Hypothesis-driven sequence test over the 4×4 status grid
# ---------------------------------------------------------------------------

#: Hypothesis strategy that draws statuses from the canonical four-value
#: set. Mirrors the database CHECK constraint's vocabulary.
_STATUS_STRATEGY = st.sampled_from(sorted(WORK_ITEM_STATUSES))


@settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    transitions=st.lists(
        st.tuples(_STATUS_STRATEGY, _STATUS_STRATEGY),
        min_size=0,
        max_size=24,
    ),
)
def test_validator_agrees_with_spec_oracle_on_random_sequences(
    transitions: list[tuple[str, str]],
) -> None:
    """For any random sequence of ``(from, to)`` pairs the validator
    matches the spec oracle on every step.

    This is the headline property: it asserts that across arbitrary
    sequences (including empty), the implementation's accept/reject
    decision tracks the design's allowed-edge set exactly. Equivalent
    to ∀ pair ∈ STATUSES × STATUSES the validator decides as the spec
    dictates, but generated as a *sequence* so Hypothesis can shrink to
    the minimal failing subsequence if it ever drifts.
    """

    for from_status, to_status in transitions:
        spec_allows = _is_allowed_edge_per_spec(from_status, to_status)
        impl_allows = is_valid_work_item_transition(from_status, to_status)

        assert impl_allows == spec_allows, (
            f"validator disagrees with design spec at "
            f"({from_status!r} -> {to_status!r}): "
            f"spec={spec_allows}, impl={impl_allows}"
        )

        if spec_allows:
            # validate_*_transition must NOT raise on allowed edges.
            validate_work_item_transition(from_status, to_status)
        else:
            with pytest.raises(InvalidWorkItemTransition) as exc_info:
                validate_work_item_transition(from_status, to_status)
            assert exc_info.value.from_status == from_status
            assert exc_info.value.to_status == to_status


# ---------------------------------------------------------------------------
# Property 9.5 — out-of-vocabulary inputs are rejected
# ---------------------------------------------------------------------------


# A text strategy that is *guaranteed* to draw values outside the
# canonical four-status vocabulary. Excludes empties so we still cover a
# realistic "looks like a status but isn't" surface.
_OUT_OF_VOCAB_STATUS = st.text(
    alphabet=st.characters(min_codepoint=0x21, max_codepoint=0x7E),
    min_size=1,
    max_size=20,
).filter(lambda s: s not in WORK_ITEM_STATUSES)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    from_status=st.one_of(_STATUS_STRATEGY, _OUT_OF_VOCAB_STATUS),
    to_status=_OUT_OF_VOCAB_STATUS,
)
def test_unknown_target_status_is_always_rejected(
    from_status: str, to_status: str
) -> None:
    """Any ``to_status`` outside the canonical set is rejected.

    Forward-compat guard: a future code change that accidentally writes
    a new status (e.g. ``"cancelled"``) MUST be caught by the same
    validator before the database CHECK constraint sees the value.
    """

    assert is_valid_work_item_transition(from_status, to_status) is False
    with pytest.raises(InvalidWorkItemTransition):
        validate_work_item_transition(from_status, to_status)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    from_status=_OUT_OF_VOCAB_STATUS,
    to_status=st.one_of(_STATUS_STRATEGY, _OUT_OF_VOCAB_STATUS),
)
def test_unknown_source_status_is_always_rejected(
    from_status: str, to_status: str
) -> None:
    """Any ``from_status`` outside the canonical set is rejected.

    Defends against a corrupt row sneaking past the schema CHECK and
    being read by the activity — even if Postgres somehow returned an
    unknown status, the validator MUST refuse to write a new one.
    """

    assert is_valid_work_item_transition(from_status, to_status) is False
    with pytest.raises(InvalidWorkItemTransition):
        validate_work_item_transition(from_status, to_status)


# ---------------------------------------------------------------------------
# Anchor example — the canonical happy-path lifecycle
# ---------------------------------------------------------------------------


def test_canonical_lifecycle_pending_running_completed_is_accepted() -> None:
    """The happy-path sequence ``pending → running → completed`` is allowed.

    Mirrors Requirement 11.5: "The Platform SHALL maintain the work_item
    record with accurate status transitions: pending → running →
    completed/failed."
    """

    validate_work_item_transition("pending", "running")
    validate_work_item_transition("running", "completed")


def test_canonical_failure_lifecycle_pending_running_failed_is_accepted() -> None:
    """The failure-path sequence ``pending → running → failed`` is allowed."""

    validate_work_item_transition("pending", "running")
    validate_work_item_transition("running", "failed")


def test_pending_to_completed_shortcut_is_rejected() -> None:
    """``pending → completed`` MUST be rejected (must transit ``running``).

    Anchor example for Property 9 — the most common temptation when a
    workflow short-circuits a no-op task. The state machine forbids this
    so the operator audit log always shows a ``running`` interval.
    """

    with pytest.raises(InvalidWorkItemTransition):
        validate_work_item_transition("pending", "completed")


def test_terminal_states_have_no_outgoing_edges() -> None:
    """No transition out of ``completed`` or ``failed`` (other than self-loop)."""

    for terminal in ("completed", "failed"):
        for target in WORK_ITEM_STATUSES - {terminal}:
            with pytest.raises(InvalidWorkItemTransition):
                validate_work_item_transition(terminal, target)


if __name__ == "__main__":  # pragma: no cover  — convenience entry point
    sys.exit(pytest.main([__file__, "-v"]))
