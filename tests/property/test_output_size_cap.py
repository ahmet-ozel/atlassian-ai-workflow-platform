"""Property tests for the output-action size cap with MinIO redirection.

Validates: Requirements 5.9, 12.3
Property 10(e) (size-cap branch): an :class:`OutputAction` whose
JSON-encoded payload exceeds :data:`MAX_OUTPUT_BYTES` is offloaded to
MinIO and rewritten with a ``summary + minio_uri`` payload; payloads
within the cap pass through unchanged.

This file pins the four invariants documented by the task brief:

1. **Identity below the cap.**  When the encoded payload size is
   ≤ 1 MiB the helper returns the action **unchanged** and never
   invokes the MinIO callback.
2. **Replacement above the cap.**  When the encoded payload size is
   > 1 MiB the helper invokes the MinIO callback exactly once with
   the canonical key shape ``ai-runs/{workflow_id}/output-{idx}.json``
   and returns a new action whose payload exposes the
   ``summary``/``minio_uri``/``size_bytes`` triple.
3. **Determinism.**  Calling the helper twice on the same input
   (with a deterministic callback) produces the same returned action
   on both calls — required by Property 10(e) so replay-time
   reasoning about the offload is sound.
4. **``format_final_jira_comment`` line presence rules.**  The ``✅``
   line is omitted when ``critical_done`` is empty; the ``⚠️`` line
   is omitted when ``best_effort_failed`` is empty; both empty →
   empty string; both populated → ✅ then ⚠️ in fixed order.

Hypothesis is used to drive the size-cap properties across a wide
range of payload shapes and sizes.  Tests below the cap use small
payloads; tests above the cap use a single large string field whose
length is parameterised by Hypothesis to span the boundary by a
deterministic margin.

The test never makes a real network or filesystem call — the MinIO
callback is a recording stub that returns a deterministic URI built
from its inputs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from temporal_shared.messages import OutputAction
from temporal_shared.output_size_cap import (
    FINAL_COMMENT_BEST_EFFORT_PREFIX,
    FINAL_COMMENT_CRITICAL_PREFIX,
    MAX_OUTPUT_BYTES,
    MINIO_KEY_TEMPLATE,
    format_final_jira_comment,
    measure_payload_bytes,
    redirect_oversized_payload,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _MinioRecorder:
    """Recording stub for the :class:`MinioCallback` protocol.

    Each ``__call__`` records ``(key, body)`` and returns a
    deterministic URI derived from the key — so two calls with the
    same key produce the same URI, satisfying the determinism
    requirement of Property 10(e).
    """

    calls: list[tuple[str, bytes]] = field(default_factory=list)

    async def __call__(self, *, key: str, body: bytes) -> str:
        self.calls.append((key, body))
        return f"s3://test-bucket/{key}"


def _run(coro):
    """Synchronously drive a coroutine to completion in a fresh loop.

    The helper isolates each ``asyncio.run`` invocation so Hypothesis
    examples do not leak event-loop state across iterations.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Workflow id strategy — the helper validates non-empty strings, so we
# generate a small alphabet that is also valid in MinIO object keys to
# avoid second-guessing the URI escape rules under test.
_VALID_WORKFLOW_ID: Final = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=40,
)

_VALID_IDX: Final = st.integers(min_value=0, max_value=999)

# OutputAction kind/severity pair — drawn from the closed vocabulary
# documented in :mod:`temporal_shared.messages`.  We sample (kind,
# severity) pairs so the dataclass invariant (kind ↔ severity) holds
# for every generated action; the cap helper preserves both fields and
# does not partition by severity, but feeding it bogus data would
# distract from the property under test.
_VALID_ACTION_KIND_SEVERITY: Final = st.sampled_from(
    [
        ("jira_comment", "critical"),
        ("bitbucket_create_pr", "critical"),
        ("confluence_create_page", "critical"),
        ("confluence_update_page", "critical"),
        ("slack_notify", "best_effort"),
        ("email_notify", "best_effort"),
        ("jira_attachment", "best_effort"),
    ]
)

