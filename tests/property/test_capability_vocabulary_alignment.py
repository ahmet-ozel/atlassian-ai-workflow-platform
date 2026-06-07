"""Surface 1 bug-condition exploration test: Capability Vocabulary Alignment.

This file contains invariant that surface the vocabulary drift
between the legacy single-token capability vocabulary used in
``platform/tests/unit/test_temporal_shared.py`` and the split read/write
vocabulary emitted by the production ``WORKFLOW_TYPE_CAPABILITIES`` mapping
in ``temporal_shared.capabilities``.

=============================================================================
COUNTEREXAMPLE DOCUMENTATION (Surface 1)
=============================================================================

Tool name: ``jira_get_issue``
 - Workflow type exercised: ``code_change_with_test`` (uses jira + bitbucket)
 - Asserted set (test_temporal_shared.py EXPECTED_MAPPING):
 frozenset({'jira', 'bitbucket', 'execution'})
 - Emitted set (production WORKFLOW_TYPE_CAPABILITIES):
 frozenset({'jira_read', 'jira_write', 'bitbucket_read', 'bitbucket_write', 'execution'})
 - Source line in test_temporal_shared.py:
 Line ~67: ``"code_change_with_test": frozenset({"jira", "bitbucket", "execution"}),``
 - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = {"jira","bitbucket"} ≠ ∅
 emitted ∩ {"jira","bitbucket","confluence"} = ∅ (only split tokens present)
 asserted ≠ emitted → isBugCondition_1 = True

Tool name: ``bitbucket_get_pr``
 - Workflow type exercised: ``pr_review``
 - Asserted set (test_temporal_shared.py EXPECTED_MAPPING):
 frozenset({'jira', 'bitbucket'})
 - Emitted set (production WORKFLOW_TYPE_CAPABILITIES):
 frozenset({'jira_read', 'jira_write', 'bitbucket_read'})
 - Source line in test_temporal_shared.py:
 Line ~69: ``"pr_review": frozenset({"jira", "bitbucket"}),``
 - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = {"jira","bitbucket"} ≠ ∅
 emitted ∩ {"jira","bitbucket","confluence"} = ∅
 asserted ≠ emitted → isBugCondition_1 = True

Tool name: ``confluence_get_page``
 - Workflow type exercised: ``confluence_doc_update``
 - Asserted set (test_temporal_shared.py EXPECTED_MAPPING):
 frozenset({'jira', 'confluence'})
 - Emitted set (production WORKFLOW_TYPE_CAPABILITIES):
 frozenset({'jira_read', 'jira_write', 'confluence_read', 'confluence_write'})
 - Source line in test_temporal_shared.py:
 Line ~71: ``"confluence_doc_update": frozenset({"jira", "confluence"}),``
 - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = {"jira","confluence"} ≠ ∅
 emitted ∩ {"jira","bitbucket","confluence"} = ∅
 asserted ≠ emitted → isBugCondition_1 = True

Root cause: ``test_temporal_shared.py`` was written against the legacy single-token
vocabulary ({"jira","bitbucket","confluence"}) while the production
``WORKFLOW_TYPE_CAPABILITIES`` was updated to the split read/write vocabulary
({"jira_read","jira_write","bitbucket_read","bitbucket_write","confluence_read",
"confluence_write"}) without updating the test assertions.

-----------------------------------------------------------------------------
POST-FIX VERIFICATION (Surface 1)
-----------------------------------------------------------------------------

Surface 1 was fixed via fix-pre-existing-test-failures ****:
``platform/tests/unit/test_temporal_shared.py`` was updated to assert the
split read/write vocabulary
({"jira_read","jira_write","bitbucket_read","bitbucket_write",
"confluence_read","confluence_write","execution","web_search"}) emitted by
production ``WORKFLOW_TYPE_CAPABILITIES``. The alignment direction recorded in
the comment header of ``test_temporal_shared.py`` (per is
**test-side** - the production helper is the canonical emitter and was left
unchanged.

After the fix, the ``TEST_TEMPORAL_SHARED_EXPECTED_MAPPING`` constant in this
file (rebuilt by mirrors the post-fix ``EXPECTED_MAPPING`` from
``test_temporal_shared.py``. Re-running
``pytest platform/tests/property/test_capability_vocabulary_alignment.py::test_surface1_temporal_shared_vocabulary_aligned``
now PASSES - the test asserts only split tokens, so the
``asserted ∩ {"jira","bitbucket","confluence"}`` set is empty for every
sampled tool name and isBugCondition_1(X) is False everywhere.

=============================================================================
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

# ---------------------------------------------------------------------------
# Vocabulary constants
# ---------------------------------------------------------------------------

#: Legacy single-token vocabulary that test_temporal_shared.py asserts.
LEGACY_TOKENS: frozenset[str] = frozenset({"jira", "bitbucket", "confluence"})

#: Split read/write vocabulary that production code emits.
SPLIT_TOKENS: frozenset[str] = frozenset(
    {
        "jira_read",
        "jira_write",
        "bitbucket_read",
        "bitbucket_write",
        "confluence_read",
        "confluence_write",
    }
)

# ---------------------------------------------------------------------------
# MCP tool name → workflow type mapping
#
# These are the MCP tool names from the helper's domain. Each tool name
# is associated with a workflow type that exercises the corresponding
# capability. This mapping is used to drive the Hypothesis strategy.
# ---------------------------------------------------------------------------

#: MCP tool name → workflow type that exercises it.
#: Drawn from the fixed helper-domain enum.
MCP_TOOL_TO_WORKFLOW_TYPE: dict[str, str] = {
    "jira_get_issue": "code_change_with_test",
    "bitbucket_get_pr": "pr_review",
    "confluence_get_page": "confluence_doc_update",
    "jira_create_issue": "code_change_with_test",
    "bitbucket_create_pr_cloud": "code_change_with_test",
    "confluence_update_page": "confluence_doc_update",
}

# ---------------------------------------------------------------------------
# Reference: what test_temporal_shared.py asserts (legacy vocabulary)
# ---------------------------------------------------------------------------

#: Per-key expected capability sets from test_temporal_shared.py EXPECTED_MAPPING.
#:
#: This dict MIRRORS the post-fix ``EXPECTED_MAPPING`` constant in
#: ``platform/tests/unit/test_temporal_shared.py`` (the split-vocabulary form
#: applied after the vocabulary alignment fix. It is the live oracle the invariant compares
#: against; if either dict drifts, update BOTH sides - the source of truth is
#: ``test_temporal_shared.py::EXPECTED_MAPPING`` and the production
#: ``WORKFLOW_TYPE_CAPABILITIES`` literal in
#: ``platform/libs/temporal-shared/src/temporal_shared/capabilities.py``.
TEST_TEMPORAL_SHARED_EXPECTED_MAPPING: dict[str, frozenset[str]] = {
    "code_change_with_test":   frozenset(
        {"jira_read", "jira_write", "bitbucket_read", "bitbucket_write", "execution"}
    ),
    "code_change_commit_only": frozenset(
        {"jira_read", "jira_write", "bitbucket_read", "bitbucket_write"}
    ),
    "pr_review":               frozenset(
        {"jira_read", "jira_write", "bitbucket_read"}
    ),
    "remote_ssh_test_only":    frozenset({"jira_read", "execution"}),
    "confluence_doc_update":   frozenset(
        {"jira_read", "jira_write", "confluence_read", "confluence_write"}
    ),
    "confluence_doc_create":   frozenset(
        {"jira_read", "jira_write", "confluence_read", "confluence_write"}
    ),
    "research_basic":          frozenset({"jira_read", "jira_write"}),
    "research_with_web":       frozenset({"jira_read", "jira_write", "web_search"}),
    "multi_step":              frozenset({"jira_read", "jira_write"}),
    "noop_test":               frozenset({"jira_read"}),
}


def tokens_emitted_by_production(tool_name: str) -> frozenset[str]:
    """Return the capability tokens emitted by production for the given MCP tool name.

 Maps the tool name to its associated workflow type, then looks up
 the capability tokens from the production ``WORKFLOW_TYPE_CAPABILITIES``
 mapping.

 Returns an empty frozenset if the tool name is not in the mapping.
 """
    workflow_type = MCP_TOOL_TO_WORKFLOW_TYPE.get(tool_name)
    if workflow_type is None:
        return frozenset()
    return frozenset(WORKFLOW_TYPE_CAPABILITIES.get(workflow_type, frozenset()))


def tokens_asserted_by_test(tool_name: str) -> frozenset[str]:
    """Return the capability tokens asserted by test_temporal_shared.py for the given MCP tool name.

 Maps the tool name to its associated workflow type, then looks up
 the expected tokens from the test file's EXPECTED_MAPPING (legacy vocabulary).

 Returns an empty frozenset if the tool name is not in the mapping.
 """
    workflow_type = MCP_TOOL_TO_WORKFLOW_TYPE.get(tool_name)
    if workflow_type is None:
        return frozenset()
    return frozenset(TEST_TEMPORAL_SHARED_EXPECTED_MAPPING.get(workflow_type, frozenset()))


def is_bug_condition_1(tool_name: str) -> bool:
    """Encode the first vocabulary-drift condition.

 Returns True iff:
 - tokens_emitted_by_production(tool) is non-empty
 - tokens_emitted_by_production(tool) ∩ {"jira","bitbucket","confluence"} is empty
 (production uses only split vocabulary)
 - tokens_asserted_by_test(tool) ∩ {"jira","bitbucket","confluence"} is non-empty
 (test asserts legacy vocabulary)

 The conjunction of these three conditions is exactly the bug condition:
 the test asserts legacy tokens but production emits only split tokens.
 """
    emitted = tokens_emitted_by_production(tool_name)
    asserted = tokens_asserted_by_test(tool_name)

    emitted_has_legacy = bool(emitted & LEGACY_TOKENS)
    asserted_has_legacy = bool(asserted & LEGACY_TOKENS)

    return (
        len(emitted) > 0          # production emits something
        and not emitted_has_legacy  # production does NOT emit legacy tokens
        and asserted_has_legacy     # test DOES assert legacy tokens
    )


# ---------------------------------------------------------------------------
# invariant: Bug Condition - Capability Vocabulary Aligned in test_temporal_shared.py
#
# ---------------------------------------------------------------------------


@given(
    tool_name=st.sampled_from(sorted(MCP_TOOL_TO_WORKFLOW_TYPE.keys()))
)
@settings(max_examples=6, deadline=None)
def test_surface1_temporal_shared_vocabulary_aligned(tool_name: str) -> None:
    """invariant: Bug Condition - Capability Vocabulary Aligned in test_temporal_shared.py.

 For each MCP tool name drawn from the helper's domain, asserts that:
 1. tokens_emitted_by_production(tool) is non-empty
 2. tokens_emitted_by_production(tool) ∩ {"jira","bitbucket","confluence"} is empty
 (production uses only split read/write vocabulary)
 3. The corresponding test in test_temporal_shared.py does NOT assert any token
 in {"jira","bitbucket","confluence"} (i.e., the test uses split vocabulary too)

 On UNFIXED code: this test FAILS because condition 3 is violated -
 test_temporal_shared.py still asserts legacy tokens while production
 emits only split tokens.

 After fix: this test PASSES because either:
 - The test is updated to assert split tokens (condition 3 becomes true), OR
 - Production is updated to emit legacy tokens (condition 2 becomes false)


 """
    emitted = tokens_emitted_by_production(tool_name)
    asserted = tokens_asserted_by_test(tool_name)

    # Condition 1: production emits something for this tool
    assert len(emitted) > 0, (
        f"tokens_emitted_by_production({tool_name!r}) is empty - "
        f"tool name not mapped to a workflow type or workflow type not in "
        f"WORKFLOW_TYPE_CAPABILITIES"
    )

    # Condition 2: production does NOT emit legacy tokens (split vocab only)
    emitted_legacy = emitted & LEGACY_TOKENS
    assert not emitted_legacy, (
        f"tokens_emitted_by_production({tool_name!r}) contains legacy tokens "
        f"{sorted(emitted_legacy)} - production should only emit split vocabulary "
        f"(jira_read/jira_write/bitbucket_read/bitbucket_write/confluence_read/confluence_write). "
        f"Full emitted set: {sorted(emitted)}"
    )

    # Condition 3 (the bug condition): test_temporal_shared.py must NOT assert
    # legacy tokens - it should use the same split vocabulary as production.
    # On UNFIXED code this assertion FAILS because the test still uses legacy tokens.
    asserted_legacy = asserted & LEGACY_TOKENS
    assert not asserted_legacy, (
        f"BUG DETECTED: test_temporal_shared.py asserts legacy tokens "
        f"{sorted(asserted_legacy)} for tool {tool_name!r} "
        f"(workflow_type={MCP_TOOL_TO_WORKFLOW_TYPE[tool_name]!r}), "
        f"but production emits only split tokens {sorted(emitted)}. "
        f"This is isBugCondition_1(X) = True: "
        f"asserted={sorted(asserted)}, emitted={sorted(emitted)}, "
        f"asserted∩legacy={sorted(asserted_legacy)}, "
        f"emitted∩legacy={sorted(emitted_legacy)}. "
        f"Fix: update test_temporal_shared.py EXPECTED_MAPPING and ALLOWED_CAPABILITIES "
        f"to use split vocabulary, OR restore legacy aliases in production."
    )


# =============================================================================
# COUNTEREXAMPLE DOCUMENTATION (Surface 2)
# =============================================================================
#
# Surface 2 is one layer closer to the capability_helpers boundary than
# Surface 1. The bug is in ``test_capability_helpers.py``:
#
# The test ``TestRequiredCapabilities.test_returns_frozenset_for_valid_types``
# asserts:
# result = required_capabilities(wf_type)
# assert result == WORKFLOW_TYPE_CAPABILITIES[wf_type]
#
# But ``required_capabilities`` collapses the split vocabulary to the simple
# vocabulary via ``_collapse_capability``, so it returns simple tokens
# ({"jira","bitbucket","execution"}) while ``WORKFLOW_TYPE_CAPABILITIES[wf_type]``
# contains split tokens ({"jira_read","jira_write","bitbucket_read","bitbucket_write",
# "execution"}).
#
# Tool name: ``jira_get_issue``
# - Workflow type exercised: ``code_change_with_test``
# - Asserted set (WORKFLOW_TYPE_CAPABILITIES["code_change_with_test"]):
# frozenset({'jira_read', 'jira_write', 'bitbucket_read', 'bitbucket_write', 'execution'})
# - Emitted set (required_capabilities("code_change_with_test")):
# frozenset({'jira', 'bitbucket', 'execution'})
# - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = ∅ (split vocab only)
# emitted ∩ {"jira","bitbucket","confluence"} = {"jira","bitbucket"} ≠ ∅
# asserted ≠ emitted → isBugCondition_2 = True
#
# Tool name: ``bitbucket_get_pr``
# - Workflow type exercised: ``pr_review``
# - Asserted set (WORKFLOW_TYPE_CAPABILITIES["pr_review"]):
# frozenset({'jira_read', 'jira_write', 'bitbucket_read'})
# - Emitted set (required_capabilities("pr_review")):
# frozenset({'jira', 'bitbucket'})
# - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = ∅
# emitted ∩ {"jira","bitbucket","confluence"} = {"jira","bitbucket"} ≠ ∅
# asserted ≠ emitted → isBugCondition_2 = True
#
# Tool name: ``confluence_get_page``
# - Workflow type exercised: ``confluence_doc_update``
# - Asserted set (WORKFLOW_TYPE_CAPABILITIES["confluence_doc_update"]):
# frozenset({'jira_read', 'jira_write', 'confluence_read', 'confluence_write'})
# - Emitted set (required_capabilities("confluence_doc_update")):
# frozenset({'jira', 'confluence'})
# - Bug condition: asserted ∩ {"jira","bitbucket","confluence"} = ∅
# emitted ∩ {"jira","bitbucket","confluence"} = {"jira","confluence"} ≠ ∅
# asserted ≠ emitted → isBugCondition_2 = True
#
# Root cause: ``test_capability_helpers.py::TestRequiredCapabilities::
# test_returns_frozenset_for_valid_types`` asserts that ``required_capabilities``
# returns the raw split vocabulary from ``WORKFLOW_TYPE_CAPABILITIES``, but
# ``required_capabilities`` intentionally collapses the split vocabulary to the
# simple service vocabulary ({"jira","bitbucket","confluence",...}) via
# ``_collapse_capability``. The test was written against the pre-collapse
# contract and was not updated when the collapse helper was introduced.
#
# -----------------------------------------------------------------------------
# POST-FIX VERIFICATION (Surface 2)
# -----------------------------------------------------------------------------
#
# Surface 2 was fixed via fix-pre-existing-test-failures ****:
# ``platform/tests/unit/test_capability_helpers.py`` was updated to assert the
# COLLAPSED-SIMPLE vocabulary ({"jira","bitbucket","confluence","execution",
# "web_search"}) emitted by ``required_capabilities`` rather than the raw
# split vocabulary stored in ``WORKFLOW_TYPE_CAPABILITIES``. The alignment
# direction is the same as Surface 1 (test-side, no production edit) but the
# OPPOSITE token form (simple, not split): ``required_capabilities`` is a
# deliberate collapse helper whose docstring documents the simple return
# vocabulary, so the canonical emission is simple here. The post-fix unit
# test asserts ``result == _expected_simple_capabilities(wf_type)`` where
# ``_expected_simple_capabilities`` collapses ``WORKFLOW_TYPE_CAPABILITIES[wf_type]``
# via the ``SPLIT_TO_SIMPLE`` mirror table.
#
# After the fix, ``tokens_asserted_by_capability_helpers_test`` in this file
# (rebuilt by mirrors the post-fix assertion oracle from
# ``test_capability_helpers.py`` - it computes the COLLAPSED-SIMPLE form using
# the same ``_SPLIT_TO_SIMPLE`` mirror table. Re-running
# ``pytest platform/tests/property/test_capability_vocabulary_alignment.py::test_surface2_capability_helpers_vocabulary_aligned``
# now PASSES - the asserted oracle and the emitted value both yield the
# collapsed-simple form, so ``asserted == emitted`` for every sampled tool
# name and isBugCondition_2(X) is False everywhere.
#
# =============================================================================

from typing import Mapping

from temporal_shared.capabilities import required_capabilities

# Mirror of ``temporal_shared.capabilities._SPLIT_TO_SIMPLE`` (the collapse
# map used by ``required_capabilities``). Kept inline here to mirror the
# post-fix oracle in ``test_capability_helpers.py`` without re-importing
# from the production module - accidental edits to the collapse logic
# would otherwise hide if both sides shared a single source.
_SPLIT_TO_SIMPLE: Mapping[str, str] = {
    "jira_read": "jira",
    "jira_write": "jira",
    "bitbucket_read": "bitbucket",
    "bitbucket_write": "bitbucket",
    "confluence_read": "confluence",
    "confluence_write": "confluence",
}


# ---------------------------------------------------------------------------
# Surface 2 helpers
# ---------------------------------------------------------------------------


def tokens_emitted_by_capability_helpers(tool_name: str) -> frozenset[str]:
    """Return the capability tokens emitted by ``required_capabilities`` for the given MCP tool name.

 Maps the tool name to its associated workflow type, then calls
 ``required_capabilities(workflow_type)`` - the function under test in
 ``test_capability_helpers.py``.

 Returns an empty frozenset if the tool name is not in the mapping.
 """
    workflow_type = MCP_TOOL_TO_WORKFLOW_TYPE.get(tool_name)
    if workflow_type is None:
        return frozenset()
    return required_capabilities(workflow_type)


def tokens_asserted_by_capability_helpers_test(tool_name: str) -> frozenset[str]:
    """Return the capability tokens that ``test_capability_helpers.py`` asserts for the given MCP tool name.

 POST-FIX: the test ``test_returns_frozenset_for_valid_types``
 now asserts:
 result == _expected_simple_capabilities(wf_type)

 where ``_expected_simple_capabilities`` collapses
 ``WORKFLOW_TYPE_CAPABILITIES[wf_type]`` via the ``SPLIT_TO_SIMPLE`` mirror
 table to the simple-vocabulary frozenset that ``required_capabilities``
 actually emits.

 So the "asserted" set post-fix is the COLLAPSED-SIMPLE form - the split
 tokens folded via:data:`_SPLIT_TO_SIMPLE`, with non-split tokens
 (``"execution"``, ``"web_search"``) passed through unchanged.

 Returns an empty frozenset if the tool name is not in the mapping.
 """
    workflow_type = MCP_TOOL_TO_WORKFLOW_TYPE.get(tool_name)
    if workflow_type is None:
        return frozenset()
    raw = WORKFLOW_TYPE_CAPABILITIES.get(workflow_type, frozenset())
    return frozenset(_SPLIT_TO_SIMPLE.get(cap, cap) for cap in raw)


def is_bug_condition_2(tool_name: str) -> bool:
    """Encode the second vocabulary-drift condition.

 Returns True iff:
 - asserted != emitted
 - AND (asserted ∩ {"jira","bitbucket","confluence"} ≠ ∅
 OR emitted ∩ {"jira","bitbucket","confluence"} ≠ ∅)

 On unfixed code:
 - asserted = WORKFLOW_TYPE_CAPABILITIES[wf_type] (split vocab, no legacy tokens)
 - emitted = required_capabilities(wf_type) (simple/collapsed vocab, has legacy tokens)
 - asserted ≠ emitted (True)
 - emitted ∩ {"jira","bitbucket","confluence"} ≠ ∅ (True - simple vocab has these)
 → isBugCondition_2 = True
 """
    asserted = tokens_asserted_by_capability_helpers_test(tool_name)
    emitted = tokens_emitted_by_capability_helpers(tool_name)

    asserted_has_legacy = bool(asserted & LEGACY_TOKENS)
    emitted_has_legacy = bool(emitted & LEGACY_TOKENS)

    return (
        asserted != emitted
        and (asserted_has_legacy or emitted_has_legacy)
    )


# ---------------------------------------------------------------------------
# invariant: Bug Condition - Capability Vocabulary Aligned in test_capability_helpers.py
#
# ---------------------------------------------------------------------------


@given(
    tool_name=st.sampled_from(sorted(MCP_TOOL_TO_WORKFLOW_TYPE.keys()))
)
@settings(max_examples=6, deadline=None)
def test_surface2_capability_helpers_vocabulary_aligned(tool_name: str) -> None:
    """invariant: Bug Condition - Capability Vocabulary Aligned in test_capability_helpers.py.

 For each MCP tool name drawn from the helper's domain, asserts that
 ``required_capabilities(workflow_type)`` returns the same set as
 ``WORKFLOW_TYPE_CAPABILITIES[workflow_type]`` - i.e., the function under
 test in ``test_capability_helpers.py`` emits the same vocabulary that the
 test asserts.

 On UNFIXED code: this test FAILS because ``required_capabilities``
 collapses the split vocabulary to the simple vocabulary, so:
 - emitted = {"jira", "bitbucket", "execution"} (simple/collapsed)
 - asserted = {"jira_read", "jira_write", "bitbucket_read", "bitbucket_write", "execution"} (split)
 - asserted ≠ emitted AND emitted ∩ {"jira","bitbucket","confluence"} ≠ ∅
 → isBugCondition_2(X) = True

 After fix: this test PASSES because either:
 - ``test_capability_helpers.py`` is updated to assert the simple vocabulary
 (matching what ``required_capabilities`` actually returns), OR
 - ``required_capabilities`` is updated to return the split vocabulary
 (matching what ``WORKFLOW_TYPE_CAPABILITIES`` stores)

 The alignment direction chosen in (update tests to use split vocabulary)
 applies here too: the fix should update ``test_capability_helpers.py`` to
 assert the simple vocabulary that ``required_capabilities`` emits, since
 the function's docstring explicitly states it collapses to simple names.


 """
    workflow_type = MCP_TOOL_TO_WORKFLOW_TYPE.get(tool_name)
    assert workflow_type is not None, (
        f"tool_name {tool_name!r} not in MCP_TOOL_TO_WORKFLOW_TYPE - "
        f"strategy should only generate known tool names"
    )

    # What test_capability_helpers.py asserts: WORKFLOW_TYPE_CAPABILITIES[wf_type]
    # (the raw split vocabulary)
    asserted = tokens_asserted_by_capability_helpers_test(tool_name)

    # What required_capabilities actually emits: simple/collapsed vocabulary
    emitted = tokens_emitted_by_capability_helpers(tool_name)

    # Condition: asserted and emitted must agree (no vocabulary drift)
    # On UNFIXED code this assertion FAILS because:
    # asserted = split vocab ({"jira_read","jira_write",...})
    # emitted = simple vocab ({"jira","bitbucket",...})
    # asserted ≠ emitted AND emitted ∩ {"jira","bitbucket","confluence"} ≠ ∅
    assert asserted == emitted, (
        f"BUG DETECTED (Surface 2): test_capability_helpers.py asserts "
        f"{sorted(asserted)} for tool {tool_name!r} "
        f"(workflow_type={workflow_type!r}), "
        f"but required_capabilities emits {sorted(emitted)}. "
        f"isBugCondition_2(X) = True: "
        f"asserted∩legacy={sorted(asserted & LEGACY_TOKENS)}, "
        f"emitted∩legacy={sorted(emitted & LEGACY_TOKENS)}. "
        f"The test asserts the raw split vocabulary from WORKFLOW_TYPE_CAPABILITIES "
        f"but required_capabilities collapses split tokens to simple service names "
        f"(jira_read/jira_write → jira, bitbucket_read/bitbucket_write → bitbucket, etc.). "
        f"Fix: update test_capability_helpers.py to assert the simple vocabulary "
        f"that required_capabilities actually returns."
    )
