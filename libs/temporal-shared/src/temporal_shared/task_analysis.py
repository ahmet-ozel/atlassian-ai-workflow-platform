"""Shared LLM task-analysis JSON parser.

This module is the **single source of truth** for the LLM
task-analysis JSON parser.  The parser validates the LLM-produced
JSON into a :class:`temporal_shared.messages.LlmAnalysisResult` and
rejects payloads that violate the workflow-type-specific required
fields.

Validation contract:

* Universal required fields - every payload must carry
  ``{workflow_type, confidence, output_actions}``.
* Workflow-type-specific required fields - see
  :data:`_TYPE_SPECIFIC_REQUIRED`.
* ``workflow_type`` must be a key of
  :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`.
* ``confidence`` must be one of ``{"high", "medium", "low"}``.
* ``output_actions`` must be a non-empty list.

The pure ``OutputAction`` and ``LlmAnalysisResult`` dataclasses live
in :mod:`temporal_shared.messages` so the parser does not introduce
a parallel type hierarchy.

Public API:

* :class:`TaskAnalysisParseError` - ``ValueError`` subclass raised
  for any validation failure.  Re-exported from
  :mod:`temporal_shared` so callers can write
  ``from temporal_shared import TaskAnalysisParseError``.
* :func:`parse_llm_analysis` - accepts either a parsed dict or a
  raw JSON string and returns a validated
  :class:`LlmAnalysisResult`.
"""

from __future__ import annotations

import json
from typing import Any, Final, Mapping

from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES
from temporal_shared.messages import LlmAnalysisResult, OutputAction

__all__ = [
    "TaskAnalysisParseError",
    "parse_llm_analysis",
]


# ---------------------------------------------------------------------------
# Constants - closed vocabularies
# ---------------------------------------------------------------------------

#: Confidence vocabulary.
_VALID_CONFIDENCES: Final[frozenset[str]] = frozenset(
    {"high", "medium", "low"}
)

#: Output-action types accepted by the parser.  Mirrors the kinds
#: defined in :data:`temporal_shared.messages.OutputActionKind` plus
#: the legacy ``bitbucket_pr`` / ``bitbucket_commit`` / ``confluence_page``
#: tags emitted by the worker's ``prompts.parser`` module - both
#: vocabularies are accepted so the shared module can replace the
#: worker-local parser without a coordinated migration.
_VALID_ACTION_TYPES: Final[frozenset[str]] = frozenset(
    {
        # Worker-local vocabulary (existing parser at
        # ``workers/agent-runner-worker/src/prompts/parser.py``).
        "jira_comment",
        "bitbucket_pr",
        "bitbucket_commit",
        "confluence_page",
        # ``temporal_shared.messages.OutputActionKind`` vocabulary.
        "jira_attachment",
        "bitbucket_create_pr",
        "confluence_create_page",
        "confluence_update_page",
        "slack_notify",
        "email_notify",
    }
)

#: Per-workflow-type required-field map.
#:
#: ``workflow_type``, ``confidence`` and ``output_actions`` are
#: universal required fields; the entries below list the *additional*
#: type-specific fields each workflow_type must carry on top of the
#: universal set.  The shape is identical to
#: ``_TYPE_SPECIFIC_REQUIRED`` in
#: ``platform/workers/agent-runner-worker/src/prompts/parser.py``.
_TYPE_SPECIFIC_REQUIRED: Final[Mapping[str, frozenset[str]]] = {
    "code_change_with_test":      frozenset({"target_repo", "target_branch"}),
    "code_change_commit_only":    frozenset({"target_repo", "target_branch"}),
    "pr_review":                  frozenset({"target_repo", "target_branch"}),
    "remote_ssh_test_only":       frozenset({"target_repo", "target_branch"}),
    "confluence_doc_create":      frozenset({"target_lang"}),
    "confluence_doc_update":      frozenset({"target_lang"}),
    "research_basic":             frozenset(),
    "research_with_web":          frozenset(),
    "multi_step":                 frozenset({"children"}),
    "noop_test":                  frozenset(),
    # EK3 additions - kept in lock-step with WORKFLOW_TYPE_CAPABILITIES
    # and ``task_analyzer.VALID_WORKFLOW_TYPES``.
    # script_execute runs a one-off script on the SSH runner; like
    # remote_ssh_test_only it needs a target repo/branch so the runner
    # knows where to check out the script source.
    "script_execute":             frozenset({"target_repo", "target_branch"}),
    # research_publish_confluence is a research workflow whose output
    # is published to Confluence - caller must say which language /
    # space context to use.
    "research_publish_confluence": frozenset({"target_lang"}),
    # research_summary_jira posts the research summary back on the
    # triggering Jira issue - no extra required fields beyond the
    # universal set.
    "research_summary_jira":      frozenset(),
}

