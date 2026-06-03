"""invariant for pure infrastructural invariants.



invariant (1, 2, 3, 4): Pure infrastructural invariants — artifact path
naming, draft PR coercion, cleanup decision truth table.

This module tests three categories of pure-function invariants:

1. **Artifact path naming** — ``agent_artifact_key`` always produces keys
 starting with ``artifacts/`` and ``execution_artifact_key`` always
 produces keys starting with ``executions/``.
2. **Draft PR coercion** — ``coerce_draft_true(x)`` returns ``True`` for
 any input value ( §1 Kural 10).
3. **Cleanup decision truth table** — ``should_cleanup(policy, exit_code)``
 matches the documented truth table for all combinations of policy and
 exit code.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from temporal_shared.helpers import (
    CleanupPolicy,
    coerce_draft_true,
    should_cleanup,
)
from temporal_shared.identifiers import (
    agent_artifact_key,
    execution_artifact_key,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid Jira issue key: ^[A-Z][A-Z0-9_]+-[1-9][0-9]*$
# The regex requires at least 2 chars before the dash: one [A-Z] then one+ [A-Z0-9_]
_ISSUE_KEY_FIRST_CHAR = st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ISSUE_KEY_REST = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"),
    min_size=1,
    max_size=7,
)

_ISSUE_KEY_NUMBER = st.integers(min_value=1, max_value=999999)

_VALID_ISSUE_KEY = st.builds(
    lambda first, rest, num: f"{first}{rest}-{num}",
    _ISSUE_KEY_FIRST_CHAR,
    _ISSUE_KEY_REST,
    _ISSUE_KEY_NUMBER,
)

# Iteration numbers (positive)
_ITERATION = st.integers(min_value=1, max_value=1000)

# Safe filenames (no path separators, non-empty)
_SAFE_FILENAME = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyz0123456789-_."
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s[0] not in ".-" and ".." not in s)

# Workflow IDs (non-empty strings without whitespace)
_WORKFLOW_ID = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyz0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    ),
    min_size=1,
    max_size=80,
)

# Artifact names (non-empty, safe characters)
_ARTIFACT_NAME = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyz0123456789-_."
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s[0] not in ".-")

# Cleanup policies
_CLEANUP_POLICY = st.sampled_from(["always", "on_success", "never"])

# Exit codes (any integer, including negative)
_EXIT_CODE = st.integers(min_value=-128, max_value=255)

# Arbitrary values for coerce_draft_true testing
_ANY_VALUE = st.one_of(
    st.booleans(),
    st.none(),
    st.integers(min_value=-100, max_value=100),
    st.text(max_size=20),
    st.floats(allow_nan=False, allow_infinity=False),
    st.just(0),
    st.just(1),
    st.just("true"),
    st.just("false"),
    st.just("True"),
    st.just("False"),
    st.just(""),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
)


# ---------------------------------------------------------------------------
# invariant: Artifact path naming (agent) — always starts with
# "artifacts/" prefix
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(issue_key=_VALID_ISSUE_KEY, iteration=_ITERATION, filename=_SAFE_FILENAME)
def test_agent_artifact_key_prefix(
    issue_key: str, iteration: int, filename: str
) -> None:
    """agent_artifact_key always produces keys starting with 'artifacts/'."""
    key = agent_artifact_key(issue_key, iteration, filename)
    assert key.startswith("artifacts/"), (
        f"Expected key to start with 'artifacts/', got: {key!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(issue_key=_VALID_ISSUE_KEY, iteration=_ITERATION, filename=_SAFE_FILENAME)
def test_agent_artifact_key_format(
    issue_key: str, iteration: int, filename: str
) -> None:
    """agent_artifact_key produces keys matching artifacts/{issue_key}/iter-{N}/{filename}."""
    key = agent_artifact_key(issue_key, iteration, filename)
    expected = f"artifacts/{issue_key}/iter-{iteration}/{filename}"
    assert key == expected, f"Expected {expected!r}, got {key!r}"


# ---------------------------------------------------------------------------
# invariant: Artifact path naming (execution) — always starts with
# "executions/" prefix
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(workflow_id=_WORKFLOW_ID, name=_ARTIFACT_NAME)
def test_execution_artifact_key_prefix(workflow_id: str, name: str) -> None:
    """execution_artifact_key always produces keys starting with 'executions/'."""
    key = execution_artifact_key(workflow_id, name)
    assert key.startswith("executions/"), (
        f"Expected key to start with 'executions/', got: {key!r}"
    )


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(workflow_id=_WORKFLOW_ID, name=_ARTIFACT_NAME)
def test_execution_artifact_key_format(workflow_id: str, name: str) -> None:
    """execution_artifact_key produces keys matching executions/{workflow_id}/{name}."""
    key = execution_artifact_key(workflow_id, name)
    expected = f"executions/{workflow_id}/{name}"
    assert key == expected, f"Expected {expected!r}, got {key!r}"


# ---------------------------------------------------------------------------
# invariant: Draft PR enforcement — coerce_draft_true always returns True
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(value=_ANY_VALUE)
def test_coerce_draft_true_always_true(value: object) -> None:
    """coerce_draft_true(x) returns True for any input value."""
    result = coerce_draft_true(value)
    assert result is True, (
        f"coerce_draft_true({value!r}) returned {result!r}, expected True"
    )


def test_coerce_draft_true_explicit_false_values() -> None:
    """coerce_draft_true returns True even for explicitly falsy values."""
    falsy_values = [False, None, 0, "", "false", "False", [], {}, 0.0]
    for val in falsy_values:
        assert coerce_draft_true(val) is True, (
            f"coerce_draft_true({val!r}) should be True"
        )


def test_coerce_draft_true_explicit_truthy_values() -> None:
    """coerce_draft_true returns True for truthy values too."""
    truthy_values = [True, 1, "true", "True", "yes", [1], {"a": 1}]
    for val in truthy_values:
        assert coerce_draft_true(val) is True, (
            f"coerce_draft_true({val!r}) should be True"
        )


# ---------------------------------------------------------------------------
# invariant: Cleanup decision truth table
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(exit_code=_EXIT_CODE)
def test_should_cleanup_always_returns_true(exit_code: int) -> None:
    """should_cleanup('always', any_exit_code) is always True."""
    assert should_cleanup("always", exit_code) is True


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(exit_code=_EXIT_CODE)
def test_should_cleanup_never_returns_false(exit_code: int) -> None:
    """should_cleanup('never', any_exit_code) is always False."""
    assert should_cleanup("never", exit_code) is False


def test_should_cleanup_on_success_zero_exit() -> None:
    """should_cleanup('on_success', 0) is True."""
    assert should_cleanup("on_success", 0) is True


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(
    exit_code=st.integers(min_value=-128, max_value=255).filter(
        lambda x: x != 0
    )
)
def test_should_cleanup_on_success_nonzero_exit(exit_code: int) -> None:
    """should_cleanup('on_success', nonzero) is False."""
    assert should_cleanup("on_success", exit_code) is False


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(policy=_CLEANUP_POLICY, exit_code=_EXIT_CODE)
def test_should_cleanup_truth_table_comprehensive(
    policy: str, exit_code: int
) -> None:
    """should_cleanup matches the full truth table for all (policy, exit_code) pairs."""
    result = should_cleanup(policy, exit_code)  # type: ignore[arg-type]

    if policy == "always":
        assert result is True
    elif policy == "on_success":
        if exit_code == 0:
            assert result is True
        else:
            assert result is False
    elif policy == "never":
        assert result is False
    else:
        pytest.fail(f"Unexpected policy: {policy!r}")


def test_should_cleanup_invalid_policy_raises() -> None:
    """should_cleanup raises ValueError for invalid policy strings."""
    with pytest.raises(ValueError, match="Invalid cleanup policy"):
        should_cleanup("sometimes", 0)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid cleanup policy"):
        should_cleanup("", 0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# invariant (additional): Determinism — same inputs always produce
# same output
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(policy=_CLEANUP_POLICY, exit_code=_EXIT_CODE)
def test_should_cleanup_deterministic(policy: str, exit_code: int) -> None:
    """should_cleanup is deterministic: same inputs always yield same output."""
    result1 = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
    result2 = should_cleanup(policy, exit_code)  # type: ignore[arg-type]
    assert result1 == result2
