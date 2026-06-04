"""Description Parser activity — YAML front-matter override extraction.

Parses the ``---\\nai-bot:\\n...\\n---`` front-matter block at the top of a
Jira task description and exposes the structured override values consumed
by:class:`AutomationWorkflow`. When the user supplies a valid YAML block
the workflow takes the deterministic path and skips the LLM analysis
entirely.

The parser is **lenient** by design:

* Missing front-matter is not an error — the function returns ``None``
 so the caller can fall through to LLM analysis.
* Invalid field values do not abort parsing — they are recorded in:attr:`ParsedFrontMatter.parse_errors`, the offending field is set to
 ``None``, and the rest of the block is preserved. The caller can
 surface ``parse_errors`` as a warning Jira comment.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Final

# ``yaml.safe_load`` is the only YAML entry point used here — we never
# call ``yaml.load`` on user-supplied content (description bodies arrive
# straight from Jira webhooks). Imported lazily inside the parsing
# helper so this module can be imported even when PyYAML is not yet
# installed, e.g. during partial dev-env bootstrapping.
try:  # pragma: no cover — exercised at runtime only
    import yaml as _yaml  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]

__all__ = (
    "ParsedFrontMatter",
    "parse_description_frontmatter",
    "VALID_WORKFLOW_TYPES",
    "VALID_CLEANUP_POLICIES",
    "TIMEOUT_SECONDS_MIN",
    "TIMEOUT_SECONDS_MAX",
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation vocabularies
# ---------------------------------------------------------------------------

#: Closed set of workflow_type discriminators accepted by the platform.
#: The parser validates against this set so a typo in the YAML
#: (eg. ``code_change``
#: vs ``code_change_with_test``) is reported via ``parse_errors`` rather
#: than silently propagated to the workflow router.
VALID_WORKFLOW_TYPES: Final[frozenset[str]] = frozenset({
    "code_change_with_test",
    "code_change_commit_only",
    "pr_review",
    "remote_ssh_test_only",
    "script_execute",
    "confluence_doc_create",
    "confluence_doc_update",
    "research_publish_confluence",
    "research_summary_jira",
    "research_basic",
    "research_with_web",
    "multi_step",
    "noop_test",
})

#: Human-friendly aliases accepted from Streamlit / assistant-created
#: tasks. The workflow router still receives the canonical value.
WORKFLOW_TYPE_ALIASES: Final[dict[str, str]] = {
    "research": "research_basic",
    "research_web": "research_with_web",
    "doc_generation": "confluence_doc_create",
    "po_review_request": "pr_review",
    "test": "remote_ssh_test_only",
}

#: Closed set of cleanup policies accepted in the YAML block. The
#: ``cleanup`` key overrides ``docker_defaults.cleanup_policy`` from the
#: department config. Anything outside this set is
#: dropped to ``None`` and logged in ``parse_errors``.
VALID_CLEANUP_POLICIES: Final[frozenset[str]] = frozenset({
    "on_success",
    "always",
    "never",
})

#: Inclusive lower bound for ``timeout_seconds``.
TIMEOUT_SECONDS_MIN: Final[int] = 60

#: Inclusive upper bound for ``timeout_seconds``.
TIMEOUT_SECONDS_MAX: Final[int] = 7200

#: Closed set of output action ``type`` values accepted in the YAML
#: ``output`` list. Mirrors the canonical action types referenced in
#: ``platform/prompts/task_creation_assistant.md`` and consumed by the
#: ``execute_output_actions`` activity. Any other value is dropped from
#: the parsed list and surfaced via ``parse_errors``.
_VALID_OUTPUT_TYPES: Final[frozenset[str]] = frozenset({
    "jira_comment",
    "jira_attachment",
    "bitbucket_commit",
    "bitbucket_create_pr",
    "confluence_create_page",
    "confluence_update_page",
    "jira_transition",
})

_OUTPUT_TYPE_ALIASES: Final[dict[str, str]] = {
    "bitbucket_put_file": "bitbucket_commit",
    "bitbucket_pr": "bitbucket_create_pr",
    "confluence_page": "confluence_create_page",
}

_COMMAND_FIELDS: Final[tuple[str, ...]] = (
    "execution_command",
    "test_command",
    "command",
    "script",
    "commands",
)

COMMAND_LENGTH_MAX: Final[int] = 8000


# ---------------------------------------------------------------------------
# Front-matter delimiter regex
# ---------------------------------------------------------------------------

#: Front-matter must begin at the very start of the description (any
#: amount of leading whitespace / BOM is tolerated). The opening
#: ``---`` line is followed by the YAML body and a closing ``---`` line.
#: ``re.DOTALL`` lets ``.*?`` match across newlines; the lazy quantifier
#: stops at the **first** ``---`` line so a ``---`` inside the YAML body
#: (eg. inside a quoted string) doesn't confuse us — but in practice
#: ``---`` is always a YAML stream separator so this is the right
#: behaviour anyway.
_FRONT_MATTER_RE: Final[re.Pattern[str]] = re.compile(
    r"\A\s*-{3,}\s*\n(?P<body>.*?)\n-{3,}\s*(?:\n|$)",
    re.DOTALL,
)

# Jira's storage/wiki conversion can round-trip underscore-heavy YAML
# identifiers through the MCP issue view as Markdown emphasis markers
# (for example ``workflow_type`` -> ``workflow*type``). Only restore this
# for structured identifiers, never free-form user text.
_JIRA_IDENTIFIER_MARKUP_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<=[A-Za-z0-9])\*(?=[A-Za-z0-9])"
)
_JIRA_IDENTIFIER_VALUE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9*._/-]*$"
)
_IDENTIFIER_PARAM_KEYS: Final[frozenset[str]] = frozenset({
    "project_key",
    "repo_slug",
    "workspace",
    "space_key",
    "branch",
    "source_branch",
    "target_branch",
    "from_branch",
    "to_branch",
})


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedFrontMatter:
    """Structured override values extracted from a YAML front-matter block.

 All fields are ``Optional`` because the user is free to omit any of
 them; the workflow falls back to department defaults for missing
 values. ``parse_errors`` lists the keys that **were** present but
 failed validation — those keys appear here as ``None`` even if the
 raw YAML contained a value..
 """

    workflow_type: str | None = None
    repo: str | None = None
    branch: str | None = None
    needs_ssh: bool | None = None
    needs_docker: bool | None = None
    execution_command: str | None = None
    cleanup: str | None = None
    timeout_seconds: int | None = None
    web_search: bool | None = None
    output: list[dict[str, Any]] | None = None
    parse_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Field-level coercion helpers
#
# Each helper takes the raw value pulled from the parsed YAML mapping
# and returns ``(coerced, error)``. ``coerced`` is ``None`` when the
# value fails validation; ``error`` is the human-readable reason logged
# in ``parse_errors``. Helpers intentionally tolerate ``None`` (= field
# omitted) by returning ``(None, None)``.
# ---------------------------------------------------------------------------


def _coerce_str(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str | None, str | None]:
    """Return a non-empty string or report an error."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, (
            f"{field_name}: expected string, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped and not allow_empty:
        return None, f"{field_name}: empty string is not allowed"
    if field_name in {"repo", "branch"}:
        stripped = _restore_jira_identifier_value(stripped)
    return stripped, None