# Small payload strategy — total encoded size will be well below the
# 1 MiB cap.  We encode each generated payload through
# :func:`measure_payload_bytes` and ``assume`` the result is small
# enough; this keeps the strategy honest without bespoke math.
_SMALL_PAYLOAD_VALUE: Final = st.one_of(
    st.text(max_size=64),
    st.integers(min_value=-(2**31), max_value=2**31 - 1),
    st.booleans(),
    st.none(),
    st.floats(
        allow_nan=False, allow_infinity=False, width=32
    ),  # JSON-friendly
)

_SMALL_PAYLOAD: Final = st.lists(
    st.tuples(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cs",),  # surrogates not JSON-safe
            ),
            min_size=1,
            max_size=24,
        ),
        _SMALL_PAYLOAD_VALUE,
    ),
    max_size=8,
).map(tuple)


# Strategy that produces a payload guaranteed to **exceed** the cap.
# We construct it from a single ``("body", <large string>)`` pair with
# the string length tuned so the JSON encoding is at least
# ``MAX_OUTPUT_BYTES + margin`` bytes.  Using a single ASCII string
# keeps the size math trivial: each char encodes to 1 byte, and the
# JSON wrapping overhead is < 16 bytes.
_OVERSIZED_PAYLOAD: Final = st.integers(
    min_value=MAX_OUTPUT_BYTES + 1, max_value=MAX_OUTPUT_BYTES + 1024
).map(lambda n: (("body", "x" * n),))


# ---------------------------------------------------------------------------
# Property 1: identity below the cap
# ---------------------------------------------------------------------------


