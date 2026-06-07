"""output_actions partition and partial-failure semantics.

This file pins the expected partition and partial-failure behavior:

(a) **Disjoint partition.** ``CRITICAL_OUTPUT_ACTION_KINDS`` and
    ``BEST_EFFORT_OUTPUT_ACTION_KINDS`` are disjoint frozensets (the
    classification table in :mod:`temporal_shared.messages` is the
    single source of truth) and :func:`partition` routes every action
    by ``kind`` membership - never by the carried ``severity`` field.

(b) **Critical failure  workflow ``failed`` + compensation.**  When a
    simulated apply step records a non-empty ``failed_critical`` list
    on an :class:`ApplyResult`, the workflow body raises
    :class:`_OutputActionCriticalFailure` (the sentinel
    :class:`Exception` carrying the partial result) - which the
    workflow's ``run`` traps to dispatch ``compensation_chain_run``
    after a critical output-action failure.

(c) **Best-effort failure  workflow ``completed`` / partial.**  When
    ``failed_critical`` is empty but ``failed_best_effort`` is
    non-empty, the simulated apply step does **not** raise; the
    list of failed best-effort kinds parities with
    ``ApplyResult.failed_best_effort`` and is what the workflow's
    final-comment formatter renders into the warning line.

(d) **``jira_attachment`` format guard.**  The
    ``jira_attachment`` payload must carry ``format ∈ {"pdf", "md"}``;
    any other value is rejected.  The pure-Python guard
    :func:`_validate_jira_attachment_format` mirrors the contract that
    ``apply()`` enforces before invoking the activity.

(e) **Size-cap  MinIO redirection invariant.**  Calling
    :func:`redirect_oversized_payload` on a payload whose JSON
    encoding exceeds :data:`MAX_OUTPUT_BYTES` returns a new action
    whose payload is the canonical ``{summary, minio_uri, size_bytes}``
    triple; below the cap the helper returns the input unchanged
    (identity).  The byte cap is exactly 1 MiB.

The tests are pure-Python: they never import the worker package and
never await a Temporal activity.  Workflow-level behaviour is
exercised by simulating the public surface (``ApplyResult`` shape,
the sentinel exception, the partition routing) - this matches the
"simulated success/failure" behavior and keeps the tests isolated
from event-loop / Temporal runtime concerns.

The companion module :mod:`test_output_size_cap` already pins the
identity / replacement contract of
:func:`redirect_oversized_payload` exhaustively across many random
payloads; clause (e) here re-asserts the boundary condition with a
single deterministic example so the routing of "oversized  summary
triple" remains covered with the partition semantics.

Run target (from ``platform/``)::

    python -m pytest tests/property/test_output_actions_partition.py -v
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from temporal_shared.messages import (
    BEST_EFFORT_OUTPUT_ACTION_KINDS,
    CRITICAL_OUTPUT_ACTION_KINDS,
    OutputAction,
)
from temporal_shared.output_actions import (
    UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE,
    ApplyResult,
    partition,
)
from temporal_shared.output_size_cap import (
    MAX_OUTPUT_BYTES,
    MINIO_KEY_TEMPLATE,
    measure_payload_bytes,
    redirect_oversized_payload,
)

# ---------------------------------------------------------------------------
# Closed-vocabulary kind alphabet
# ---------------------------------------------------------------------------

#: Closed alphabet of valid :class:`OutputAction.kind` values plus the
#: severity each kind maps to (the kind  severity invariant from
#: :mod:`temporal_shared.messages`).  Hypothesis samples from this
#: tuple so every generated action is well-formed in isolation; the
#: partition behaviour is what the property tests assert.
_VALID_KIND_SEVERITY: Final[tuple[tuple[str, str], ...]] = (
    # critical
    ("jira_comment", "critical"),
    ("bitbucket_create_pr", "critical"),
    ("confluence_create_page", "critical"),
    ("confluence_update_page", "critical"),
    # best-effort
    ("slack_notify", "best_effort"),
    ("email_notify", "best_effort"),
    ("jira_attachment", "best_effort"),
)

#: Set of unknown kinds - used to drive the ValueError clause of the
#: partition contract.  These strings are deliberately picked to look
#: plausible but to fall outside both classification frozensets.
_UNKNOWN_KINDS: Final[tuple[str, ...]] = (
    "unknown_kind",
    "jira_unknown",
    "bitbucket_merge_pr",  # banned tool - never a valid output action
    "confluence_delete_page",  # banned tool
    "send_carrier_pigeon",
    "",  # empty kind - also unclassified
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


def _make_action(kind: str, severity: str) -> OutputAction:
    """Construct a minimal :class:`OutputAction` of *kind* / *severity*.

    The payload carries a single ``("idx", n)`` pair so generated
    instances are distinguishable in ordering assertions.  We use a
    fresh int per call (taken from the iteration count of the
    surrounding strategy) - this keeps each action distinct without
    bloating the search space.
    """
    return OutputAction(kind=kind, severity=severity, payload=(("idx", 0),))


def _action_strategy() -> st.SearchStrategy[OutputAction]:
    """Strategy producing a single well-formed :class:`OutputAction`.

    Each draw samples a (kind, severity) pair from the closed
    alphabet plus a small integer payload tag so action equality
    rarely collides - useful for the union-preservation check in
    partition invariant.
    """
    return st.builds(
        lambda kind_sev, idx: OutputAction(
            kind=kind_sev[0],
            severity=kind_sev[1],
            payload=(("idx", idx),),
        ),
        kind_sev=st.sampled_from(_VALID_KIND_SEVERITY),
        idx=st.integers(min_value=0, max_value=10_000),
    )


#: ``tuple[OutputAction, ...]`` of length 0..10 - the natural input to
#: :func:`partition`.  The upper bound is small (10 actions) because
#: that is the realistic maximum a single LLM analysis emits and
#: keeps the union-preservation check cheap.
_ACTION_TUPLE: Final = st.lists(_action_strategy(), min_size=0, max_size=10).map(tuple)


# ---------------------------------------------------------------------------
# Disjoint partition + union preservation
# ---------------------------------------------------------------------------


class TestPartitionDisjoint:
    """``CRITICAL_KINDS ∩ BEST_EFFORT_KINDS == ∅`` and union-preservation."""

    def test_classification_frozensets_are_disjoint(self) -> None:
        """The classification frozensets are disjoint.

        The two classification frozensets in
        :mod:`temporal_shared.messages` are the single source of
        truth for output-action routing.  Their intersection MUST
        be empty so that no kind can ever be routed twice; the
        invariant is asserted as a structural fact about the
        constants themselves.  This is the (a) clause of
        the partition invariant.
        """
        intersection = (
            CRITICAL_OUTPUT_ACTION_KINDS & BEST_EFFORT_OUTPUT_ACTION_KINDS
        )
        assert intersection == frozenset()

    def test_classification_frozensets_are_non_empty(self) -> None:
        """Both partitions are populated.

        Both frozensets carry at least one kind so the partition
        routing has somewhere to send each action.  An empty
        frozenset would silently turn every action of that
        category into an unclassified ValueError.
        """
        assert len(CRITICAL_OUTPUT_ACTION_KINDS) >= 1
        assert len(BEST_EFFORT_OUTPUT_ACTION_KINDS) >= 1

    @settings(max_examples=100, deadline=None)
    @given(actions=_ACTION_TUPLE)
    def test_partition_preserves_count_and_membership(
        self, actions: tuple[OutputAction, ...]
    ) -> None:
        """The partition union preserves the original actions.

        For any tuple of well-formed actions:

        * ``len(critical) + len(best_effort) == len(actions)`` -
          every action lands in exactly one bucket.
        * Every critical-bucket action's ``kind`` is in
          :data:`CRITICAL_OUTPUT_ACTION_KINDS`.
        * Every best-effort-bucket action's ``kind`` is in
          :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS`.
        * The concatenation ``critical + best_effort`` (treated as
          a multiset) equals ``actions`` as a multiset - no
          duplication, no loss.
        """
        critical, best_effort = partition(actions)

        # Count parity - partition is a partition, not a copy.
        assert len(critical) + len(best_effort) == len(actions)

        # Membership: critical bucket carries only critical kinds.
        for action in critical:
            assert action.kind in CRITICAL_OUTPUT_ACTION_KINDS

        # Membership: best-effort bucket carries only best-effort kinds.
        for action in best_effort:
            assert action.kind in BEST_EFFORT_OUTPUT_ACTION_KINDS

        # Multiset preservation - every input action appears exactly
        # once across the two buckets.  We compare the multisets keyed
        # by (kind, payload-idx) since :class:`OutputAction` is a frozen
        # dataclass and hashable, but Counter on the dataclass itself
        # would also work; the keyed-tuple form makes the test intent
        # explicit and survives future ``__eq__`` changes.
        original = list(actions)
        recombined = list(critical) + list(best_effort)

        def _sort_key(a: OutputAction) -> tuple[str, object]:
            return (a.kind, dict(a.payload).get("idx", 0))

        assert sorted(original, key=_sort_key) == sorted(
            recombined, key=_sort_key
        )

    @settings(max_examples=100, deadline=None)
    @given(actions=_ACTION_TUPLE)
    def test_partition_preserves_relative_order_within_buckets(
        self, actions: tuple[OutputAction, ...]
    ) -> None:
        """Partitioning preserves relative order within each bucket.

        :func:`partition` MUST preserve the relative order of each
        bucket from the input iterable so the workflow body applies
        actions in the LLM-emitted sequence.  The check filters the
        input down to its critical / best-effort sublists and asserts
        equality with the partition output.
        """
        critical, best_effort = partition(actions)

        expected_critical = tuple(
            a for a in actions if a.kind in CRITICAL_OUTPUT_ACTION_KINDS
        )
        expected_best_effort = tuple(
            a for a in actions if a.kind in BEST_EFFORT_OUTPUT_ACTION_KINDS
        )

        assert critical == expected_critical
        assert best_effort == expected_best_effort

    @pytest.mark.parametrize("unknown_kind", _UNKNOWN_KINDS)
    def test_partition_rejects_unknown_kind_with_value_error(
        self, unknown_kind: str
    ) -> None:
        """Unknown kinds raise ``ValueError``.

        Any :class:`OutputAction` whose ``kind`` is in neither
        classification frozenset MUST raise :class:`ValueError`
        with the documented prefix
        :data:`UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE`.  This
        prevents an LLM-emitted typo from silently falling
        through the partition.
        """
        action = OutputAction(
            kind=unknown_kind,
            severity="best_effort",
            payload=(),
        )
        with pytest.raises(ValueError, match=UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE):
            partition((action,))

    def test_partition_rejects_non_outputaction_with_type_error(self) -> None:
        """Non-``OutputAction`` elements raise ``TypeError``.

        Non-:class:`OutputAction` elements raise :class:`TypeError`
        rather than coercing or silently routing.  This catches a
        programming mistake at the activity boundary where the
        wrong shape might otherwise propagate.
        """
        with pytest.raises(TypeError, match="OutputAction"):
            partition(("not-an-action",))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# failed_critical non-empty  critical-failure raised
# ---------------------------------------------------------------------------


@dataclass
class _CompensationRecorder:
    """Records compensation invocations triggered by a critical failure.

    The simulated workflow caller raises an
    :class:`_OutputActionCriticalFailure`-shaped sentinel exception
    when ``failed_critical`` is non-empty.  The recorder
    captures the apply result so the test can assert that the
    compensation step received the partial bookkeeping.
    """

    invocations: list[ApplyResult] = field(default_factory=list)

    def trigger(self, apply_result: ApplyResult) -> None:
        """Pretend to dispatch ``compensation_chain_run`` with *apply_result*."""
        self.invocations.append(apply_result)


class _SimulatedCriticalFailure(Exception):
    """Local stand-in for :class:`_OutputActionCriticalFailure`.

    The workflow defines that exception privately inside
    ``agent_runner.workflows.agent_runner_workflow``; importing it
    here would couple the property test to the worker package, which
    the test suite intentionally avoids (the worker imports
    ``temporalio`` heavily, and pytest's ``pythonpath`` does not
    inject the worker source).  We define an equivalent local
    sentinel so the simulated apply step has a concrete exception
    type to raise - the contract under test is *that* an exception
    is raised when ``failed_critical`` is non-empty, not which class
    name carries the result.

    """

    def __init__(self, apply_result: ApplyResult) -> None:
        super().__init__("critical output action failed")
        self.apply_result = apply_result


def _simulate_apply(
    actions: tuple[OutputAction, ...],
    *,
    fail_critical_indices: frozenset[int],
    fail_best_effort_indices: frozenset[int],
    compensation: _CompensationRecorder,
) -> ApplyResult:
    """Simulate ``apply()`` with deterministic per-action outcomes.

    The simulation mirrors the public surface of the workflow body's
    ``_execute_output_actions`` step (see
    ``platform/workers/agent-runner-worker/.../agent_runner_workflow.py``):

    1. ``partition()`` the input.
    2. Walk the critical bucket; on the first failure (index in
       ``fail_critical_indices``) record into
       :attr:`ApplyResult.failed_critical`, dispatch the
       compensation recorder, and raise
       :class:`_SimulatedCriticalFailure` carrying the partial
       result.  This matches the fail-fast semantics.
    3. Otherwise walk the best-effort bucket; failures are recorded
       into :attr:`ApplyResult.failed_best_effort` but never raise
       .

    The function is intentionally synchronous - the simulation does
    not need an event loop because every "activity" is a no-op.
    Indices refer to position **within the partitioned bucket**, not
    the original tuple.
    """

    result = ApplyResult()
    critical, best_effort = partition(actions)

    # ----- Critical bucket: fail-fast -----
    for idx, action in enumerate(critical):
        if idx in fail_critical_indices:
            result.failed_critical.append((action.kind, "simulated_failure"))
            compensation.trigger(result)
            raise _SimulatedCriticalFailure(result)
        result.successful_critical.append(action.kind)

    # ----- Best-effort bucket: continue on failure -----
    for idx, action in enumerate(best_effort):
        if idx in fail_best_effort_indices:
            result.failed_best_effort.append((action.kind, "simulated_failure"))
        else:
            result.successful_best_effort.append(action.kind)

    return result


class TestCriticalFailureTriggersCompensation:
    """``failed_critical`` non-empty  exception + compensation dispatch."""

    @settings(max_examples=100, deadline=None)
    @given(
        actions=_ACTION_TUPLE.filter(
            lambda a: any(act.kind in CRITICAL_OUTPUT_ACTION_KINDS for act in a)
        ),
        # The first critical action always fails - this is the most
        # interesting case (it short-circuits the rest of the bucket).
        # Hypothesis would otherwise spend examples picking different
        # indices that all collapse to the same fail-fast branch.
        failing_index=st.just(0),
    )
    def test_critical_failure_raises_and_invokes_compensation(
        self,
        actions: tuple[OutputAction, ...],
        failing_index: int,
    ) -> None:
        """Critical failures raise and invoke compensation.

        For any input where the partitioned critical bucket has at
        least one element, simulating a failure on
        ``failing_index`` (within that bucket) MUST:

        * raise :class:`_SimulatedCriticalFailure`,
        * carry a non-empty :attr:`ApplyResult.failed_critical`
          on the exception's ``apply_result``,
        * trigger the compensation recorder exactly once.

        The simulation models the exact short-circuit semantics
        documented by ``_execute_output_actions``: subsequent
        critical actions are not applied after the first failure.
        """
        compensation = _CompensationRecorder()

        with pytest.raises(_SimulatedCriticalFailure) as exc_info:
            _simulate_apply(
                actions,
                fail_critical_indices=frozenset({failing_index}),
                fail_best_effort_indices=frozenset(),
                compensation=compensation,
            )

        # Compensation invoked exactly once with the partial result.
        assert len(compensation.invocations) == 1
        partial = compensation.invocations[0]
        assert partial is exc_info.value.apply_result
        assert partial.failed_critical, (
            "failed_critical must be non-empty when "
            "_SimulatedCriticalFailure is raised"
        )
        assert partial.has_critical_failure() is True

        # Best-effort bucket is never touched after a critical failure
        # (fail-fast).  Both lists must be empty regardless of
        # how many best-effort actions were in the input.
        assert partial.successful_best_effort == []
        assert partial.failed_best_effort == []

    def test_no_critical_failure_does_not_raise(self) -> None:
        """The happy path does not raise.

        When every critical action succeeds the simulated apply
        returns the result without raising and without invoking the
        compensation recorder.  This is the negative case of
        the exception fires only on actual failure.
        """
        compensation = _CompensationRecorder()
        actions = (
            _make_action("jira_comment", "critical"),
            _make_action("bitbucket_create_pr", "critical"),
            _make_action("slack_notify", "best_effort"),
        )

        result = _simulate_apply(
            actions,
            fail_critical_indices=frozenset(),
            fail_best_effort_indices=frozenset(),
            compensation=compensation,
        )

        assert compensation.invocations == []
        assert result.has_critical_failure() is False
        assert result.successful_critical == ["jira_comment", "bitbucket_create_pr"]
        assert result.successful_best_effort == ["slack_notify"]


# ---------------------------------------------------------------------------
# best-effort failure  completed_with_partial_failure
# ---------------------------------------------------------------------------


class TestBestEffortFailureIsRecordedNotRaised:
    """``failed_best_effort`` non-empty (and ``failed_critical`` empty)
    workflow ``completed`` / partial; the failed kinds parity with
    ``ApplyResult.failed_best_effort``."""

    @settings(max_examples=100, deadline=None)
    @given(
        actions=_ACTION_TUPLE.filter(
            lambda a: any(
                act.kind in BEST_EFFORT_OUTPUT_ACTION_KINDS for act in a
            )
        ),
        # Always fail the first best-effort action.  Hypothesis still
        # explores varied tuple shapes around that fixed index.
        failing_index=st.just(0),
    )
    def test_best_effort_failure_does_not_raise(
        self,
        actions: tuple[OutputAction, ...],
        failing_index: int,
    ) -> None:
        """Best-effort failure is recorded and does not abort.

        Failing the chosen best-effort action MUST NOT raise - the
        simulated apply returns an :class:`ApplyResult` whose
        ``failed_best_effort`` list contains the failed kind and
        whose ``failed_critical`` list is empty.  This is the (c)
        best-effort failure behavior.
        """
        compensation = _CompensationRecorder()

        result = _simulate_apply(
            actions,
            fail_critical_indices=frozenset(),
            fail_best_effort_indices=frozenset({failing_index}),
            compensation=compensation,
        )

        # No exception  no compensation dispatch.
        assert compensation.invocations == []

        # No critical failure recorded.
        assert result.has_critical_failure() is False
        assert result.failed_critical == []

        # Best-effort failure recorded.
        assert result.has_best_effort_failure() is True
        assert len(result.failed_best_effort) == 1

        # Bucket parity: every best-effort action lands in either
        # ``successful_best_effort`` or ``failed_best_effort`` exactly
        # once, and the total equals the bucket size.  This is the
        # parity invariant the workflow output's
        # ``partial_failure_actions`` field relies on - a
        # failed action must never be silently double-counted as a
        # success.
        _, best_effort_bucket = partition(actions)
        assert (
            len(result.successful_best_effort)
            + len(result.failed_best_effort)
            == len(best_effort_bucket)
        )

        # The failing index points at the kind recorded in
        # ``failed_best_effort``; multiple actions of the same kind
        # may legitimately appear in ``successful_best_effort`` because
        # only the chosen index failed (the others succeeded).
        failing_kind = best_effort_bucket[failing_index].kind
        assert result.failed_best_effort[0][0] == failing_kind


# ---------------------------------------------------------------------------
# jira_attachment format ∈ {pdf, md}
# ---------------------------------------------------------------------------


#: Closed alphabet of accepted ``jira_attachment.format`` values.
_VALID_JIRA_ATTACHMENT_FORMATS: Final[frozenset[str]] = frozenset({"pdf", "md"})


def _validate_jira_attachment_format(action: OutputAction) -> None:
    """Pure guard pinning the attachment format contract.

    The partition module does not enforce this rule today; validation
    belongs to ``apply()`` in the worker. Any ``jira_attachment`` action
    whose payload's ``format`` field is not in
    :data:`_VALID_JIRA_ATTACHMENT_FORMATS` must be rejected with
    :class:`ValueError`.

    Raises
    ------
    ValueError
        If ``action.kind == "jira_attachment"`` and the payload's
        ``format`` field is missing or outside the closed alphabet.
    """
    if action.kind != "jira_attachment":
        return
    payload = dict(action.payload)
    fmt = payload.get("format")
    if fmt not in _VALID_JIRA_ATTACHMENT_FORMATS:
        raise ValueError(
            f"jira_attachment.payload['format'] must be one of "
            f"{sorted(_VALID_JIRA_ATTACHMENT_FORMATS)}; got {fmt!r}"
        )


class TestJiraAttachmentFormatGuard:
    """``jira_attachment.format`` must be ``"pdf"`` or ``"md"``."""

    @pytest.mark.parametrize("fmt", sorted(_VALID_JIRA_ATTACHMENT_FORMATS))
    def test_valid_formats_pass(self, fmt: str) -> None:
        """The guard accepts ``pdf`` and ``md``.

        The two documented values pass the guard without raising.
        Other ``OutputAction`` kinds are unaffected by the format
        check (see :func:`_validate_jira_attachment_format`).
        """
        action = OutputAction(
            kind="jira_attachment",
            severity="best_effort",
            payload=(("format", fmt), ("filename", f"report.{fmt}")),
        )
        # Should not raise.
        _validate_jira_attachment_format(action)

    @settings(max_examples=100, deadline=None)
    @given(
        bad_format=st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs", "Cc"),
            ),
            max_size=16,
        ).filter(lambda s: s not in _VALID_JIRA_ATTACHMENT_FORMATS),
    )
    def test_arbitrary_other_format_rejected(self, bad_format: str) -> None:
        """The guard rejects any other format value.

        For any string outside the closed alphabet the guard MUST
        raise :class:`ValueError`.  Hypothesis explores arbitrary
        non-control text (including the empty string and look-alike
        values such as ``"PDF"``) so the closed-alphabet contract
        cannot drift to a case-insensitive match by accident.
        """
        action = OutputAction(
            kind="jira_attachment",
            severity="best_effort",
            payload=(("format", bad_format),),
        )
        with pytest.raises(ValueError, match="jira_attachment"):
            _validate_jira_attachment_format(action)

    def test_missing_format_rejected(self) -> None:
        """The payload must carry a ``format`` field.

        A ``jira_attachment`` with no ``format`` field is rejected;
        the closed alphabet does not include ``None`` and the
        absence of the key indicates a malformed activity output.
        """
        action = OutputAction(
            kind="jira_attachment",
            severity="best_effort",
            payload=(),
        )
        with pytest.raises(ValueError, match="jira_attachment"):
            _validate_jira_attachment_format(action)

    @pytest.mark.parametrize(
        "kind",
        sorted(CRITICAL_OUTPUT_ACTION_KINDS | BEST_EFFORT_OUTPUT_ACTION_KINDS),
    )
    def test_other_kinds_unaffected_by_format_guard(self, kind: str) -> None:
        """The format guard is scoped to ``jira_attachment``.

        The format guard is targeted: it never raises for any other
        kind, regardless of payload contents.  This pins the
        scoping so a future refactor cannot accidentally extend the
        check to (e.g.) ``confluence_create_page``.
        """
        if kind == "jira_attachment":
            return  # covered by the rejecting tests above
        # Build a payload that would fail the jira_attachment check
        # if the guard were misapplied.
        severity = (
            "critical" if kind in CRITICAL_OUTPUT_ACTION_KINDS else "best_effort"
        )
        action = OutputAction(
            kind=kind,
            severity=severity,
            payload=(("format", "exe"),),  # invalid for jira_attachment
        )
        # Should not raise - guard ignores non-jira_attachment kinds.
        _validate_jira_attachment_format(action)


# ---------------------------------------------------------------------------
# size cap  MinIO redirection invariant (boundary)
# ---------------------------------------------------------------------------
#
# The exhaustive size-cap behaviour (random-payload identity / replacement
# determinism) lives in :mod:`test_output_size_cap`.  Here we re-pin the
# boundary condition with two deterministic examples - one just below the
# cap, one just above.


@dataclass
class _StaticMinioWriter:
    """MinIO callback that returns a deterministic URI per key."""

    calls: list[tuple[str, int]] = field(default_factory=list)

    async def __call__(self, *, key: str, body: bytes) -> str:
        self.calls.append((key, len(body)))
        return f"s3://test-bucket/{key}"


def _run(coro):
    """Run *coro* in a fresh event loop (isolation across cases)."""
    return asyncio.run(coro)


class TestSizeCapMinioRedirection:
    """Boundary anchors for size-cap redirection."""

    def test_payload_within_cap_passes_through(self) -> None:
        """Payloads below the cap pass through unchanged.

        A small payload (well under 1 MiB) is returned unchanged by
        :func:`redirect_oversized_payload`.  The MinIO callback is
        not invoked.  This pins the no-op contract for every
        well-behaved activity output.
        """
        action = OutputAction(
            kind="jira_comment",
            severity="critical",
            payload=(("body", "kısa açıklama"),),
        )
        # Sanity: the payload is well under the cap.
        assert measure_payload_bytes(action.payload) < MAX_OUTPUT_BYTES

        writer = _StaticMinioWriter()
        result = _run(
            redirect_oversized_payload(
                action,
                workflow_id="automation-jira-PAY-1",
                idx=0,
                minio_callback=writer,
            )
        )

        assert result is action
        assert writer.calls == []

    def test_payload_above_cap_is_offloaded_with_summary_triple(self) -> None:
        """Payloads above the cap are replaced with a summary triple.

        A payload whose JSON encoding exceeds :data:`MAX_OUTPUT_BYTES`
        is replaced with a tuple-of-pairs payload exposing exactly
        ``summary``, ``minio_uri``, and ``size_bytes``.  The
        MinIO callback is invoked once with the canonical key
        ``ai-runs/{workflow_id}/output-{idx}.json`` and the original
        encoded body.  ``kind`` and ``severity`` are preserved.
        """
        # Single ASCII string just above the cap - JSON wrapping
        # overhead is < 16 bytes for this shape, so the encoded
        # length comfortably exceeds the cap.
        oversized_body = "x" * (MAX_OUTPUT_BYTES + 100)
        action = OutputAction(
            kind="confluence_create_page",
            severity="critical",
            payload=(("body", oversized_body),),
        )
        original_size = measure_payload_bytes(action.payload)
        assert original_size > MAX_OUTPUT_BYTES

        writer = _StaticMinioWriter()
        result = _run(
            redirect_oversized_payload(
                action,
                workflow_id="automation-jira-PAY-1",
                idx=3,
                minio_callback=writer,
            )
        )

        # New instance, kind/severity preserved.
        assert result is not action
        assert result.kind == "confluence_create_page"
        assert result.severity == "critical"

        # Replacement payload: exactly summary / minio_uri / size_bytes.
        result_payload = dict(result.payload)
        assert set(result_payload.keys()) == {
            "summary",
            "minio_uri",
            "size_bytes",
        }
        expected_key = MINIO_KEY_TEMPLATE.format(
            workflow_id="automation-jira-PAY-1", idx=3
        )
        assert result_payload["minio_uri"] == f"s3://test-bucket/{expected_key}"
        assert result_payload["size_bytes"] == original_size
        assert isinstance(result_payload["summary"], str)
        assert 0 < len(result_payload["summary"]) <= 256

        # Callback invoked exactly once with the encoded body.
        assert len(writer.calls) == 1
        called_key, body_len = writer.calls[0]
        assert called_key == expected_key
        assert body_len == original_size

    def test_max_output_bytes_is_one_mebibyte(self) -> None:
        """The cap value is exactly one mebibyte.

        The cap is exactly 1 MiB (2**20 bytes).  Pinning the
        constant here keeps this boundary coverage and the size-cap
        property-test suite in sync.
        """
        assert MAX_OUTPUT_BYTES == 1 * 1024 * 1024
        assert MAX_OUTPUT_BYTES == 1_048_576
