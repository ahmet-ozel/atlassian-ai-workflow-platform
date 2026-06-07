"""Unit tests for ``description_override`` helper module.

The helper bridges the analyser's :class:`TaskAnalysisResult` (rich,
numeric confidence, dict-shaped output actions) and the legacy
:class:`temporal_shared.messages.LlmAnalysisResult` (literal-confidence,
:class:`OutputAction`-tuple) consumed by the existing capability-gate
+ branch-rule + dispatch pipeline.

These tests cover:

* :func:`build_description_override` - projection of analyser fields
  into the immutable :class:`DescriptionOverride` envelope.
* :func:`to_llm_analysis_result` - confidence-mapping rules and
  output-action coercion.

Validates Requirements: R5.1-R5.10 (analyser → workflow_type), R11.1-
R11.7 (description override merge).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


# pylint: disable=wrong-import-position
from automation_worker.activities.task_analyzer import (  # noqa: E402
    TaskAnalysisResult,
)
from automation_worker.workflows.description_override import (  # noqa: E402
    DescriptionOverride,
    build_description_override,
    to_llm_analysis_result,
)
from temporal_shared.messages import (  # noqa: E402
    LlmAnalysisResult,
    OutputAction,
)


def _make_result(
    *,
    workflow_type: str = "code_change_with_test",
    confidence: float = 0.9,
    cleanup_policy: str = "on_success",
    timeout_seconds: int | None = 1800,
    web_search: bool = False,
    repo: str | None = "org/foo",
    branch: str | None = "develop",
    output_actions: list[dict] | None = None,
    missing_fields: list[str] | None = None,
    reasoning: str = "",
    source: str = "llm_analysis",
) -> TaskAnalysisResult:
    """Build a :class:`TaskAnalysisResult` with sensible defaults."""

    return TaskAnalysisResult(
        accepted=True,
        workflow_type=workflow_type,
        needs_ssh=False,
        needs_docker=False,
        execution_command="pytest -q",
        repo=repo,
        branch=branch,
        cleanup_policy=cleanup_policy,
        timeout_seconds=timeout_seconds,
        web_search=web_search,
        output_actions=output_actions or [],
        confidence=confidence,
        missing_fields=missing_fields or [],
        reasoning=reasoning,
        source=source,  # type: ignore[arg-type]
        status="ready",
        error=None,
        downgraded=False,
    )


# ===========================================================================
# 1. ``build_description_override``
# ===========================================================================


class TestBuildDescriptionOverride:
    def test_simple_fields_pass_through(self) -> None:
        result = _make_result(
            workflow_type="pr_review",
            cleanup_policy="never",
            timeout_seconds=900,
            web_search=True,
            repo="acme/widget",
            branch="release/1.2",
        )
        override = build_description_override(result)

        assert isinstance(override, DescriptionOverride)
        assert override.workflow_type == "pr_review"
        assert override.cleanup_policy == "never"
        assert override.timeout_seconds == 900
        assert override.web_search is True
        assert override.target_repo == "acme/widget"
        assert override.target_branch == "release/1.2"

    def test_empty_workflow_type_becomes_empty_string(self) -> None:
        # Defensive - the workflow rejects empty workflow_type before
        # calling the builder, but the helper must not crash on None.
        result = _make_result(workflow_type="")
        override = build_description_override(result)
        assert override.workflow_type == ""

    def test_output_actions_normalised_to_tuple_of_pairs(self) -> None:
        result = _make_result(
            output_actions=[
                {
                    "type": "jira_comment",
                    "params": {"body": "Done", "issue_key": "PAY-1"},
                },
                {
                    "type": "bitbucket_pr",
                    "params": {"branch": "feature/x"},
                },
            ]
        )
        override = build_description_override(result)

        assert len(override.output_actions) == 2
        kinds = [k for k, _ in override.output_actions]
        assert kinds == ["jira_comment", "bitbucket_create_pr"]

        first_kind, first_params = override.output_actions[0]
        assert first_kind == "jira_comment"
        # Sorted keys → deterministic replay-safe payload.
        assert first_params == (
            ("body", "Done"),
            ("issue_key", "PAY-1"),
        )

    def test_action_without_type_is_skipped(self) -> None:
        result = _make_result(
            output_actions=[
                {"type": "jira_comment", "params": {"body": "ok"}},
                {"params": {"body": "missing type"}},  # invalid
                {"type": "", "params": {}},  # empty type
                "not a dict",  # invalid
            ]
        )
        override = build_description_override(result)
        assert len(override.output_actions) == 1

    def test_missing_params_default_to_empty(self) -> None:
        result = _make_result(
            output_actions=[{"type": "jira_comment"}],  # no params key
        )
        override = build_description_override(result)
        kind, params = override.output_actions[0]
        assert kind == "jira_comment"
        assert params == ()

    def test_override_is_immutable(self) -> None:
        # ``frozen=True`` makes mutation raise FrozenInstanceError.
        result = _make_result()
        override = build_description_override(result)
        with pytest.raises(Exception):
            override.timeout_seconds = 60  # type: ignore[misc]


# ===========================================================================
# 2. ``to_llm_analysis_result``
# ===========================================================================


class TestToLlmAnalysisResult:
    def test_high_confidence_maps_to_high(self) -> None:
        result = _make_result(confidence=0.95)
        llm = to_llm_analysis_result(result)
        assert isinstance(llm, LlmAnalysisResult)
        assert llm.confidence == "high"

    def test_boundary_high_at_0_85(self) -> None:
        result = _make_result(confidence=0.85)
        assert to_llm_analysis_result(result).confidence == "high"

    def test_just_below_high_maps_to_medium(self) -> None:
        result = _make_result(confidence=0.84)
        assert to_llm_analysis_result(result).confidence == "medium"

    def test_boundary_medium_at_0_7(self) -> None:
        result = _make_result(confidence=0.7)
        assert to_llm_analysis_result(result).confidence == "medium"

    def test_just_below_threshold_maps_to_low(self) -> None:
        result = _make_result(confidence=0.69)
        assert to_llm_analysis_result(result).confidence == "low"

    def test_zero_confidence_maps_to_low(self) -> None:
        result = _make_result(confidence=0.0)
        assert to_llm_analysis_result(result).confidence == "low"

    def test_workflow_type_passes_through(self) -> None:
        result = _make_result(workflow_type="confluence_doc_create")
        llm = to_llm_analysis_result(result)
        assert llm.workflow_type == "confluence_doc_create"

    def test_repo_branch_pass_through(self) -> None:
        result = _make_result(repo="acme/svc", branch="main")
        llm = to_llm_analysis_result(result)
        assert llm.target_repo == "acme/svc"
        assert llm.target_branch == "main"

    def test_missing_fields_become_needs_info_questions(self) -> None:
        result = _make_result(
            missing_fields=["repo", "branch"],
        )
        llm = to_llm_analysis_result(result)
        assert llm.needs_info_questions == ("repo", "branch")

    def test_reasoning_becomes_rationale(self) -> None:
        result = _make_result(reasoning="LLM explained its choice")
        llm = to_llm_analysis_result(result)
        assert llm.rationale == "LLM explained its choice"

    def test_output_actions_become_OutputAction_tuple(self) -> None:
        result = _make_result(
            output_actions=[
                {
                    "type": "jira_comment",
                    "params": {"body": "Done"},
                },
            ],
        )
        llm = to_llm_analysis_result(result)
        assert len(llm.output_actions) == 1
        action = llm.output_actions[0]
        assert isinstance(action, OutputAction)
        assert action.kind == "jira_comment"
        assert action.severity == "critical"
        assert action.payload == (("body", "Done"),)

    def test_empty_output_actions_yields_empty_tuple(self) -> None:
        result = _make_result(output_actions=[])
        llm = to_llm_analysis_result(result)
        assert llm.output_actions == ()

    def test_token_usage_defaults_to_zero(self) -> None:
        # The analyser does not surface token_usage; keep the default
        # so audit / cost tracking treats it as unknown.
        result = _make_result()
        llm = to_llm_analysis_result(result)
        assert llm.token_usage == 0