class TestRedirectIdentityBelowCap:
    """Payload ≤ 1 MiB → action returned unchanged, callback not invoked."""

    @settings(
        max_examples=100,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(
        kind_severity=_VALID_ACTION_KIND_SEVERITY,
        payload=_SMALL_PAYLOAD,
        workflow_id=_VALID_WORKFLOW_ID,
        idx=_VALID_IDX,
    )
    def test_small_payload_returns_action_unchanged(
        self,
        kind_severity: tuple[str, str],
        payload: tuple[tuple[str, object], ...],
        workflow_id: str,
        idx: int,
    ) -> None:
        """**Validates: Requirement 5.9, Property 10(e) identity branch**

        For any payload whose JSON encoding fits within
        :data:`MAX_OUTPUT_BYTES`, :func:`redirect_oversized_payload`
        returns the input action **as-is** (``is`` identity) and never
        calls the MinIO callback.  This is the identity branch of
        Property 10(e) — it pins the no-op contract for small
        payloads so the offload path stays cold for every
        well-behaved activity.
        """
        kind, severity = kind_severity
        action = OutputAction(kind=kind, severity=severity, payload=payload)

        # Sanity check the strategy produced an in-cap payload.  If
        # Hypothesis ever generates something just under 1 MiB this
        # branch is still correct, but the strategy bounds prevent it
        # in practice.
        encoded_size = measure_payload_bytes(payload)
        assert encoded_size <= MAX_OUTPUT_BYTES

        recorder = _MinioRecorder()
        result = _run(
            redirect_oversized_payload(
                action,
                workflow_id=workflow_id,
                idx=idx,
                minio_callback=recorder,
            )
        )

        # Identity contract: same instance, no callback invocation.
        assert result is action
        assert recorder.calls == []


# ---------------------------------------------------------------------------
# Property 2: replacement above the cap
# ---------------------------------------------------------------------------


class TestRedirectReplacementAboveCap:
    """Payload > 1 MiB → action replaced, callback invoked exactly once."""

    @settings(
        # Few examples — each one materialises a > 1 MiB string, so we
        # cap the count to keep the suite fast.  10 examples is enough
        # to exercise a range of oversize margins.
        max_examples=10,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.data_too_large,
        ],
        deadline=None,
    )
    @given(
        kind_severity=_VALID_ACTION_KIND_SEVERITY,
        payload=_OVERSIZED_PAYLOAD,
        workflow_id=_VALID_WORKFLOW_ID,
        idx=_VALID_IDX,
    )
    def test_oversized_payload_is_offloaded_and_summarised(
        self,
        kind_severity: tuple[str, str],
        payload: tuple[tuple[str, object], ...],
        workflow_id: str,
        idx: int,
    ) -> None:
        """**Validates: Requirement 5.9, Property 10(e) offload branch**

        For any payload whose JSON encoding exceeds
        :data:`MAX_OUTPUT_BYTES`, the helper:

        * Calls :class:`MinioCallback` exactly once with the
          spec-mandated key
          ``ai-runs/{workflow_id}/output-{idx}.json`` and the
          full encoded body.
        * Returns a new :class:`OutputAction` whose
          ``kind``/``severity`` mirror the input and whose payload is
          a tuple-of-pairs exposing exactly three keys —
          ``summary``, ``minio_uri``, ``size_bytes``.
        * The replacement is not the same instance as the input
          (``is not``).
        """
        kind, severity = kind_severity
        action = OutputAction(kind=kind, severity=severity, payload=payload)

        # The strategy produces oversized payloads by construction;
        # the assertion documents the precondition for the test.
        original_size = measure_payload_bytes(payload)
        assert original_size > MAX_OUTPUT_BYTES

        recorder = _MinioRecorder()
        result = _run(
            redirect_oversized_payload(
                action,
                workflow_id=workflow_id,
                idx=idx,
                minio_callback=recorder,
            )
        )

        # Callback was invoked exactly once with the expected key.
        assert len(recorder.calls) == 1
        called_key, called_body = recorder.calls[0]
        expected_key = MINIO_KEY_TEMPLATE.format(
            workflow_id=workflow_id, idx=idx
        )
        assert called_key == expected_key
        # The body is the canonical encoding, so its length matches
        # the size_bytes recorded in the replacement payload.
        assert len(called_body) == original_size

        # Result is a new instance with preserved kind/severity.
        assert result is not action
        assert result.kind == kind
        assert result.severity == severity

        # Replacement payload shape: tuple-of-pairs with the three
        # documented keys.
        result_keys = {key for key, _ in result.payload}
        assert result_keys == {"summary", "minio_uri", "size_bytes"}
        # Round-trip the replacement payload to a dict for value
        # assertions; tuple iteration would also work but a dict view
        # makes the test intent clearer.
        result_payload = dict(result.payload)
        assert result_payload["minio_uri"] == f"s3://test-bucket/{expected_key}"
        assert result_payload["size_bytes"] == original_size
        assert isinstance(result_payload["summary"], str)
        # Summary is the prefix of the encoded JSON, capped to 256
        # chars (the constant is exposed but the contract under test
        # is "non-empty + within cap"; the byte-cap test pins the
        # exact length).
        assert 0 < len(result_payload["summary"]) <= 256


# ---------------------------------------------------------------------------
# Property 3: determinism — same input twice yields the same output
# ---------------------------------------------------------------------------