def _restore_jira_identifier_markup(value: str) -> str:
    """Restore Jira/MCP emphasis-marked identifier fragments."""
    return _JIRA_IDENTIFIER_MARKUP_RE.sub("_", value)


def _restore_jira_identifier_value(value: str) -> str:
    """Restore a scalar value only when it looks like an identifier."""
    if "*" not in value or not _JIRA_IDENTIFIER_VALUE_RE.fullmatch(value):
        return value
    return _restore_jira_identifier_markup(value)


def _normalise_mapping_keys(value: Any) -> Any:
    """Recursively normalise structured mapping keys after YAML parsing."""
    if isinstance(value, dict):
        return {
            _restore_jira_identifier_markup(key) if isinstance(key, str) else key:
            _normalise_mapping_keys(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise_mapping_keys(item) for item in value]
    return value


def _normalise_identifier_params(params: dict[str, Any]) -> dict[str, Any]:
    """Restore Jira-mangled identifier values in action params."""
    normalised: dict[str, Any] = {}
    for key, value in params.items():
        if key in _IDENTIFIER_PARAM_KEYS and isinstance(value, str):
            normalised[key] = _restore_jira_identifier_value(value)
        else:
            normalised[key] = value
    return normalised


def _coerce_bool(
    value: Any,
    field_name: str,
) -> tuple[bool | None, str | None]:
    """Return a strict bool or report an error.

 PyYAML ``safe_load`` already converts ``true``/``false``/``yes``/
 ``no``/``on``/``off`` to ``bool``, but we still reject other types
 explicitly so a YAML string like ``"true"`` (quoted) surfaces as a
 parse error rather than being silently accepted.
 """
    if value is None:
        return None, None
    if not isinstance(value, bool):
        return None, (
            f"{field_name}: expected boolean, got {type(value).__name__}"
        )
    return value, None


def _coerce_workflow_type(
    value: Any,
) -> tuple[str | None, str | None]:
    """Validate ``workflow_type`` against:data:`VALID_WORKFLOW_TYPES`."""
    coerced, err = _coerce_str(value, "workflow_type")
    if err is not None or coerced is None:
        return None, err
    coerced = _restore_jira_identifier_markup(coerced)
    coerced = WORKFLOW_TYPE_ALIASES.get(coerced, coerced)
    if coerced not in VALID_WORKFLOW_TYPES:
        return None, (
            f"workflow_type: {coerced!r} is not a valid workflow_type "
            f"(allowed: {sorted(VALID_WORKFLOW_TYPES)})"
        )
    return coerced, None


def _coerce_cleanup(value: Any) -> tuple[str | None, str | None]:
    """Validate ``cleanup`` against:data:`VALID_CLEANUP_POLICIES`."""
    coerced, err = _coerce_str(value, "cleanup")
    if err is not None or coerced is None:
        return None, err
    if coerced not in VALID_CLEANUP_POLICIES:
        return None, (
            f"cleanup: {coerced!r} is not valid "
            f"(allowed: {sorted(VALID_CLEANUP_POLICIES)})"
        )
    return coerced, None


def _coerce_timeout_seconds(value: Any) -> tuple[int | None, str | None]:
    """Validate ``timeout_seconds`` is an int in ``[60, 7200]``.

 Booleans are a subclass of ``int`` in Python; we reject them here
 explicitly so ``timeout_seconds: true`` doesn't sneak through as
 ``1``.
 """
    if value is None:
        return None, None
    if isinstance(value, bool):
        return None, (
            f"timeout_seconds: expected integer, got bool"
        )
    if not isinstance(value, int):
        return None, (
            f"timeout_seconds: expected integer, "
            f"got {type(value).__name__}"
        )
    if value < TIMEOUT_SECONDS_MIN or value > TIMEOUT_SECONDS_MAX:
        return None, (
            f"timeout_seconds: {value} out of range "
            f"[{TIMEOUT_SECONDS_MIN}, {TIMEOUT_SECONDS_MAX}]"
        )
    return value, None


def _coerce_command(value: Any, field_name: str) -> tuple[str | None, str | None]:
    """Return a shell command supplied as string or list of strings."""
    if value is None:
        return None, None

    if isinstance(value, str):
        command = value.strip()
        if not command:
            return None, f"{field_name}: empty string is not allowed"
    elif isinstance(value, list):
        parts: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str):
                return None, (
                    f"{field_name}[{index}]: expected string, "
                    f"got {type(item).__name__}"
                )
            part = item.strip()
            if not part:
                return None, f"{field_name}[{index}]: empty string is not allowed"
            parts.append(part)
        command = " && ".join(parts)
        if not command:
            return None, f"{field_name}: empty list is not allowed"
    else:
        return None, (
            f"{field_name}: expected string or list, got {type(value).__name__}"
        )

    if "\x00" in command or "\r" in command:
        return None, f"{field_name}: NUL/CR characters are not allowed"
    if len(command) > COMMAND_LENGTH_MAX:
        return None, (
            f"{field_name}: command length {len(command)} exceeds "
            f"{COMMAND_LENGTH_MAX}"
        )
    return command, None


