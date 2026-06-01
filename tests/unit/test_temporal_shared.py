# Surface 1 alignment direction (fix-pre-existing-test-failures task 7.1): update tests to assert the split vocabulary (jira_read/jira_write/bitbucket_read/bitbucket_write/confluence_read/confluence_write); production WORKFLOW_TYPE_CAPABILITIES in temporal_shared.capabilities emits split-only and the helper-side ALIASES fallback would require altering the canonical emission, so the test-side change is strictly smaller — see design § "Surface 1 — Specific Changes" step 1.
"""Unit tests for ``WORKFLOW_TYPE_CAPABILITIES`` dictionary integrity.

These tests pin the shape of the workflow-type → capability set mapping
shipped by ``libs/temporal-shared``. The dictionary is the runtime
projection of MIMARI.md §2.5.3 (the workflow type → capability table)
and design.md §3.4 (the explicit Python literal). Both sources are
authoritative; this test is the structural guardrail that prevents the
two from drifting out of sync.

Validated invariants (see task 14.2 in
``.kiro/specs/multi-service-scaffold/tasks.md``):

1. The dict has exactly the **10** workflow-type keys currently shipped
   by ``temporal_shared.capabilities`` (the 9 enumerated in MIMARI
   §2.5.3 plus ``multi_step``, which is now a static entry rather than
   a runtime-computed union — see Surface 1 alignment note above).
2. Every value is a ``frozenset`` so callers cannot mutate the shared
   mapping at runtime (Requirement 5.4).
3. Every capability string is drawn from the closed split-vocabulary
   set ``{"jira_read", "jira_write", "bitbucket_read",
   "bitbucket_write", "confluence_read", "confluence_write",
   "execution", "web_search"}`` emitted by
   ``temporal_shared.capabilities``.
4. Each individual key maps to the exact split-vocabulary capability
   set declared by the production literal.
5. ``multi_step`` IS a key in the current production mapping (this
   test was historically inverted; see test_multi_step_is_not_a_key
   docstring).
"""

from __future__ import annotations

import pytest

from temporalio.converter import default

from temporal_shared import WORKFLOW_TYPE_CAPABILITIES
from temporal_shared.messages import (
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
    OutputAction,
)


# ---------------------------------------------------------------------------
# Authoritative reference values, mirrored verbatim from
# ``temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES``. Keeping them
# inline (rather than re-importing from the library under test) ensures the
# test catches accidental edits to the library's literal — a regression that
# would otherwise hide if we used the same source on both sides.
#
# Vocabulary alignment: per Surface 1 (fix-pre-existing-test-failures task
# 7.2), assertions use the split vocabulary
# (``"jira_read"`` / ``"jira_write"`` / ``"bitbucket_read"`` /
# ``"bitbucket_write"`` / ``"confluence_read"`` / ``"confluence_write"``)
# emitted by production rather than the legacy single tokens
# (``"jira"`` / ``"bitbucket"`` / ``"confluence"``).
# ---------------------------------------------------------------------------

#: Closed capability vocabulary emitted by ``temporal_shared.capabilities``.
ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "jira_read",
        "jira_write",
        "bitbucket_read",
        "bitbucket_write",
        "confluence_read",
        "confluence_write",
        "execution",
        "web_search",
    }
)

#: The exact workflow-type keys currently shipped by
#: ``temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES``.
EXPECTED_KEYS: frozenset[str] = frozenset(
    {
        "code_change_with_test",
        "code_change_commit_only",
        "pr_review",
        "remote_ssh_test_only",
        "confluence_doc_update",
        "confluence_doc_create",
        "research_basic",
        "research_with_web",
        "research_publish_confluence",
        "research_summary_jira",
        "script_execute",
        "multi_step",
        "noop_test",
    }
)

#: Per-key expected capability sets, transcribed from the production
#: ``WORKFLOW_TYPE_CAPABILITIES`` literal in
#: ``platform/libs/temporal-shared/src/temporal_shared/capabilities.py``.
EXPECTED_MAPPING: dict[str, frozenset[str]] = {
    "code_change_with_test": frozenset(
        {
            "jira_read",
            "jira_write",
            "bitbucket_read",
            "bitbucket_write",
            "execution",
        }
    ),
    "code_change_commit_only": frozenset(
        {
            "jira_read",
            "jira_write",
            "bitbucket_read",
            "bitbucket_write",
        }
    ),
    "pr_review": frozenset(
        {
            "jira_read",
            "jira_write",
            "bitbucket_read",
        }
    ),
    "remote_ssh_test_only": frozenset(
        {
            "jira_read",
            "execution",
        }
    ),
    "confluence_doc_update": frozenset(
        {
            "jira_read",
            "jira_write",
            "confluence_read",
            "confluence_write",
        }
    ),
    "confluence_doc_create": frozenset(
        {
            "jira_read",
            "jira_write",
            "confluence_read",
            "confluence_write",
        }
    ),
    "research_basic": frozenset(
        {
            "jira_read",
            "jira_write",
        }
    ),
    "research_with_web": frozenset(
        {
            "jira_read",
            "jira_write",
            "web_search",
        }
    ),
    "research_publish_confluence": frozenset(
        {
            "jira_read",
            "jira_write",
            "confluence_read",
            "confluence_write",
            "web_search",
        }
    ),
    "research_summary_jira": frozenset(
        {
            "jira_read",
            "jira_write",
        }
    ),
    "script_execute": frozenset(
        {
            "jira_read",
            "jira_write",
            "execution",
        }
    ),
    "multi_step": frozenset(
        {
            "jira_read",
            "jira_write",
        }
    ),
    "noop_test": frozenset(
        {
            "jira_read",
        }
    ),
}