class TestRedirectDeterminism:
    """Same input + deterministic callback → same redirected action."""

    @settings(
        max_examples=10,
        suppress_health_check=[
            HealthCheck.too_slow,
            HealthCheck.data_too_large,
        ],
        deadline=None,
    )
    @given(
        kind_severity=_VALID_ACTION_KIND_SEVERITY,
        payload=_OVERSIZED_PAYLOAD,
        workflow_id=_VALID_WORKFLOW_ID,
        idx=_VALID_IDX,
    )
    def test_redirect_is_deterministic(
        self,
        kind_severity: tuple[str, str],
        payload: tuple[tuple[str, object], ...],
        workflow_id: str,
        idx: int,
    ) -> None:
        """**Validates: Requirement 5.9, Property 10(e) determinism clause**

        Two evaluations of :func:`redirect_oversized_payload` with the
        same inputs and a deterministic callback produce equal
        :class:`OutputAction` instances.  This is the determinism
        clause of Property 10(e) — the helper must be replay-safe
        when it lands inside :class:`AgentRunnerWorkflow`.
        """
        kind, severity = kind_severity
        action = OutputAction(kind=kind, severity=severity, payload=payload)

        # Two independent recorders (so each call is fresh) but both
        # use the same deterministic URI rule, mirroring how a real
        # MinIO writer would behave for the same key.
        recorder_a = _MinioRecorder()
        recorder_b = _MinioRecorder()

        first = _run(
            redirect_oversized_payload(
                action,
                workflow_id=workflow_id,
                idx=idx,
                minio_callback=recorder_a,
            )
        )
        second = _run(
            redirect_oversized_payload(
                action,
                workflow_id=workflow_id,
                idx=idx,
                minio_callback=recorder_b,
            )
        )

        # Frozen dataclasses with the same field values compare equal.
        assert first == second
        # And the callback observed identical inputs both times.
        assert recorder_a.calls == recorder_b.calls


# ---------------------------------------------------------------------------
# Property 4: format_final_jira_comment line-presence rules
# ---------------------------------------------------------------------------


# Strategies for the comment formatter — short ASCII strings keep the
# rendered output compact and the line-presence assertions sharp.
_NAME_STR: Final = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),  # no surrogates / control
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789_-",
    ),
    min_size=1,
    max_size=20,
)

_REASON_STR: Final = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"),
        whitelist_characters="abcdefghijklmnopqrstuvwxyz0123456789_- ",
    ),
    min_size=0,
    max_size=40,
)

_CRITICAL_LIST: Final = st.lists(_NAME_STR, max_size=8)
_FAILED_LIST: Final = st.lists(
    st.tuples(_NAME_STR, _REASON_STR), max_size=8
)


class TestFormatFinalJiraCommentInvariants:
    """Line-presence rules pinned by Requirement 12.3."""

    def test_both_empty_returns_empty_string(self) -> None:
        """**Validates: Requirement 12.3 — both empty → empty string**

        Pins the documented edge case: when the workflow produced
        neither completed critical steps nor failed best-effort
        actions, the formatter returns the empty string so the
        caller can suppress the final comment entirely.
        """
        assert format_final_jira_comment([], []) == ""

    @settings(max_examples=100)
    @given(critical_done=_CRITICAL_LIST)
    def test_only_critical_omits_warning_line(
        self, critical_done: list[str]
    ) -> None:
        """**Validates: Requirement 12.3 — empty failed → no ⚠️ line**

        With ``best_effort_failed`` empty the formatter must not
        emit the ``⚠️`` prefix anywhere in the result regardless of
        the critical-step content (or its absence).  When
        ``critical_done`` is also empty the result is the empty
        string; otherwise it begins with the ✅ prefix.
        """
        result = format_final_jira_comment(critical_done, [])

        # ⚠️ prefix never appears.
        assert FINAL_COMMENT_BEST_EFFORT_PREFIX not in result

        # Filter out the empty-name defensive drop applied by the
        # formatter so the assertion matches the rendered output.
        non_empty = [name for name in critical_done if name]
        if non_empty:
            assert result.startswith(FINAL_COMMENT_CRITICAL_PREFIX)
            # Single line — no separator embedded.
            assert "\n" not in result
        else:
            assert result == ""

    @settings(max_examples=100)
    @given(best_effort_failed=_FAILED_LIST)
    def test_only_failed_omits_check_line(
        self, best_effort_failed: list[tuple[str, str]]
    ) -> None:
        """**Validates: Requirement 12.3 — empty critical → no ✅ line**

        With ``critical_done`` empty the formatter must not emit
        the ✅ prefix.  When ``best_effort_failed`` is non-empty
        (after the empty-name defensive drop) the result starts
        with the ⚠️ prefix; otherwise it is the empty string.
        """
        result = format_final_jira_comment([], best_effort_failed)

        assert FINAL_COMMENT_CRITICAL_PREFIX not in result

        non_empty = [
            (name, reason)
            for name, reason in best_effort_failed
            if name
        ]
        if non_empty:
            assert result.startswith(FINAL_COMMENT_BEST_EFFORT_PREFIX)
            assert "\n" not in result
        else:
            assert result == ""

    @settings(max_examples=100)
    @given(
        critical_done=_CRITICAL_LIST.filter(
            lambda items: any(name for name in items)
        ),
        best_effort_failed=_FAILED_LIST.filter(
            lambda items: any(name for name, _ in items)
        ),
    )
    def test_both_populated_renders_both_lines_in_fixed_order(
        self,
        critical_done: list[str],
        best_effort_failed: list[tuple[str, str]],
    ) -> None:
        """**Validates: Requirement 12.3 — both lines, ✅ before ⚠️**

        When both lists carry at least one non-empty name the
        formatter emits two lines separated by ``\\n`` with the
        ``✅`` line first and the ``⚠️`` line second — the order
        pinned verbatim by the requirement text.
        """
        result = format_final_jira_comment(
            critical_done, best_effort_failed
        )

        # Two lines exactly.
        lines = result.split("\n")
        assert len(lines) == 2

        # Order: ✅ first, ⚠️ second.
        assert lines[0].startswith(FINAL_COMMENT_CRITICAL_PREFIX)
        assert lines[1].startswith(FINAL_COMMENT_BEST_EFFORT_PREFIX)

        # And the prose contains every non-empty name supplied.
        for name in critical_done:
            if name:
                assert name in lines[0]
        for name, reason in best_effort_failed:
            if name:
                rendered = f"{name} ({reason})"
                assert rendered in lines[1]