def _coerce_output(
    value: Any,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Validate the ``output`` list of action dicts.

 Each entry must be a mapping with a ``type`` field that names one of
 the canonical action types. ``params`` is optional (defaults to an
 empty dict) and may be any mapping. Invalid entries are dropped from
 the returned list and reported as individual errors so a single bad
 action doesn't poison the rest of the override block.

 Returns:
 ``(coerced_list, errors)``. The list is ``None`` if the
 ``output`` key was omitted or the top-level value was not a
 list at all; in the latter case ``errors`` carries the reason.
 """
    if value is None:
        return None, []
    if not isinstance(value, list):
        return None, [
            f"output: expected list, got {type(value).__name__}",
        ]

    coerced: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            raw = {"type": raw.strip(), "params": {}}
        if not isinstance(raw, dict):
            errors.append(
                f"output[{index}]: expected mapping, "
                f"got {type(raw).__name__}"
            )
            continue
        action_type = raw.get("type")
        if not isinstance(action_type, str) or not action_type.strip():
            errors.append(
                f"output[{index}]: missing or invalid 'type' field"
            )
            continue
        action_type_clean = _restore_jira_identifier_markup(action_type.strip())
        canonical_type = _OUTPUT_TYPE_ALIASES.get(
            action_type_clean, action_type_clean
        )
        if canonical_type not in _VALID_OUTPUT_TYPES:
            errors.append(
                f"output[{index}]: type {action_type!r} is not valid "
                f"(allowed: {sorted(_VALID_OUTPUT_TYPES | frozenset(_OUTPUT_TYPE_ALIASES))})"
            )
            continue
        params = raw.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            errors.append(
                f"output[{index}]: 'params' must be a mapping, "
                f"got {type(params).__name__}"
            )
            continue
        coerced.append({
            "type": canonical_type,
            "params": _normalise_identifier_params(dict(params)),
        })

    return coerced, errors


# ---------------------------------------------------------------------------
# Front-matter extraction
# ---------------------------------------------------------------------------


def _extract_front_matter_body(description: str) -> str | None:
    """Return the YAML body between the ``---`` delimiters or ``None``.

 The match is anchored at the start of the description so only a
 *true* front-matter block is recognised — a stray ``---`` further
 down in the body (eg. a Markdown horizontal rule) will not trigger
 parsing.
 """
    if not description:
        return None
    match = _FRONT_MATTER_RE.match(description)
    if match is None:
        return None
    return match.group("body")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_description_frontmatter(
    description: str | None,
) -> ParsedFrontMatter | None:
    """Parse the ``ai-bot`` YAML front-matter block from a description.

 Behaviour matrix:

 * ``description`` is empty / ``None`` / contains no front-matter →
 return ``None``. The caller falls through to LLM analysis.
 * Front-matter delimiters are present but the YAML body is empty,
 malformed, or does not contain an ``ai-bot`` mapping → return
 ``None``. We treat a syntactically broken block the same as a
 missing block; the LLM path is the safety net.
 * Front-matter is well-formed → return a:class:`ParsedFrontMatter`
 whose fields carry the validated overrides; invalid field values
 are dropped to ``None`` and recorded in ``parse_errors``..
 """
    if description is None:
        return None

    body = _extract_front_matter_body(description)
    if body is None:
        return None

    if _yaml is None:
        # PyYAML missing — treat as a parse failure rather than crashing
        # the workflow. The caller still sees the front-matter block
        # textually but cannot extract structured values, so we return
        # ``None`` and let the LLM path take over.
        _logger.warning(
            "description_parser: PyYAML not installed; falling back to "
            "LLM analysis path"
        )
        return None

    try:
        parsed: Any = _yaml.safe_load(body)
    except _yaml.YAMLError as exc:
        _logger.info(
            "description_parser: malformed YAML front-matter (%s); "
            "falling back to LLM analysis path",
            exc,
        )
        return None

    if not isinstance(parsed, dict):
        return None
    parsed = _normalise_mapping_keys(parsed)

    ai_bot = parsed.get("ai-bot")
    if ai_bot is None:
        ai_bot = parsed.get("ai_bot")
    if not isinstance(ai_bot, dict):
        return None

    errors: list[str] = []

    workflow_type, err = _coerce_workflow_type(ai_bot.get("workflow_type"))
    if err is not None:
        errors.append(err)

    repo, err = _coerce_str(ai_bot.get("repo"), "repo")
    if err is not None:
        errors.append(err)

    branch, err = _coerce_str(ai_bot.get("branch"), "branch")
    if err is not None:
        errors.append(err)

    needs_ssh, err = _coerce_bool(ai_bot.get("needs_ssh"), "needs_ssh")
    if err is not None:
        errors.append(err)

    needs_docker, err = _coerce_bool(
        ai_bot.get("needs_docker"), "needs_docker"
    )
    if err is not None:
        errors.append(err)

    execution_command: str | None = None
    for field_name in _COMMAND_FIELDS:
        if field_name not in ai_bot:
            continue
        execution_command, err = _coerce_command(
            ai_bot.get(field_name), field_name
        )
        if err is not None:
            errors.append(err)
        break

    cleanup, err = _coerce_cleanup(ai_bot.get("cleanup"))
    if err is not None:
        errors.append(err)

    timeout_seconds, err = _coerce_timeout_seconds(
        ai_bot.get("timeout_seconds")
    )
    if err is not None:
        errors.append(err)

    web_search, err = _coerce_bool(ai_bot.get("web_search"), "web_search")
    if err is not None:
        errors.append(err)

    output, output_errors = _coerce_output(
        ai_bot.get("output", ai_bot.get("output_actions"))
    )
    errors.extend(output_errors)

    return ParsedFrontMatter(
        workflow_type=workflow_type,
        repo=repo,
        branch=branch,
        needs_ssh=needs_ssh,
        needs_docker=needs_docker,
        execution_command=execution_command,
        cleanup=cleanup,
        timeout_seconds=timeout_seconds,
        web_search=web_search,
        output=output,
        parse_errors=errors,
    )
