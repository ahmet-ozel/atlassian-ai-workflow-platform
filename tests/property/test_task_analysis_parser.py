"""Tests for the LLM task-analysis JSON parser.

For any LLM JSON response, the parser
(``platform/workers/agent-runner-worker/src/prompts/parser.py``,
:func:`parse_task_analysis`, raising :class:`TaskAnalysisError`) satisfies:

1. **Successful parse implies whitelist** — any output of ``parse_task_analysis``
   has ``workflow_type ∈ WORKFLOW_TYPE_CAPABILITIES``.

2. **Whitelist enforcement (negative)** — a ``workflow_type`` string that is
   not in ``WORKFLOW_TYPE_CAPABILITIES.keys()`` raises
   :class:`TaskAnalysisError`.

3. **Required-field enforcement** — when any of the workflow-type-specific
   required fields is missing, the parser raises
   :class:`TaskAnalysisError`. The required-field list is:

   * ``code_change_with_test`` / ``code_change_commit_only`` / ``pr_review`` /
     ``remote_ssh_test_only``: ``{workflow_type, confidence, output_actions,
     target_repo, target_branch}``
   * ``confluence_doc_create`` / ``confluence_doc_update``: ``{workflow_type,
     confidence, output_actions, target_lang}``
   * ``research_basic`` / ``research_with_web``: ``{workflow_type,
     confidence, output_actions}``
   * ``multi_step``: ``{workflow_type, confidence, output_actions, children}``
   * ``noop_test``: ``{workflow_type, confidence, output_actions}``

4. **Round-trip determinism** — for any payload that parses successfully,
   ``parse_task_analysis(format_task_analysis(t))`` returns an equivalent
   :class:`TaskAnalysis`.

5. **Draft coercion** — any ``bitbucket_pr`` action has its ``draft``
   field coerced to ``True``.

The :class:`TaskAnalysisError` symbol is the project-local equivalent of
``TaskAnalysisParseError``. :func:`parse_task_analysis` lives at
``platform/workers/agent-runner-worker/src/prompts/parser.py``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap for worker imports
#
# The agent-runner-worker package is laid out so the ``prompts`` and
# ``activities`` modules are imported as ``src.prompts`` / ``src.activities``
# from the worker root.  We mirror the same path setup the worker's own
# unit tests use so the parser module can be imported from this property
# test without installing the worker as a package.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKER_ROOT = _PLATFORM_ROOT / "workers" / "agent-runner-worker"

for _path in (
    _WORKER_ROOT,
    _WORKER_ROOT / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    _str = str(_path)
    if _path.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from src.prompts.parser import (  # noqa: E402
    OutputAction,
    TaskAnalysis,
    TaskAnalysisError,
    format_task_analysis,
    parse_task_analysis,
)
from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES  # noqa: E402

# ``TaskAnalysisParseError`` is the spec-name; the implementation file
# uses ``TaskAnalysisError``. Alias for forward-compatibility with
# ``temporal_shared.task_analysis.TaskAnalysisParseError``.
TaskAnalysisParseError = TaskAnalysisError


# ---------------------------------------------------------------------------
# Required-field map
# ---------------------------------------------------------------------------

#: Per-workflow-type required-field map.  ``confidence`` and
#: ``output_actions`` are required for every workflow type; the entries
#: below list the *additional* type-specific fields.
#:
#: This table follows the capability key set so the assertion against
#: :data:`WORKFLOW_TYPE_CAPABILITIES` stays in sync.
_TYPE_SPECIFIC_REQUIRED: Mapping[str, frozenset[str]] = {
    "code_change_with_test":   frozenset({"target_repo", "target_branch"}),
    "code_change_commit_only": frozenset({"target_repo", "target_branch"}),
    "pr_review":               frozenset({"target_repo", "target_branch"}),
    "remote_ssh_test_only":    frozenset({"target_repo", "target_branch"}),
    "confluence_doc_create":   frozenset({"target_lang"}),
    "confluence_doc_update":   frozenset({"target_lang"}),
    "research_basic":          frozenset(),
    "research_with_web":       frozenset(),
    "multi_step":              frozenset({"children"}),
    "noop_test":               frozenset(),
}

# Sanity: every key of the type-specific table is a workflow type.  This
# guards against the property test silently shrinking when the
# capability mapping changes shape.
assert set(_TYPE_SPECIFIC_REQUIRED.keys()) == set(WORKFLOW_TYPE_CAPABILITIES.keys()), (
    "Required-field map is out of sync with WORKFLOW_TYPE_CAPABILITIES."
)

#: Universal required fields — every workflow_type must carry these.
_ALWAYS_REQUIRED: frozenset[str] = frozenset({
    "workflow_type", "confidence", "output_actions",
})


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Valid workflow types from the capability mapping.
valid_workflow_types = st.sampled_from(sorted(WORKFLOW_TYPE_CAPABILITIES.keys()))

#: Valid confidence levels.
valid_confidences = st.sampled_from(["high", "medium", "low"])

#: Non-low confidence levels (high or medium).
non_low_confidences = st.sampled_from(["high", "medium"])

#: Strategy for generating a non-empty question string.
non_empty_questions = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
    min_size=1,
    max_size=200,
).filter(lambda s: s.strip())

#: Strategy for valid string identifiers (repo / branch / topic / lang).
valid_identifier_text = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="-_/"),
    min_size=1,
    max_size=40,
)

#: Valid action types accepted by the parser.
valid_action_types = st.sampled_from([
    "jira_comment",
    "bitbucket_pr",
    "bitbucket_commit",
    "confluence_page",
])

#: A simple payload dict (non-bitbucket_pr).
simple_payload = st.fixed_dictionaries(
    {},
    optional={
        "body": st.text(min_size=0, max_size=100),
        "title": st.text(min_size=0, max_size=100),
    },
)

#: A bitbucket_pr payload (with arbitrary draft input).
bitbucket_pr_payload = st.fixed_dictionaries(
    {},
    optional={
        "title": st.text(min_size=1, max_size=100),
        "description": st.text(min_size=0, max_size=200),
        "draft": st.one_of(st.booleans(), st.none(), st.text(min_size=0, max_size=10)),
    },
)


@st.composite
def output_actions_strategy(draw: st.DrawFn) -> list[dict]:
    """Generate a non-empty list of output action dicts."""
    count = draw(st.integers(min_value=1, max_value=5))
    actions = []
    for _ in range(count):
        action_type = draw(valid_action_types)
        if action_type == "bitbucket_pr":
            payload = draw(bitbucket_pr_payload)
        else:
            payload = draw(simple_payload)
        actions.append({"type": action_type, "payload": payload})
    return actions


@st.composite
def valid_task_analysis_dict(draw: st.DrawFn) -> dict:
    """Generate a valid TaskAnalysis dict that should parse successfully.

    The dict carries the *core* required fields plus the
    workflow-type-specific extras drawn from
    :data:`_TYPE_SPECIFIC_REQUIRED`, so it is a positive example for the
    required-field contract.
    """
    confidence = draw(valid_confidences)
    workflow_type = draw(valid_workflow_types)
    output_actions = draw(output_actions_strategy())

    result: dict[str, Any] = {
        "workflow_type": workflow_type,
        "output_actions": output_actions,
        "confidence": confidence,
    }

    if confidence == "low":
        result["needs_info_question"] = draw(non_empty_questions)
    else:
        result["needs_info_question"] = None

    # Type-specific extras.
    extras = _TYPE_SPECIFIC_REQUIRED[workflow_type]
    if "target_repo" in extras:
        result["target_repo"] = draw(valid_identifier_text)
    if "target_branch" in extras:
        result["target_branch"] = draw(valid_identifier_text)
    if "target_lang" in extras:
        result["target_lang"] = draw(st.sampled_from(["tr", "en"]))
    if "children" in extras:
        # children for multi_step — minimal schema; we only need the
        # field to be present.
        result["children"] = [
            {"workflow_type": draw(st.sampled_from(
                [k for k in WORKFLOW_TYPE_CAPABILITIES if k != "multi_step"]
            ))}
            for _ in range(draw(st.integers(min_value=1, max_value=3)))
        ]

    return result


# ---------------------------------------------------------------------------
# Successful parse implies workflow_type ∈ keys
# ---------------------------------------------------------------------------


class TestSuccessfulParseInWhitelist:
    """Any successful parse yields ``workflow_type ∈ WORKFLOW_TYPE_CAPABILITIES``."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_parse_yields_whitelisted_workflow_type(self, data: dict) -> None:
        result = parse_task_analysis(data)
        assert result.workflow_type in WORKFLOW_TYPE_CAPABILITIES, (
            f"parser returned workflow_type={result.workflow_type!r} "
            f"which is not in the capability whitelist."
        )

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_parse_via_json_string_equivalent(self, data: dict) -> None:
        """JSON string and dict inputs produce equivalent results."""
        from_dict = parse_task_analysis(data)
        from_str = parse_task_analysis(json.dumps(data))
        assert from_dict.workflow_type == from_str.workflow_type
        assert from_dict.confidence == from_str.confidence
        assert from_dict.output_actions == from_str.output_actions