# ---------------------------------------------------------------------------
# Concrete examples — guard against regressions in the canonical shape
# ---------------------------------------------------------------------------


class TestFormatFinalJiraCommentExamples:
    """Concrete examples reproducing the requirement's prose verbatim."""

    def test_canonical_example_from_requirement(self) -> None:
        """**Validates: Requirement 12.3 — canonical sample**

        Pins the exact sample shape from the task brief so a future
        refactor cannot accidentally drift the prose.
        """
        result = format_final_jira_comment(
            ["a", "b", "c"],
            [("x", "sebep1"), ("y", "sebep2")],
        )
        assert result == (
            "\u2705 Tamamlanan kritik ad\u0131mlar: a, b, c\n"
            "\u26a0\ufe0f Ba\u015far\u0131s\u0131z yan-aksiyonlar: "
            "x (sebep1), y (sebep2)"
        )

    def test_critical_only_example(self) -> None:
        """**Validates: Requirement 12.3 — critical-only sample**"""
        assert format_final_jira_comment(["alpha"], []) == (
            "\u2705 Tamamlanan kritik ad\u0131mlar: alpha"
        )

    def test_failed_only_example(self) -> None:
        """**Validates: Requirement 12.3 — failed-only sample**"""
        assert format_final_jira_comment(
            [], [("slack_notify", "rate_limited")]
        ) == (
            "\u26a0\ufe0f Ba\u015far\u0131s\u0131z yan-aksiyonlar: "
            "slack_notify (rate_limited)"
        )


# ---------------------------------------------------------------------------
# Argument validation — pinned at the unit level so the property tests
# can stay focused on the cap behaviour.
# ---------------------------------------------------------------------------


