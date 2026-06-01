"""TaskAnalysis JSON output parser and Jinja2 prompt renderer.

This module provides:
- ``TaskAnalysisError``: raised when LLM output fails validation.
- ``OutputAction``: frozen dataclass representing a single output action.
- ``TaskAnalysis``: frozen dataclass representing the full LLM analysis result.
- ``parse_task_analysis``: validates and parses raw LLM JSON into TaskAnalysis.
- ``parse_context_block``: extracts and parses a ``<context>...</context>`` JSON
  block from an issue description (Feature 11). If valid, returns a TaskAnalysis
  directly without LLM NL parsing.
- ``format_task_analysis``: serialises TaskAnalysis back to a plain dict (lossless round-trip).
- ``render_prompt``: renders a Jinja2 markdown template with the given context.

Validation rules (from MIMARI §1 Kural 10 and design §3.4):
- ``workflow_type`` must be a key in ``WORKFLOW_TYPE_CAPABILITIES``.
- ``confidence`` must be one of ``{"high", "medium", "low"}``.
- When ``confidence == "low"``, ``needs_info_question`` must be non-empty.
- ``output_actions`` must be a non-empty list.
- Any ``bitbucket_pr`` action has its ``draft`` field coerced to ``True``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import BaseLoader, Environment, FileSystemLoader, TemplateNotFound

from temporal_shared.capabilities import WORKFLOW_TYPE_CAPABILITIES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_CONFIDENCES: frozenset[str] = frozenset({"high", "medium", "low"})

_VALID_ACTION_TYPES: frozenset[str] = frozenset({
    "jira_comment",
    "bitbucket_pr",
    "bitbucket_commit",
    "confluence_page",
})

# Per-workflow-type required-field map (Requirement 6.7).
#
# ``workflow_type``, ``confidence`` and ``output_actions`` are universal
# required fields; the entries below list the *additional* type-specific
# fields each workflow_type must carry on top of the universal set.
#
# Mirrors ``_TYPE_SPECIFIC_REQUIRED`` in
# ``platform/tests/property/test_task_analysis_parser.py``.
_TYPE_SPECIFIC_REQUIRED: dict[str, frozenset[str]] = {
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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskAnalysisError(ValueError):
    """Raised when LLM output fails structural or semantic validation."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputAction:
    """A single output action from the LLM task analysis.

    Attributes:
        type: Action type identifier (e.g. ``"jira_comment"``, ``"bitbucket_pr"``).
        payload: Arbitrary action-specific payload dictionary.
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskAnalysis:
    """Parsed and validated LLM task analysis result.

    Attributes:
        workflow_type: One of the keys in ``WORKFLOW_TYPE_CAPABILITIES``.
        target_repo: Target repository name, or ``None`` for non-code workflows.
        target_branch: Target branch name, or ``None`` for non-code workflows.
        output_actions: Non-empty tuple of output actions to perform.
        confidence: One of ``"high"``, ``"medium"``, ``"low"``.
        needs_info_question: Question for the reporter when confidence is low;
            ``None`` otherwise.
        target_lang: Target language for confluence workflows
            (``"tr"``/``"en"``); ``None`` for non-confluence workflows.
        children: Child sub-task descriptors for ``multi_step`` workflows;
            ``None`` for other workflow types.
    """

    workflow_type: str
    target_repo: str | None
    target_branch: str | None
    output_actions: tuple[OutputAction, ...]
    confidence: str
    needs_info_question: str | None = None
    target_lang: str | None = None
    children: tuple[dict[str, Any], ...] | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_draft_true(action_payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure ``draft`` is always ``True`` for bitbucket_pr actions.

    MIMARI §1 Kural 10: PR'lar her zaman draft olarak açılır.
    """
    coerced = dict(action_payload)
    coerced["draft"] = True
    return coerced


