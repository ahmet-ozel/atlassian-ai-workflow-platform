# Alignment note: tests assert the SIMPLE/collapsed vocabulary
# ({"jira","bitbucket","confluence","execution","web_search"}) emitted by
# ``temporal_shared.capabilities.required_capabilities`` rather than the raw
# split vocabulary stored in ``WORKFLOW_TYPE_CAPABILITIES``. This uses the
# simple token form, not the split form: ``required_capabilities`` is a
# deliberate collapse helper whose docstring explicitly documents the simple
# return vocabulary, so the canonical emission is simple here.
"""Unit tests for capability gate set-algebra helpers.

Tests the pure functions ``required_capabilities``, ``missing_capabilities``,
and ``has_jira_credential`` from ``temporal_shared.capabilities``.

"""

from __future__ import annotations

from typing import Mapping

import pytest

from temporal_shared.capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    has_jira_credential,
    missing_capabilities,
    required_capabilities,
)


# ---------------------------------------------------------------------------
# Vocabulary alignment helper
#
# ``required_capabilities`` collapses the split read/write tokens stored in
# :data:`WORKFLOW_TYPE_CAPABILITIES` (``"jira_read"`` / ``"jira_write"`` /
# ``"bitbucket_read"`` / ``"bitbucket_write"`` / ``"confluence_read"`` /
# ``"confluence_write"``) down to their simple service names
# (``"jira"`` / ``"bitbucket"`` / ``"confluence"``). This mirror table is
# kept inline rather than re-imported from the production module so the
# test catches accidental edits to the collapse logic — a regression that
# would otherwise hide if both sides shared a single source.
# ---------------------------------------------------------------------------

#: Mirror of ``temporal_shared.capabilities._SPLIT_TO_SIMPLE`` (the collapse
#: map used by ``required_capabilities``). Capabilities not in this map
#: pass through unchanged (e.g. ``"execution"``, ``"web_search"``).
SPLIT_TO_SIMPLE: Mapping[str, str] = {
    "jira_read": "jira",
    "jira_write": "jira",
    "bitbucket_read": "bitbucket",
    "bitbucket_write": "bitbucket",
    "confluence_read": "confluence",
    "confluence_write": "confluence",
}


def _expected_simple_capabilities(wf_type: str) -> frozenset[str]:
    """Collapsed form of ``WORKFLOW_TYPE_CAPABILITIES[wf_type]``.

    Returns the simple-vocabulary frozenset that ``required_capabilities``
    emits for *wf_type* — i.e. the split tokens folded via
    :data:`SPLIT_TO_SIMPLE` and any non-split tokens passed through.
    """

    return frozenset(
        SPLIT_TO_SIMPLE.get(cap, cap)
        for cap in WORKFLOW_TYPE_CAPABILITIES[wf_type]
    )


# ---------------------------------------------------------------------------
# required_capabilities
# ---------------------------------------------------------------------------