class TestRedirectArgumentValidation:
    """Defensive checks documented in :func:`redirect_oversized_payload`."""

    def test_rejects_non_outputaction(self) -> None:
        """**Validates: Requirement 5.9 — type contract**"""
        recorder = _MinioRecorder()
        with pytest.raises(TypeError, match="action must be"):
            _run(
                redirect_oversized_payload(
                    "not an OutputAction",  # type: ignore[arg-type]
                    workflow_id="wf-1",
                    idx=0,
                    minio_callback=recorder,
                )
            )

    def test_rejects_empty_workflow_id(self) -> None:
        """**Validates: Requirement 5.9 — workflow_id required for key**"""
        action = OutputAction(
            kind="jira_comment",
            severity="critical",
            payload=(("body", "x"),),
        )
        recorder = _MinioRecorder()
        with pytest.raises(ValueError, match="workflow_id must not be empty"):
            _run(
                redirect_oversized_payload(
                    action,
                    workflow_id="",
                    idx=0,
                    minio_callback=recorder,
                )
            )

    def test_rejects_negative_idx(self) -> None:
        """**Validates: Requirement 5.9 — idx must be non-negative**"""
        action = OutputAction(
            kind="jira_comment",
            severity="critical",
            payload=(("body", "x"),),
        )
        recorder = _MinioRecorder()
        with pytest.raises(ValueError, match="idx must be non-negative"):
            _run(
                redirect_oversized_payload(
                    action,
                    workflow_id="wf-1",
                    idx=-1,
                    minio_callback=recorder,
                )
            )

    def test_rejects_bool_idx(self) -> None:
        """**Validates: Requirement 5.9 — bool rejected as idx**

        ``bool`` is a subclass of ``int`` in Python; the helper
        rejects it explicitly so a stray ``True``/``False`` cannot
        silently collide with index 0/1 in the offload key.
        """
        action = OutputAction(
            kind="jira_comment",
            severity="critical",
            payload=(("body", "x"),),
        )
        recorder = _MinioRecorder()
        with pytest.raises(TypeError, match="idx must be an int"):
            _run(
                redirect_oversized_payload(
                    action,
                    workflow_id="wf-1",
                    idx=True,  # type: ignore[arg-type]
                    minio_callback=recorder,
                )
            )

    def test_rejects_non_callable_callback(self) -> None:
        """**Validates: Requirement 5.9 — callback must be callable**"""
        action = OutputAction(
            kind="jira_comment",
            severity="critical",
            payload=(("body", "x"),),
        )
        with pytest.raises(TypeError, match="minio_callback must be"):
            _run(
                redirect_oversized_payload(
                    action,
                    workflow_id="wf-1",
                    idx=0,
                    minio_callback="not callable",  # type: ignore[arg-type]
                )
            )


# ---------------------------------------------------------------------------
# Constant pinning — guard against accidental drift of the public cap
# ---------------------------------------------------------------------------


class TestPublicConstants:
    """Public constants are pinned by the requirement text."""

    def test_max_output_bytes_is_one_mebibyte(self) -> None:
        """**Validates: Requirement 5.9 — 1 MB cap**

        The cap is exactly 1 MiB (2**20 bytes), matching the
        S3/MinIO size-header convention.
        """
        assert MAX_OUTPUT_BYTES == 1 * 1024 * 1024
        assert MAX_OUTPUT_BYTES == 1_048_576

    def test_minio_key_template_matches_requirement(self) -> None:
        """**Validates: Requirement 5.9 — canonical MinIO key shape**"""
        assert MINIO_KEY_TEMPLATE == "ai-runs/{workflow_id}/output-{idx}.json"
        # Round-trip a sample to pin the substitution semantics.
        assert MINIO_KEY_TEMPLATE.format(
            workflow_id="automation-jira-PAY-1", idx=3
        ) == "ai-runs/automation-jira-PAY-1/output-3.json"

    def test_final_comment_prefixes_use_real_glyphs(self) -> None:
        """**Validates: Requirement 12.3 — emoji + Turkish prose**

        The runtime values must be the real ✅ and ⚠️ glyphs (not
        the escape sequences) and must contain the Turkish dotted-i
        / s-cedilla characters from the spec text.
        """
        assert FINAL_COMMENT_CRITICAL_PREFIX.startswith("\u2705")
        assert "ad\u0131mlar" in FINAL_COMMENT_CRITICAL_PREFIX
        assert FINAL_COMMENT_BEST_EFFORT_PREFIX.startswith("\u26a0")
        assert "Ba\u015far\u0131s\u0131z" in FINAL_COMMENT_BEST_EFFORT_PREFIX