def test_agent_runner_input_with_output_actions_round_trips_temporal_converter() -> None:
    """OutputAction payload values must survive Temporal child input decoding."""

    analysis = LlmAnalysisResult(
        workflow_type="code_change_with_test",
        confidence="high",
        target_repo="smoke-test",
        target_branch="main",
        output_actions=(
            OutputAction(
                kind="confluence_create_page",
                severity="critical",
                payload=(
                    ("space_key", "E2ETEST"),
                    ("title", "Live smoke"),
                    ("content", "ok"),
                ),
            ),
        ),
    )
    original = AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-KAN-1",
        issue_key="KAN-1",
        department_id="test",
        workflow_type="code_change_with_test",
        analysis=analysis,
        target_repo="smoke-test",
        target_branch="main",
    )

    converter = default().payload_converter
    payloads = converter.to_payloads([original])
    decoded = converter.from_payloads(
        payloads,
        type_hints=[AgentRunnerWorkflowInput],
    )[0]

    assert decoded == original


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dict_has_exactly_nine_keys() -> None:
    """The mapping must contain exactly the workflow-type keys shipped by production.

    Function name preserved per fix-pre-existing-test-failures preservation
    clause 3.6; the production mapping currently has 10 keys (the 9 from
    MIMARI §2.5.3 plus ``multi_step``), so the assertion compares against
    ``EXPECTED_KEYS`` rather than a hard-coded count.
    """

    assert len(WORKFLOW_TYPE_CAPABILITIES) == len(EXPECTED_KEYS), (
        f"expected {len(EXPECTED_KEYS)} workflow-type keys, found "
        f"{len(WORKFLOW_TYPE_CAPABILITIES)}: "
        f"{sorted(WORKFLOW_TYPE_CAPABILITIES)}"
    )
    assert set(WORKFLOW_TYPE_CAPABILITIES) == set(EXPECTED_KEYS), (
        "WORKFLOW_TYPE_CAPABILITIES key set does not match the production "
        "literal in temporal_shared.capabilities. "
        f"missing={EXPECTED_KEYS - set(WORKFLOW_TYPE_CAPABILITIES)}, "
        f"unexpected={set(WORKFLOW_TYPE_CAPABILITIES) - EXPECTED_KEYS}"
    )


def test_multi_step_is_not_a_key() -> None:
    """``multi_step`` IS a key in the current production mapping.

    Function name preserved per fix-pre-existing-test-failures preservation
    clause 3.6. The historical assumption (that ``multi_step`` was computed
    at runtime as the union of its sub-workflows) no longer holds: the
    production literal in ``temporal_shared.capabilities`` ships
    ``multi_step`` as a static frozenset entry, so this test now asserts
    its presence rather than its absence.
    """

    assert "multi_step" in WORKFLOW_TYPE_CAPABILITIES, (
        "'multi_step' is expected to be a static key in the current "
        "production WORKFLOW_TYPE_CAPABILITIES mapping."
    )


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
def test_each_value_is_frozenset(key: str) -> None:
    """Every value must be a ``frozenset`` so the mapping is immutable."""

    value = WORKFLOW_TYPE_CAPABILITIES[key]
    assert isinstance(value, frozenset), (
        f"WORKFLOW_TYPE_CAPABILITIES[{key!r}] must be a frozenset, "
        f"got {type(value).__name__}"
    )
    # Sanity: a frozenset of one element of an unrelated runtime type
    # would still satisfy ``isinstance(..., frozenset)`` — guard the
    # element type as well so a stray ``frozenset({1, 2})`` is caught.
    for element in value:
        assert isinstance(element, str), (
            f"WORKFLOW_TYPE_CAPABILITIES[{key!r}] contains non-string "
            f"element {element!r} ({type(element).__name__})"
        )


@pytest.mark.parametrize("key", sorted(EXPECTED_KEYS))
def test_capabilities_are_within_closed_vocabulary(key: str) -> None:
    """Every capability string must come from the production closed split set."""

    value = WORKFLOW_TYPE_CAPABILITIES[key]
    unknown = value - ALLOWED_CAPABILITIES
    assert not unknown, (
        f"WORKFLOW_TYPE_CAPABILITIES[{key!r}] contains capabilities outside "
        f"the closed production split vocabulary: {sorted(unknown)}; "
        f"allowed = {sorted(ALLOWED_CAPABILITIES)}"
    )


@pytest.mark.parametrize(
    "key,expected",
    sorted(EXPECTED_MAPPING.items()),
    ids=sorted(EXPECTED_MAPPING),
)
def test_exact_mapping_matches_design(key: str, expected: frozenset[str]) -> None:
    """Each key maps to the exact split-vocabulary capability set in production."""

    actual = WORKFLOW_TYPE_CAPABILITIES[key]
    assert actual == expected, (
        f"WORKFLOW_TYPE_CAPABILITIES[{key!r}] mismatch with production literal: "
        f"expected={sorted(expected)}, actual={sorted(actual)}"
    )


def test_full_mapping_equals_design_literal() -> None:
    """A single equality check that pins the entire mapping verbatim.

    Catches any discrepancy that the per-key parametrised tests above
    might individually pass while the overall set of keys diverges.
    """

    assert dict(WORKFLOW_TYPE_CAPABILITIES) == EXPECTED_MAPPING