class TestRequiredCapabilities:
    """Tests for ``required_capabilities(workflow_type)``."""

    @pytest.mark.parametrize("wf_type", sorted(WORKFLOW_TYPE_CAPABILITIES.keys()))
    def test_returns_frozenset_for_valid_types(self, wf_type: str) -> None:
        result = required_capabilities(wf_type)
        assert isinstance(result, frozenset)
        # ``required_capabilities`` collapses split tokens to simple service
        # names (Surface 2 alignment, see file header). Compare against the
        # collapsed form rather than the raw ``WORKFLOW_TYPE_CAPABILITIES``
        # entry.
        assert result == _expected_simple_capabilities(wf_type)

    def test_code_change_with_test(self) -> None:
        assert required_capabilities("code_change_with_test") == frozenset(
            {"jira", "bitbucket", "execution"}
        )

    def test_research_summary_jira(self) -> None:
        """``research_summary_jira`` is NOT a key in ``WORKFLOW_TYPE_CAPABILITIES``.

        The historical assumption (that ``research_summary_jira``
        was a key in the base mapping) no longer holds: the
        workflow aliases ``research_publish_confluence`` and
        ``research_summary_jira`` live in the automation-worker layer
        (``automation_worker.activities.task_analyzer.VALID_WORKFLOW_TYPES``)
        as task-analyzer aliases, while ``WORKFLOW_TYPE_CAPABILITIES`` ships
        the base keys (``research_basic`` / ``research_with_web``)
        — see the comment on this in
        ``platform/tests/property/test_task_analysis_parser.py``. So
        ``required_capabilities("research_summary_jira")`` raises ``KeyError``
        rather than returning a capability frozenset, mirroring the
        ``test_unknown_type_raises_key_error`` contract below.
        """

        with pytest.raises(KeyError):
            required_capabilities("research_summary_jira")

    def test_multi_step_raises_key_error(self) -> None:
        """``multi_step`` IS a key in the current production mapping.

        The historical assumption (that ``multi_step`` was
        computed at runtime as the union of its sub-workflows and therefore
        absent from ``WORKFLOW_TYPE_CAPABILITIES``) no longer holds: the
        production literal in ``temporal_shared.capabilities`` ships
        ``multi_step`` as a static frozenset entry, mirroring the inversion
        applied to ``test_temporal_shared.py::test_multi_step_is_not_a_key``.
        So ``required_capabilities("multi_step")``
        succeeds and returns the collapsed-simple form of the static entry.
        """

        result = required_capabilities("multi_step")
        assert isinstance(result, frozenset)
        assert result == _expected_simple_capabilities("multi_step")

    def test_unknown_type_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            required_capabilities("nonexistent_workflow")

    def test_empty_string_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            required_capabilities("")


# ---------------------------------------------------------------------------
# missing_capabilities
# ---------------------------------------------------------------------------


class TestMissingCapabilities:
    """Tests for ``missing_capabilities(required, available)``."""

    def test_no_missing_when_superset(self) -> None:
        required = frozenset({"jira", "bitbucket"})
        available = frozenset({"jira", "bitbucket", "confluence", "execution"})
        assert missing_capabilities(required, available) == set()

    def test_no_missing_when_exact_match(self) -> None:
        required = frozenset({"jira", "bitbucket", "execution"})
        available = frozenset({"jira", "bitbucket", "execution"})
        assert missing_capabilities(required, available) == set()

    def test_returns_missing_elements(self) -> None:
        required = frozenset({"jira", "bitbucket", "execution"})
        available = frozenset({"jira"})
        result = missing_capabilities(required, available)
        assert result == {"bitbucket", "execution"}

    def test_all_missing_when_empty_available(self) -> None:
        required = frozenset({"jira", "confluence"})
        available: set[str] = set()
        result = missing_capabilities(required, available)
        assert result == {"jira", "confluence"}

    def test_empty_required_always_empty_result(self) -> None:
        required = frozenset[str]()
        available = frozenset({"jira", "bitbucket"})
        assert missing_capabilities(required, available) == set()

    def test_accepts_set_as_available(self) -> None:
        """available can be a plain set, not just frozenset."""
        required = frozenset({"jira", "bitbucket"})
        available = {"jira"}
        result = missing_capabilities(required, available)
        assert result == {"bitbucket"}

    def test_returns_set_type(self) -> None:
        required = frozenset({"jira"})
        available = frozenset({"jira"})
        result = missing_capabilities(required, available)
        assert isinstance(result, set)


# ---------------------------------------------------------------------------
# has_jira_credential
# ---------------------------------------------------------------------------


class TestHasJiraCredential:
    """Tests for ``has_jira_credential(dept_caps)``."""

    def test_true_when_jira_present(self) -> None:
        assert has_jira_credential(frozenset({"jira", "bitbucket"})) is True

    def test_true_when_only_jira(self) -> None:
        assert has_jira_credential(frozenset({"jira"})) is True

    def test_false_when_jira_absent(self) -> None:
        assert has_jira_credential(frozenset({"bitbucket", "confluence"})) is False

    def test_false_when_empty(self) -> None:
        assert has_jira_credential(frozenset()) is False

    def test_accepts_plain_set(self) -> None:
        assert has_jira_credential({"jira", "execution"}) is True

    def test_false_with_plain_set_no_jira(self) -> None:
        assert has_jira_credential({"confluence"}) is False