# ---------------------------------------------------------------------------
# workflow_type ∉ keys raises TaskAnalysisParseError
# ---------------------------------------------------------------------------


class TestWorkflowTypeWhitelistRejected:
    """Any ``workflow_type`` not in the capability mapping is rejected."""

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
    @given(
        random_type=st.text(min_size=1, max_size=50).filter(
            lambda s: s and s not in WORKFLOW_TYPE_CAPABILITIES
        ),
        data=valid_task_analysis_dict(),
    )
    def test_invalid_workflow_type_raises(
        self, random_type: str, data: dict
    ) -> None:
        data["workflow_type"] = random_type
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_missing_workflow_type_raises(self, data: dict) -> None:
        data.pop("workflow_type", None)
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_null_workflow_type_raises(self, data: dict) -> None:
        data["workflow_type"] = None
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)


# ---------------------------------------------------------------------------
# Required-field enforcement (universal fields)
# ---------------------------------------------------------------------------


class TestRequiredFieldsAlwaysEnforced:
    """The universal required-field set
    ``{workflow_type, confidence, output_actions}`` is enforced for every
    workflow type — removing any one of them raises
    :class:`TaskAnalysisParseError`.
    """

    @settings(max_examples=80, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        data=valid_task_analysis_dict(),
        field_to_drop=st.sampled_from(sorted(_ALWAYS_REQUIRED)),
    )
    def test_dropping_universal_field_raises(
        self, data: dict, field_to_drop: str
    ) -> None:
        data.pop(field_to_drop, None)
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_empty_output_actions_raises(self, data: dict) -> None:
        data["output_actions"] = []
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)

    @settings(max_examples=50, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_invalid_confidence_raises(self, data: dict) -> None:
        data["confidence"] = "definitely-yes"
        with pytest.raises(TaskAnalysisParseError):
            parse_task_analysis(data)


# ---------------------------------------------------------------------------
# Type-specific required fields
# ---------------------------------------------------------------------------


def _workflow_types_with_extras() -> list[str]:
    """Return the workflow types that have at least one type-specific field."""
    return sorted(k for k, v in _TYPE_SPECIFIC_REQUIRED.items() if v)


class TestTypeSpecificRequiredFields:
    """Each workflow_type defines its own additional required-field set; if
    any one of those fields is missing the parser must raise
    :class:`TaskAnalysisParseError`.

    The canonical field list is:

    ============================  =============================================
    workflow_type                 required additional fields
    ============================  =============================================
    code_change_with_test         target_repo, target_branch
    code_change_commit_only       target_repo, target_branch
    pr_review                     target_repo, target_branch
    remote_ssh_test_only          target_repo, target_branch
    confluence_doc_create         target_lang
    confluence_doc_update         target_lang
    multi_step                    children
    research_basic                (none)
    research_with_web             (none)
    noop_test                     (none)
    ============================  =============================================
    """

    @pytest.mark.parametrize("workflow_type", _workflow_types_with_extras())
    @settings(max_examples=30, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_dropping_type_specific_field_raises(
        self, workflow_type: str, data: dict
    ) -> None:
        """Dropping any required type-specific field raises an error."""
        # Pin the input to the workflow_type under test so the strategy
        # generates a payload that already carries the extras.
        data["workflow_type"] = workflow_type
        # Pin confidence to a non-low value so the test is not also
        # triggered by the ``needs_info_question`` rule.
        data["confidence"] = "high"
        data.pop("needs_info_question", None)
        # Re-populate type-specific fields so dropping ONE leaves the
        # other present (otherwise the input may already be invalid for
        # an unrelated reason).
        for field_name in _TYPE_SPECIFIC_REQUIRED[workflow_type]:
            if field_name == "target_repo":
                data["target_repo"] = "repo-x"
            elif field_name == "target_branch":
                data["target_branch"] = "main"
            elif field_name == "target_lang":
                data["target_lang"] = "tr"
            elif field_name == "children":
                data["children"] = [{"workflow_type": "noop_test"}]

        # Parsing the *complete* payload must succeed.
        ok = parse_task_analysis(dict(data))
        assert ok.workflow_type == workflow_type

        # Drop each extra in turn; parser must reject.
        for field_name in _TYPE_SPECIFIC_REQUIRED[workflow_type]:
            broken = dict(data)
            broken.pop(field_name, None)
            with pytest.raises(TaskAnalysisParseError):
                parse_task_analysis(broken)


# ---------------------------------------------------------------------------
# Round-trip stability for valid inputs
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Successful parses round-trip through :func:`format_task_analysis`
    without information loss.
    """

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=valid_task_analysis_dict())
    def test_parse_format_roundtrip(self, data: dict) -> None:
        parsed = parse_task_analysis(data)
        reparsed = parse_task_analysis(format_task_analysis(parsed))

        assert reparsed.workflow_type == parsed.workflow_type
        assert reparsed.confidence == parsed.confidence
        assert reparsed.target_repo == parsed.target_repo
        assert reparsed.target_branch == parsed.target_branch
        assert reparsed.needs_info_question == parsed.needs_info_question
        assert len(reparsed.output_actions) == len(parsed.output_actions)
        for a, b in zip(parsed.output_actions, reparsed.output_actions):
            assert a.type == b.type
            assert a.payload == b.payload


# ---------------------------------------------------------------------------
# bitbucket_pr draft coercion
# ---------------------------------------------------------------------------


class TestDraftCoercion:
    """A ``bitbucket_pr`` action's ``draft`` field is always ``True`` after
    parsing, regardless of input value.
    """

    @settings(max_examples=100, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(
        data=valid_task_analysis_dict(),
        draft_value=st.one_of(
            st.just(True),
            st.just(False),
            st.none(),
            st.text(min_size=0, max_size=10),
            st.integers(min_value=0, max_value=100),
        ),
    )
    def test_bitbucket_pr_draft_coerced_to_true(
        self, data: dict, draft_value: object
    ) -> None:
        data["output_actions"] = [{
            "type": "bitbucket_pr",
            "payload": {"title": "T", "draft": draft_value},
        }]
        parsed = parse_task_analysis(data)
        for action in parsed.output_actions:
            if action.type == "bitbucket_pr":
                assert action.payload["draft"] is True