def _parse_output_action(raw: Any, index: int) -> OutputAction:
    """Parse a single output action entry from the raw list."""
    if not isinstance(raw, dict):
        raise TaskAnalysisError(
            f"output_actions[{index}]: expected dict, got {type(raw).__name__}"
        )

    action_type = raw.get("type")
    if not isinstance(action_type, str) or not action_type:
        raise TaskAnalysisError(
            f"output_actions[{index}]: 'type' must be a non-empty string"
        )

    payload = raw.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise TaskAnalysisError(
            f"output_actions[{index}]: 'payload' must be a dict, got {type(payload).__name__}"
        )

    # Coerce draft to True for bitbucket_pr actions (MIMARI §1 Kural 10)
    if action_type == "bitbucket_pr":
        payload = _coerce_draft_true(payload)

    return OutputAction(type=action_type, payload=payload)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_task_analysis(payload: dict[str, Any] | str) -> TaskAnalysis:
    """Parse and validate an LLM task-analysis JSON response.

    Args:
        payload: Either a parsed dict or a raw JSON string from the LLM.

    Returns:
        A validated ``TaskAnalysis`` instance.

    Raises:
        TaskAnalysisError: If the payload fails any validation rule.
    """
    # --- Deserialise if string ---
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise TaskAnalysisError(f"Invalid JSON: {exc}") from exc
    elif isinstance(payload, dict):
        data = payload
    else:
        raise TaskAnalysisError(
            f"payload must be dict or str, got {type(payload).__name__}"
        )

    # --- workflow_type ---
    workflow_type = data.get("workflow_type")
    if not isinstance(workflow_type, str):
        raise TaskAnalysisError("'workflow_type' must be a string")
    if workflow_type not in WORKFLOW_TYPE_CAPABILITIES:
        allowed = ", ".join(sorted(WORKFLOW_TYPE_CAPABILITIES.keys()))
        raise TaskAnalysisError(
            f"Invalid workflow_type '{workflow_type}'. "
            f"Must be one of: {allowed}"
        )

    # --- confidence ---
    confidence = data.get("confidence")
    if not isinstance(confidence, str):
        raise TaskAnalysisError("'confidence' must be a string")
    if confidence not in _VALID_CONFIDENCES:
        raise TaskAnalysisError(
            f"Invalid confidence '{confidence}'. "
            f"Must be one of: {', '.join(sorted(_VALID_CONFIDENCES))}"
        )

    # --- needs_info_question ---
    needs_info_question = data.get("needs_info_question")
    if confidence == "low":
        if not needs_info_question or not isinstance(needs_info_question, str):
            raise TaskAnalysisError(
                "'needs_info_question' must be a non-empty string "
                "when confidence is 'low'"
            )
    else:
        # Normalise to None for non-low confidence
        if needs_info_question is not None and not isinstance(needs_info_question, str):
            needs_info_question = None
        # Allow non-empty string even for high/medium (LLM may provide it)
        # but normalise empty string to None
        if isinstance(needs_info_question, str) and not needs_info_question.strip():
            needs_info_question = None

    # --- output_actions ---
    raw_actions = data.get("output_actions")
    if not isinstance(raw_actions, list) or len(raw_actions) == 0:
        raise TaskAnalysisError(
            "'output_actions' must be a non-empty list"
        )
    output_actions = tuple(
        _parse_output_action(item, idx) for idx, item in enumerate(raw_actions)
    )

    # --- target_repo / target_branch ---
    target_repo = data.get("target_repo")
    if target_repo is not None and not isinstance(target_repo, str):
        raise TaskAnalysisError("'target_repo' must be a string or null")

    target_branch = data.get("target_branch")
    if target_branch is not None and not isinstance(target_branch, str):
        raise TaskAnalysisError("'target_branch' must be a string or null")

    # --- workflow-type-specific required fields (Requirement 6.7) ---
    extras = _TYPE_SPECIFIC_REQUIRED.get(workflow_type, frozenset())
    for field_name in sorted(extras):
        if field_name not in data or data.get(field_name) is None:
            raise TaskAnalysisError(
                f"workflow_type {workflow_type!r} requires field {field_name!r}"
            )
        value = data[field_name]
        if field_name == "children":
            if not isinstance(value, list) or len(value) == 0:
                raise TaskAnalysisError(
                    f"workflow_type {workflow_type!r} requires field 'children' "
                    f"to be a non-empty list"
                )
        else:
            # target_repo / target_branch / target_lang must be non-empty strings.
            if not isinstance(value, str) or not value.strip():
                raise TaskAnalysisError(
                    f"workflow_type {workflow_type!r} requires field "
                    f"{field_name!r} to be a non-empty string"
                )

    # --- target_lang / children (carried through for round-trip) ---
    target_lang = data.get("target_lang")
    if target_lang is not None and not isinstance(target_lang, str):
        raise TaskAnalysisError("'target_lang' must be a string or null")

    raw_children = data.get("children")
    children: tuple[dict[str, Any], ...] | None
    if raw_children is None:
        children = None
    elif isinstance(raw_children, list):
        for idx, item in enumerate(raw_children):
            if not isinstance(item, dict):
                raise TaskAnalysisError(
                    f"children[{idx}]: expected dict, got {type(item).__name__}"
                )
        children = tuple(dict(item) for item in raw_children)
    else:
        raise TaskAnalysisError("'children' must be a list or null")

    return TaskAnalysis(
        workflow_type=workflow_type,
        target_repo=target_repo,
        target_branch=target_branch,
        output_actions=output_actions,
        confidence=confidence,
        needs_info_question=needs_info_question,
        target_lang=target_lang,
        children=children,
    )


def format_task_analysis(t: TaskAnalysis) -> dict[str, Any]:
    """Serialise a ``TaskAnalysis`` to a plain dict suitable for JSON encoding.

    The output is lossless: ``parse_task_analysis(format_task_analysis(t))``
    returns an equivalent ``TaskAnalysis``.
    """
    result: dict[str, Any] = {
        "workflow_type": t.workflow_type,
        "target_repo": t.target_repo,
        "target_branch": t.target_branch,
        "output_actions": [
            {"type": action.type, "payload": action.payload}
            for action in t.output_actions
        ],
        "confidence": t.confidence,
        "needs_info_question": t.needs_info_question,
    }
    if t.target_lang is not None:
        result["target_lang"] = t.target_lang
    if t.children is not None:
        result["children"] = [dict(child) for child in t.children]
    return result