# Sanity: the type-specific table covers exactly the workflow-type
# whitelist.  Catches drift between the two single-source-of-truth
# constants at import time rather than at first parse.
assert set(_TYPE_SPECIFIC_REQUIRED.keys()) == set(
    WORKFLOW_TYPE_CAPABILITIES.keys()
), (
    "Required-field map is out of sync with WORKFLOW_TYPE_CAPABILITIES; "
    "update _TYPE_SPECIFIC_REQUIRED in temporal_shared.task_analysis."
)

#: Universal required fields - every workflow_type must carry these
#: in addition to the workflow-type-specific extras.
_ALWAYS_REQUIRED: Final[frozenset[str]] = frozenset(
    {"workflow_type", "confidence", "output_actions"}
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskAnalysisParseError(ValueError):
    """Raised when LLM task-analysis output fails validation.

    Subclasses :class:`ValueError` so callers can catch it generically
    when they only care about "bad input" - and so the worker's
    legacy ``TaskAnalysisError`` (also a ``ValueError``) can alias to
    this name without breaking ``except`` blocks.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_draft_true(action_payload: dict[str, Any]) -> dict[str, Any]:
    """Coerce ``draft`` to ``True`` for ``bitbucket_pr`` actions.

    PR actions are always opened as drafts. Pure: returns a fresh
    dict; the input is left untouched.
    """

    coerced = dict(action_payload)
    coerced["draft"] = True
    return coerced


def _parse_output_action(raw: Any, index: int) -> OutputAction:
    """Parse one entry of the ``output_actions`` list.

    The result is a frozen :class:`OutputAction` from
    :mod:`temporal_shared.messages`.  Severity is inferred from the
    action kind via the
    :data:`temporal_shared.messages.CRITICAL_OUTPUT_ACTION_KINDS` /
    :data:`BEST_EFFORT_OUTPUT_ACTION_KINDS` membership; legacy kinds
    (``bitbucket_pr`` / ``bitbucket_commit`` / ``confluence_page``)
    that the new partition table does not classify default to
    ``"critical"`` - the conservative choice for a side-effect that
    writes to a tracked system of record.
    """

    if not isinstance(raw, dict):
        raise TaskAnalysisParseError(
            f"output_actions[{index}]: expected dict, got "
            f"{type(raw).__name__}"
        )

    action_type = raw.get("type")
    if not isinstance(action_type, str) or not action_type:
        raise TaskAnalysisParseError(
            f"output_actions[{index}]: 'type' must be a non-empty string"
        )

    payload = raw.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TaskAnalysisParseError(
            f"output_actions[{index}]: 'payload' must be a dict, got "
            f"{type(payload).__name__}"
        )

    if action_type == "bitbucket_pr":
        payload = _coerce_draft_true(payload)

    # Lazy import to avoid a circular dependency at module load when
    # the messages module is still being initialised.
    from temporal_shared.messages import (
        BEST_EFFORT_OUTPUT_ACTION_KINDS,
        CRITICAL_OUTPUT_ACTION_KINDS,
    )

    if action_type in CRITICAL_OUTPUT_ACTION_KINDS:
        severity: str = "critical"
    elif action_type in BEST_EFFORT_OUTPUT_ACTION_KINDS:
        severity = "best_effort"
    else:
        # Legacy / unclassified kind - default to ``critical`` so a
        # silent failure of an un-tagged action can never be swept
        # under "best_effort".
        severity = "critical"

    return OutputAction(
        kind=action_type,  # type: ignore[arg-type]
        severity=severity,  # type: ignore[arg-type]
        payload=tuple(payload.items()),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_llm_analysis(payload: dict[str, Any] | str) -> LlmAnalysisResult:
    """Parse and validate the LLM task-analysis JSON payload.

    Accepts either a parsed ``dict`` or a raw JSON ``str``.  Returns
    a frozen :class:`LlmAnalysisResult` with the workflow-type
    whitelist enforced and the workflow-type-specific required
    fields validated.

    Raises
    ------
    TaskAnalysisParseError
        For any of:

        * Malformed JSON (when ``payload`` is a string).
        * Wrong top-level type (anything other than ``dict`` or
          ``str``).
        * Missing or non-string ``workflow_type``.
        * ``workflow_type`` ∉ :data:`WORKFLOW_TYPE_CAPABILITIES`.
        * Missing or invalid ``confidence``.
        * Missing or empty ``output_actions``.
        * Missing workflow-type-specific required field.
        * Malformed ``output_actions`` entries.
    """

    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TaskAnalysisParseError(f"Invalid JSON: {exc}") from exc
    elif isinstance(payload, dict):
        data = payload
    else:
        raise TaskAnalysisParseError(
            f"payload must be dict or str, got {type(payload).__name__}"
        )

    # --- workflow_type whitelist ----------------------------------------
    workflow_type = data.get("workflow_type")
    if not isinstance(workflow_type, str):
        raise TaskAnalysisParseError("'workflow_type' must be a string")
    if workflow_type not in WORKFLOW_TYPE_CAPABILITIES:
        allowed = ", ".join(sorted(WORKFLOW_TYPE_CAPABILITIES.keys()))
        raise TaskAnalysisParseError(
            f"Invalid workflow_type {workflow_type!r}. "
            f"Must be one of: {allowed}"
        )

    # --- confidence -----------------------------------------------------
    confidence = data.get("confidence")
    if not isinstance(confidence, str):
        raise TaskAnalysisParseError("'confidence' must be a string")
    if confidence not in _VALID_CONFIDENCES:
        raise TaskAnalysisParseError(
            f"Invalid confidence {confidence!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_CONFIDENCES))}"
        )

    # --- output_actions -------------------------------------------------
    raw_actions = data.get("output_actions")
    if not isinstance(raw_actions, list) or len(raw_actions) == 0:
        raise TaskAnalysisParseError(
            "'output_actions' must be a non-empty list"
        )
    output_actions: tuple[OutputAction, ...] = tuple(
        _parse_output_action(item, idx)
        for idx, item in enumerate(raw_actions)
    )

    # --- workflow-type-specific required fields -------------------------
    extras = _TYPE_SPECIFIC_REQUIRED[workflow_type]
    for field_name in sorted(extras):
        if field_name not in data or data.get(field_name) is None:
            raise TaskAnalysisParseError(
                f"workflow_type {workflow_type!r} requires field "
                f"{field_name!r}"
            )
        value = data[field_name]
        if field_name == "children":
            if not isinstance(value, list) or len(value) == 0:
                raise TaskAnalysisParseError(
                    f"workflow_type {workflow_type!r} requires field "
                    f"'children' to be a non-empty list"
                )
        else:
            if not isinstance(value, str) or not value.strip():
                raise TaskAnalysisParseError(
                    f"workflow_type {workflow_type!r} requires field "
                    f"{field_name!r} to be a non-empty string"
                )

    # --- needs_info question handling ----------------------------------
    needs_info_question = data.get("needs_info_question")
    needs_info_questions: tuple[str, ...] = ()
    if confidence == "low":
        if (
            not needs_info_question
            or not isinstance(needs_info_question, str)
        ):
            raise TaskAnalysisParseError(
                "'needs_info_question' must be a non-empty string "
                "when confidence is 'low'"
            )
        needs_info_questions = (needs_info_question,)
    elif isinstance(needs_info_question, str) and needs_info_question.strip():
        needs_info_questions = (needs_info_question,)

    # --- optional fields ------------------------------------------------
    target_repo = data.get("target_repo")
    if target_repo is not None and not isinstance(target_repo, str):
        raise TaskAnalysisParseError(
            "'target_repo' must be a string or null"
        )

    target_branch = data.get("target_branch")
    if target_branch is not None and not isinstance(target_branch, str):
        raise TaskAnalysisParseError(
            "'target_branch' must be a string or null"
        )

    target_space = data.get("target_space")
    if target_space is not None and not isinstance(target_space, str):
        raise TaskAnalysisParseError(
            "'target_space' must be a string or null"
        )

    target_page_id = data.get("target_page_id")
    if target_page_id is not None and not isinstance(target_page_id, str):
        raise TaskAnalysisParseError(
            "'target_page_id' must be a string or null"
        )

    title = data.get("title", "") or ""
    if not isinstance(title, str):
        raise TaskAnalysisParseError("'title' must be a string")

    rationale = data.get("rationale", "") or ""
    if not isinstance(rationale, str):
        raise TaskAnalysisParseError("'rationale' must be a string")

    token_usage = data.get("token_usage", 0)
    if not isinstance(token_usage, int):
        raise TaskAnalysisParseError(
            "'token_usage' must be an integer"
        )

    return LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence=confidence,  # type: ignore[arg-type]
        target_repo=target_repo,
        target_branch=target_branch,
        target_space=target_space,
        target_page_id=target_page_id,
        title=title,
        rationale=rationale,
        output_actions=output_actions,
        needs_info_questions=needs_info_questions,
        token_usage=token_usage,
    )