def render_prompt(template_path: str | Path, context: dict[str, Any]) -> str:
    """Render a Jinja2 markdown template with the given context variables.

    Args:
        template_path: Absolute or relative path to the ``.md`` template file.
        context: Dictionary of template variables to inject.

    Returns:
        The rendered prompt string.

    Raises:
        FileNotFoundError: If the template file does not exist.
        jinja2.TemplateError: If the template contains syntax errors.
    """
    path = Path(template_path)
    if not path.is_file():
        raise FileNotFoundError(f"Template not found: {path}")

    env = Environment(
        loader=FileSystemLoader(str(path.parent)),
        keep_trailing_newline=True,
        autoescape=False,
    )
    template = env.get_template(path.name)
    return template.render(**context)


# ---------------------------------------------------------------------------
# Feature 11: Structured JSON-block in task description parsing
# ---------------------------------------------------------------------------

#: Regex to extract a <context>...</context> JSON block from issue description.
_CONTEXT_BLOCK_RE = re.compile(
    r"<context>\s*(.*?)\s*</context>",
    re.DOTALL,
)

#: Expected keys in the context block JSON schema.
_CONTEXT_BLOCK_KEYS = frozenset({
    "task_type",
    "repo",
    "branch",
    "cleanup",
    "output_actions",
    "language",
})

#: Mapping from context block ``task_type`` to workflow_type.
#: If the task_type is already a valid workflow_type, it passes through.
_TASK_TYPE_TO_WORKFLOW: dict[str, str] = {
    "code_change": "code_change_with_test",
    "code_change_with_test": "code_change_with_test",
    "code_change_commit_only": "code_change_commit_only",
    "pr_review": "pr_review",
    "remote_ssh_test_only": "remote_ssh_test_only",
    "confluence_doc_create": "confluence_doc_create",
    "confluence_doc_update": "confluence_doc_update",
    "research_basic": "research_basic",
    "research_with_web": "research_with_web",
    "multi_step": "multi_step",
    "noop_test": "noop_test",
}


def parse_context_block(description: str) -> TaskAnalysis | None:
    """Attempt to extract and parse a ``<context>...</context>`` JSON block.

    If the description contains a valid ``<context>`` block with the
    expected schema, constructs a ``TaskAnalysis`` directly without
    requiring LLM NL parsing.

    Args:
        description: The full issue description text.

    Returns:
        A ``TaskAnalysis`` if a valid context block was found and parsed,
        or ``None`` if no block was found or the block was invalid.

    The JSON block schema::

        {
            "task_type": str,       # maps to workflow_type
            "repo": str,            # target repository
            "branch": str,          # target branch
            "cleanup": str,         # cleanup strategy (ignored for now)
            "output_actions": [str],# list of action type strings
            "language": str         # target language (tr/en)
        }
    """
    match = _CONTEXT_BLOCK_RE.search(description)
    if match is None:
        return None

    raw_json = match.group(1)
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    # Validate required keys are present
    task_type = data.get("task_type")
    repo = data.get("repo")
    branch = data.get("branch")
    output_actions_raw = data.get("output_actions")
    language = data.get("language")

    if not isinstance(task_type, str) or not task_type:
        return None
    if not isinstance(repo, str) or not repo:
        return None
    if not isinstance(branch, str) or not branch:
        return None
    if not isinstance(output_actions_raw, list) or len(output_actions_raw) == 0:
        return None

    # Map task_type to workflow_type
    workflow_type = _TASK_TYPE_TO_WORKFLOW.get(task_type, task_type)
    if workflow_type not in WORKFLOW_TYPE_CAPABILITIES:
        return None

    # Build output actions
    output_actions: list[OutputAction] = []
    for action_str in output_actions_raw:
        if isinstance(action_str, str) and action_str:
            # If it's a bitbucket_pr, coerce draft to True
            payload: dict[str, Any] = {}
            if action_str == "bitbucket_pr":
                payload["draft"] = True
            output_actions.append(OutputAction(type=action_str, payload=payload))
        elif isinstance(action_str, dict):
            parsed = _parse_output_action(action_str, len(output_actions))
            output_actions.append(parsed)

    if not output_actions:
        return None

    # Determine target_lang
    target_lang: str | None = None
    if isinstance(language, str) and language.strip():
        target_lang = language.strip()

    return TaskAnalysis(
        workflow_type=workflow_type,
        target_repo=repo,
        target_branch=branch,
        output_actions=tuple(output_actions),
        confidence="high",
        needs_info_question=None,
        target_lang=target_lang,
        children=None,
    )
