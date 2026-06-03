"""Task Analyzer activity for the automation-worker.

Implements the ``analyze_task`` Temporal activity that decides which
workflow_type a Jira issue should run as. The activity follows a
two-stage strategy:

1. **YAML front-matter (deterministic)** — if the issue description
 starts with a ``---\nai-bot:\n...\n---`` block, the parsed values
 are accepted directly and the LLM call is skipped entirely. This
 is the fast/cheap path used by power users and the
 ``task_creation_assistant`` chat flow.

2. **LLM analysis (fallback)** — when no YAML block is present (or it
 is missing required fields), the activity renders the
 ``task_analysis.md`` prompt with the issue + department context and
 calls the configured LLM provider. The response is parsed as JSON
 and validated.

After either branch produces a candidate result the activity applies
post-processing:

* ``workflow_type`` must be in:data:`VALID_WORKFLOW_TYPES`; otherwise
 the task is rejected and a Jira comment is posted.
* ``confidence < 0.7`` triggers the *needs_info* flow — the activity
 posts a comment listing ``missing_fields`` and signals the workflow
 to wait for user input.
* If ``workflow_type`` requires web search but the department's
 ``web_search_enabled`` flag is ``false``, the type is downgraded
 from ``research_with_web``/``research_publish_confluence`` to
 ``research_basic``.
* When the YAML front-matter contains invalid field values
 (``parse_errors`` non-empty), each offending field is silently
 dropped to ``None`` by the parser and a single advisory Jira
 comment is posted listing every parse error so the reporter can
 fix the override block (/).
 Department defaults take over for the dropped fields — see:func:`_result_from_frontmatter` for the override-on-top-of-dept
 logic for ``cleanup``, ``timeout_seconds``, and ``web_search``
 (—).

The prompt file is loaded with a **hot-reload** mechanism: every call
checks the file's ``mtime`` and re-reads it when changed. This allows admins to edit the prompt at runtime without
restarting the worker.

Design reference: ``design.md`` §2 (Task Analyzer)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from temporalio import activity

# Description parser is implemented in (parallel). We import
# lazily inside ``analyze_task`` so this module can still be loaded
# (and unit-tested for its non-YAML branches) before
# ``description_parser.py`` lands on disk.
__all__ = (
    "analyze_task",
    "TaskAnalysisInput",
    "TaskAnalysisResult",
    "TaskAnalysisError",
    "VALID_WORKFLOW_TYPES",
    "WEB_SEARCH_WORKFLOW_TYPES",
    "CONFIDENCE_THRESHOLD",
    "DEFAULT_PROMPT_PATH",
    "set_llm_caller",
    "get_llm_caller",
    "set_jira_commenter",
    "get_jira_commenter",
    "set_prompt_path",
    "get_prompt_path",
    "PromptCache",
    "_is_epic_issue",
    "_get_epic_subtasks",
    "_build_epic_needs_subtasks_comment",
)

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Closed set of supported workflow_type values. Matches 
#: and the ``WORKFLOW_TYPE_CAPABILITIES`` source of truth in
#: ``temporal_shared.capabilities`` (with the ``research_summary_jira``
#: alias used by the chat assistant).
VALID_WORKFLOW_TYPES: frozenset[str] = frozenset({
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

WORKFLOW_TYPE_ALIASES: dict[str, str] = {
    "research": "research_basic",
    "research_web": "research_with_web",
    "doc_generation": "confluence_doc_create",
    "po_review_request": "pr_review",
    "test": "remote_ssh_test_only",
}

#: Workflow types that require the ``web_search`` capability. When the
#: department disables web search these are downgraded to
#: ``research_basic``.
WEB_SEARCH_WORKFLOW_TYPES: frozenset[str] = frozenset({
    "research_with_web",
    "research_publish_confluence",
})

EXECUTION_COMMAND_WORKFLOW_TYPES: frozenset[str] = frozenset(
    {
        "code_change_with_test",
        "remote_ssh_test_only",
        "script_execute",
    }
)

_OUTPUT_ACTION_TYPE_ALIASES: dict[str, str] = {
    "bitbucket_put_file": "bitbucket_commit",
    "bitbucket_pr": "bitbucket_create_pr",
    "confluence_page": "confluence_create_page",
}

_VALID_OUTPUT_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "jira_comment",
        "jira_attachment",
        "bitbucket_commit",
        "bitbucket_create_pr",
        "confluence_create_page",
        "confluence_update_page",
        "jira_transition",
        "slack_notify",
        "email_notify",
    }
)


def _canonical_workflow_type(value: str | None) -> str | None:
    """Map accepted UI / LLM aliases to canonical workflow names."""
    if value is None:
        return None
    return WORKFLOW_TYPE_ALIASES.get(value, value)

#: Confidence threshold below which the task transitions to
#: ``needs_info``.
CONFIDENCE_THRESHOLD: float = 0.7

#: Default location of the task analysis prompt file (,
#: 20.1). Resolved relative to the platform repo root so the worker
#: can load it without copying the file into its image.
#:
#: ``__file__`` lives at
#: ``platform/workers/automation-worker/src/automation_worker/activities/task_analyzer.py``
#: so ``parents[5]`` points at the ``platform/`` directory. The
#: canonical prompt lives in the ``agent-runner-worker`` (not at the
#: platform root) — both workers share the same task-analysis prompt
#: and the agent-runner-worker is its owner. This default may be
#: overridden via:func:`set_prompt_path` (used in tests and to point
#: at a Vault-mounted volume in production).
def _resolve_default_prompt_path() -> Path:
    """Resolve the default task-analysis prompt path safely.

 Dev tree layout puts the file under
 ``platform/workers/automation-worker/src/automation_worker/activities/``
 (5 parents to ``platform/``), but the production container ships
 the source under ``/app/src/...`` (only 4 parents available).
 Trying ``parents[5]`` in that layout raises ``IndexError`` at
 import time, taking the whole worker down.

 We probe a small set of plausible roots and pick the first that
 actually contains the prompt file. Falls back to the dev-tree
 expectation if nothing matches; callers can still override via:func:`set_prompt_path`.
 """

    here = Path(__file__).resolve()
    candidates = []
    # Walk the parents until we exhaust them — at least one of these
    # will be a sensible "repo root" candidate.
    for parent in here.parents:
        candidates.append(
            parent / "workers" / "agent-runner-worker" / "prompts" / "task_analysis.md"
        )
        # Also try the sibling agent-runner-worker (production layout
        # where workers live as siblings under /app or a shared root).
        candidates.append(
            parent / "agent-runner-worker" / "prompts" / "task_analysis.md"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    # Fallback — return the *expected* dev path even if it doesn't
    # exist so a missing-prompt error surfaces at first use rather
    # than at import time.
    try:
        repo_root = here.parents[5]
    except IndexError:
        repo_root = here.parents[-1]
    return (
        repo_root
        / "workers"
        / "agent-runner-worker"
        / "prompts"
        / "task_analysis.md"
    )


DEFAULT_PROMPT_PATH: Path = _resolve_default_prompt_path()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TaskAnalysisError(ValueError):
    """Raised when LLM output fails structural or semantic validation."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskAnalysisInput:
    """Input for the ``analyze_task`` activity.

 Attributes:
 issue_key: The Jira issue key (e.g. ``"PAY-4211"``).
 title: Issue summary / title.
 description: Full description text (may contain a YAML
 front-matter block at the top).
 labels: Jira labels attached to the issue.
 custom_fields: Mapping of custom-field names to raw values
 (e.g. ``{"AI Bot Repo": "org/foo", "AI Bot Branch": "develop"}``).
 dept_id: Department identifier — used for credential resolution
 when posting comments.
 dept_config: Department configuration block. Only fields the
 analyzer cares about are read: ``web_search_enabled``,
 ``available_repos``, ``available_spaces``,
 ``available_capabilities``, ``default_language``,
 ``docker_defaults``. A plain dict is used so the input
 stays serialisable through the Temporal payload converter.
 trace_id: Trace identifier propagated for log correlation.
 issue_meta: Optional Jira issue metadata dict carrying fields
 like ``issuetype`` (``{"name": "Epic"}``) and ``subtasks``
 (list of subtask dicts). Used by the Epic auto-detect
 branch to bypass the LLM when
 the issue is an Epic.
 """

    issue_key: str
    title: str
    description: str
    labels: list[str]
    custom_fields: dict[str, str | None]
    dept_id: str
    dept_config: dict[str, Any]
    trace_id: str = ""
    issue_meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskAnalysisResult:
    """Result of the ``analyze_task`` activity.

 Attributes:
 accepted: ``True`` if the task can proceed to execution; ``False``
 when the analyzer rejected the task (invalid workflow_type)
 or downgraded to ``needs_info``.
 workflow_type: The resolved workflow_type, or ``None`` when the
 task was rejected outright.
 needs_ssh: Whether the workflow requires SSH access.
 needs_docker: Whether the workflow requires Docker access.
 repo: Target repository (Bitbucket slug or URL), or ``None``.
 branch: Target branch, or ``None``.
 cleanup_policy: One of ``"on_success"``, ``"always"``, ``"never"``.
 timeout_seconds: Per-task timeout override, or ``None`` to use
 the department default.
 web_search: Whether web search is enabled for this task.
 output_actions: List of output action dicts (passed through to
 the output_actions activity).
 confidence: Confidence score in the analysis result (0.0 to 1.0).
 missing_fields: Fields the analyzer could not determine — only
 populated when ``status == "needs_info"``.
 reasoning: Free-text explanation of the decision (LLM only).
 source: Where the result came from — ``"yaml_frontmatter"``
 when the YAML block was used, ``"llm_analysis"`` when the
 LLM produced the answer.
 status: ``"ready"`` (proceed), ``"needs_info"`` (wait for user),
 or ``"rejected"`` (terminal failure — invalid workflow_type).
 error: Error message when ``status == "rejected"``.
 downgraded: ``True`` when ``workflow_type`` was downgraded to
 ``research_basic`` because the department disabled web
 search.
 """

    accepted: bool
    workflow_type: str | None
    needs_ssh: bool
    needs_docker: bool
    execution_command: str | None
    repo: str | None
    branch: str | None
    cleanup_policy: str
    timeout_seconds: int | None
    web_search: bool
    output_actions: list[dict[str, Any]]
    confidence: float
    missing_fields: list[str]
    reasoning: str
    source: Literal["yaml_frontmatter", "llm_analysis", "epic_auto_detect"]
    status: Literal["ready", "needs_info", "rejected"]
    error: str | None = None
    downgraded: bool = False


# ---------------------------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMCallerProtocol(Protocol):
    """Protocol for the LLM provider used by the task analyzer.

 Production wires this to an ``LLMProvider`` from
 ``llm_orchestrator``. Tests inject a fake that returns a scripted
 JSON string so ``analyze_task`` can be exercised without a live
 LLM.
 """

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        """Call the LLM with the rendered prompt and return raw text.

 Args:
 prompt: The fully rendered system+user prompt.
 dept_id: Department identifier used to resolve the
 department-specific provider override (``primary``,
 ``fallback``).

 Returns:
 Raw LLM response text — the analyzer is responsible for
 JSON extraction.
 """
        ...


@runtime_checkable
class JiraCommenterProtocol(Protocol):
    """Protocol for posting comments to Jira.

 Reused from ``repo_resolver`` — same shape, different consumer.
 """

    async def add_comment(
        self,
        issue_key: str,
        body: str,
        *,
        dept_id: str,
    ) -> None:
        ...


_llm_caller: LLMCallerProtocol | None = None
_jira_commenter: JiraCommenterProtocol | None = None
_prompt_path: Path = DEFAULT_PROMPT_PATH


def set_llm_caller(caller: LLMCallerProtocol) -> None:
    """Register the LLM caller used by the activity.

 Called once at worker boot. Tests call this with an in-memory
 fake before invoking the activity directly.
 """
    global _llm_caller  # noqa: PLW0603
    _llm_caller = caller


def get_llm_caller() -> LLMCallerProtocol:
    """Resolve the registered LLM caller or fail loudly."""
    if _llm_caller is None:
        raise RuntimeError(
            "task_analyzer activity: LLM caller not initialised; "
            "call set_llm_caller during worker startup."
        )
    return _llm_caller


def set_jira_commenter(commenter: JiraCommenterProtocol) -> None:
    """Register the Jira commenter used by the activity."""
    global _jira_commenter  # noqa: PLW0603
    _jira_commenter = commenter


def get_jira_commenter() -> JiraCommenterProtocol:
    """Resolve the registered Jira commenter or fail loudly."""
    if _jira_commenter is None:
        raise RuntimeError(
            "task_analyzer activity: Jira commenter not initialised; "
            "call set_jira_commenter during worker startup."
        )
    return _jira_commenter


def set_prompt_path(path: Path | str) -> None:
    """Override the prompt file location.

 Useful in tests — production wires:data:`DEFAULT_PROMPT_PATH` automatically. Resetting the path
 invalidates the in-memory prompt cache.
 """
    global _prompt_path  # noqa: PLW0603
    _prompt_path = Path(path)
    _prompt_cache.invalidate()


def get_prompt_path() -> Path:
    """Return the currently configured prompt path."""
    return _prompt_path


# ---------------------------------------------------------------------------
# Hot-reload prompt cache 
# ---------------------------------------------------------------------------


class PromptCache:
    """Thread-safe cache that reloads the prompt file when ``mtime`` changes.

 The cache is intentionally minimal — Temporal activities run on a
 pool of asyncio workers which can race, so we guard the cache
 fields with a lock. The reload check is a single ``stat`` call
 per ``analyze_task`` invocation which is cheap (~microseconds)
 and avoids stale prompt content after ``echo > task_analysis.md``
 on the host.
 """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._content: str | None = None
        self._mtime: float | None = None
        self._path: Path | None = None

    def load(self, path: Path) -> str:
        """Return the prompt body, reloading if the file changed.

 Raises ``FileNotFoundError`` if the prompt is missing. Any
 ``OSError`` is propagated so the caller (``analyze_task``)
 can fall back / surface a meaningful error.
 """
        with self._lock:
            try:
                stat = path.stat()
            except FileNotFoundError:
                # Surface a clear error — prompt missing means the
                # whole analyzer is non-functional.
                self._content = None
                self._mtime = None
                self._path = None
                raise

            current_mtime = stat.st_mtime

            cache_valid = (
                self._content is not None
                and self._mtime == current_mtime
                and self._path == path
            )

            if not cache_valid:
                _logger.info(
                    "task_analyzer: (re)loading prompt from %s (mtime=%s)",
                    path,
                    current_mtime,
                )
                self._content = path.read_text(encoding="utf-8")
                self._mtime = current_mtime
                self._path = path

            assert self._content is not None  # for type checkers
            return self._content

    def invalidate(self) -> None:
        """Drop the cached content (used by ``set_prompt_path``)."""
        with self._lock:
            self._content = None
            self._mtime = None
            self._path = None

    @property
    def cached_mtime(self) -> float | None:
        """Currently cached ``mtime`` value — useful in tests."""
        return self._mtime


_prompt_cache = PromptCache()


# ---------------------------------------------------------------------------
# Helpers — JSON extraction and prompt rendering
# ---------------------------------------------------------------------------


def _strip_markdown_fence(text: str) -> str:
    """Remove ``` or ```json fences that LLMs sometimes wrap JSON with."""
    body = text.strip()
    if body.startswith("```"):
        lines = body.split("\n")
        # drop opening fence
        lines = lines[1:]
        # drop closing fence if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    return body


def _parse_json_response(raw: str) -> dict[str, Any]:
    """Parse the LLM JSON response, handling code fences."""
    body = _strip_markdown_fence(raw)
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TaskAnalysisError(
            f"LLM returned invalid JSON: {exc}. "
            f"Raw response (first 200 chars): {body[:200]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise TaskAnalysisError(
            f"LLM JSON must be an object, got {type(parsed).__name__}"
        )
    return parsed


def _render_prompt(template: str, input: TaskAnalysisInput) -> str:
    """Render the task_analysis.md prompt with the input context.

 The prompt template is plain markdown — we deliberately use simple
 string substitution rather than Jinja2 to keep the activity
 sandbox-clean (Temporal forbids many third-party imports inside
 activities running on the workflow side, and even on the activity
 side the smallest possible dependency surface is preferred).

 The prompt is not Jinja-rendered: the LLM is given the *raw*
 template followed by a JSON-encoded ``CONTEXT`` block so we never
 accidentally execute template directives that the prompt author
 intended to be example code. This matches the "tool calling +
 structured output" pattern used by Anthropic / OpenAI guidelines.
 """
    context = {
        "issue_key": input.issue_key,
        "title": input.title,
        "description": input.description,
        "labels": list(input.labels),
        "custom_fields": dict(input.custom_fields),
        "dept_id": input.dept_id,
        "dept_config": _safe_dept_config_for_prompt(input.dept_config),
        "trace_id": input.trace_id,
    }
    context_json = json.dumps(context, ensure_ascii=False, indent=2, default=str)
    return f"{template}\n\n## CONTEXT\n\n```json\n{context_json}\n```\n"


def _safe_dept_config_for_prompt(dept_config: dict[str, Any]) -> dict[str, Any]:
    """Strip secret-shaped fields before sending the dept config to the LLM.

 The activity is invoked with the full department config which may
 contain credential references (e.g. ``bot.jira.api_token``). We
 only pass the fields the LLM actually needs to make a decision.
 """
    allowed_keys = (
        "available_repos",
        "available_spaces",
        "available_capabilities",
        "default_language",
        "web_search_enabled",
        "docker_defaults",
        "repo_mappings",
        "approvers",
        "max_concurrent_workflows",
    )
    return {k: dept_config.get(k) for k in allowed_keys if k in dept_config}


# ---------------------------------------------------------------------------
# YAML front-matter handling (delegates to description_parser)
# ---------------------------------------------------------------------------


def _try_parse_frontmatter(description: str) -> Any | None:
    """Attempt to parse the YAML front-matter block.

 Returns the ``ParsedFrontMatter`` dataclass (from) or
 ``None`` when:
 * the description has no YAML block, or
 * the parser is not yet importable (module developed in parallel).

 The activity must remain functional even before ``description_parser``
 lands so we degrade gracefully — when the import fails the LLM
 branch handles every task.
 """
    try:
        from automation_worker.activities.description_parser import (  # type: ignore[import-not-found]
            parse_description_frontmatter,
        )
    except ImportError:
        _logger.debug(
            "task_analyzer: description_parser not available, "
            "skipping YAML front-matter check"
        )
        return None

    try:
        return parse_description_frontmatter(description)
    except Exception as exc:  # noqa: BLE001
        # The parser is supposed to never raise (invalid fields go in
        # ``parse_errors``) but if it does we don't want to abort the
        # whole task — fall through to the LLM.
        _logger.warning(
            "task_analyzer: description_parser raised %r — "
            "falling back to LLM analysis",
            exc,
        )
        return None


def _frontmatter_has_workflow_type(parsed: Any) -> bool:
    """Whether the parsed front-matter carries enough info to skip the LLM.

 The minimum required field is ``workflow_type``. Other fields can
 be ``None`` because they will be filled in by department defaults
 later in the workflow.
 """
    return parsed is not None and getattr(parsed, "workflow_type", None) is not None


def _description_without_frontmatter(description: str) -> str:
    """Return the user task body after an optional YAML front-matter block."""

    lines = description.splitlines()
    if lines and lines[0].strip() == "---":
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return "\n".join(lines[idx + 1 :]).strip()
    return description.strip()


def _result_from_frontmatter(
    parsed: Any,
    *,
    input: TaskAnalysisInput,
    dept_defaults: _DeptDefaults,
) -> TaskAnalysisResult:
    """Build a ``TaskAnalysisResult`` from a parsed YAML front-matter.

 YAML provides a *deterministic* path with implicit ``confidence==1.0``
 (+). Missing optional fields are
 filled in from ``dept_defaults``.
 """
    wf_type = getattr(parsed, "workflow_type", None) or ""

    # Normalise needs_ssh / needs_docker — derive from workflow_type
    # when the front-matter doesn't specify them explicitly.
    needs_ssh = getattr(parsed, "needs_ssh", None)
    if needs_ssh is None:
        needs_ssh = wf_type in {
            "code_change_with_test",
            "remote_ssh_test_only",
            "script_execute",
        }

    needs_docker = getattr(parsed, "needs_docker", None)
    if needs_docker is None:
        needs_docker = False

    # — invalid / missing ``cleanup`` falls back to the dept
    # default (the parser already dropped invalid values to ``None``;
    # any remaining ``None`` here means "user did not specify").
    cleanup = getattr(parsed, "cleanup", None) or dept_defaults.cleanup_policy

    # — same contract for ``timeout_seconds``: out-of-range or
    # otherwise-invalid values arrive as ``None`` and we backfill from
    # the dept docker_defaults so the workflow always has a finite
    # budget. This mirrors the LLM branch's behaviour in
    # ``_result_from_llm`` (— Per-task description override).
    timeout = getattr(parsed, "timeout_seconds", None)
    if timeout is None:
        timeout = dept_defaults.default_timeout_seconds

    web_search = getattr(parsed, "web_search", None)
    if web_search is None:
        web_search = False

    output = getattr(parsed, "output", None)
    output_actions = list(output) if output else []
    task_details = _description_without_frontmatter(input.description)
    reasoning = "Derived from YAML front-matter (deterministic path)."
    if task_details:
        reasoning = f"{reasoning}\n\nTask details:\n{task_details}"

    return TaskAnalysisResult(
        accepted=True,
        workflow_type=wf_type,
        needs_ssh=bool(needs_ssh),
        needs_docker=bool(needs_docker),
        execution_command=getattr(parsed, "execution_command", None),
        repo=getattr(parsed, "repo", None),
        branch=getattr(parsed, "branch", None),
        cleanup_policy=cleanup,
        timeout_seconds=timeout,
        web_search=bool(web_search),
        output_actions=output_actions,
        confidence=1.0,
        missing_fields=[],
        reasoning=reasoning,
        source="yaml_frontmatter",
        status="ready",
        error=None,
        downgraded=False,
    )


# ---------------------------------------------------------------------------
# Department defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DeptDefaults:
    """Distilled department defaults used by post-processing."""

    web_search_enabled: bool
    cleanup_policy: str
    default_timeout_seconds: int | None


def _extract_dept_defaults(dept_config: dict[str, Any]) -> _DeptDefaults:
    """Read the fields the analyzer cares about from the dept config."""
    docker_defaults = dept_config.get("docker_defaults") or {}
    return _DeptDefaults(
        web_search_enabled=bool(dept_config.get("web_search_enabled", False)),
        cleanup_policy=str(docker_defaults.get("cleanup_policy", "on_success")),
        default_timeout_seconds=docker_defaults.get("default_timeout_seconds"),
    )


# ---------------------------------------------------------------------------
# LLM result → TaskAnalysisResult
# ---------------------------------------------------------------------------


def _result_from_llm(
    raw_data: dict[str, Any],
    *,
    dept_defaults: _DeptDefaults,
) -> TaskAnalysisResult:
    """Convert the LLM's JSON dict into a ``TaskAnalysisResult``.

 All fields are accessed defensively because the LLM is allowed to
 omit values — defaults are filled in from the department config.
 Validation (workflow_type set membership, confidence threshold,
 web_search downgrade) happens in the caller.
 """

    def _bool(v: Any, default: bool) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"true", "yes", "1"}
        return default

    def _str_or_none(v: Any) -> str | None:
        if isinstance(v, str) and v.strip():
            return v.strip()
        return None

    def _command_or_none(*keys: str) -> str | None:
        for key in keys:
            value = raw_data.get(key)
            if isinstance(value, str) and value.strip():
                command = value.strip()
            elif isinstance(value, list):
                parts = [part.strip() for part in value if isinstance(part, str)]
                if not parts or len(parts) != len(value):
                    continue
                command = " && ".join(parts)
            else:
                continue
            if command and "\x00" not in command and "\r" not in command:
                return command
        return None

    def _list_of_output_actions(v: Any) -> list[dict[str, Any]]:
        if not isinstance(v, list):
            return []
        out: list[dict[str, Any]] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                kind = item.strip()
                params: dict[str, Any] = {}
            elif isinstance(item, dict):
                raw_kind = item.get("type", item.get("kind"))
                if not isinstance(raw_kind, str) or not raw_kind.strip():
                    continue
                kind = raw_kind.strip()
                raw_params = item.get("params")
                if raw_params is None:
                    raw_params = item.get("payload", {})
                params = dict(raw_params) if isinstance(raw_params, dict) else {}
            else:
                continue
            kind = _OUTPUT_ACTION_TYPE_ALIASES.get(kind, kind)
            if kind in _VALID_OUTPUT_ACTION_TYPES:
                out.append({"type": kind, "params": params})
        return out

    workflow_type = _canonical_workflow_type(
        _str_or_none(raw_data.get("workflow_type"))
    )
    confidence_raw = raw_data.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    # Clamp to [0, 1] — LLMs occasionally emit 1.5 / -0.2 etc.
    confidence = max(0.0, min(1.0, confidence))

    missing_raw = raw_data.get("missing_fields") or []
    if isinstance(missing_raw, list):
        missing = [str(m) for m in missing_raw if isinstance(m, str) and m]
    else:
        missing = []

    cleanup_policy = _str_or_none(raw_data.get("cleanup_policy")) or dept_defaults.cleanup_policy
    timeout_raw = raw_data.get("timeout_seconds")
    timeout: int | None = None
    if isinstance(timeout_raw, int) and not isinstance(timeout_raw, bool):
        timeout = timeout_raw
    elif isinstance(timeout_raw, str) and timeout_raw.isdigit():
        timeout = int(timeout_raw)
    if timeout is None:
        timeout = dept_defaults.default_timeout_seconds

    return TaskAnalysisResult(
        accepted=True,
        workflow_type=workflow_type,
        needs_ssh=_bool(raw_data.get("needs_ssh"), False),
        needs_docker=_bool(raw_data.get("needs_docker"), False),
        execution_command=_command_or_none(
            "execution_command",
            "test_command",
            "command",
            "script",
            "commands",
        ),
        repo=_str_or_none(raw_data.get("repo")) or _str_or_none(raw_data.get("target_repo")),
        branch=_str_or_none(raw_data.get("branch")) or _str_or_none(raw_data.get("target_branch")),
        cleanup_policy=cleanup_policy,
        timeout_seconds=timeout,
        web_search=_bool(raw_data.get("web_search"), False),
        output_actions=_list_of_output_actions(
            raw_data.get("output_actions", raw_data.get("output"))
        ),
        confidence=confidence,
        missing_fields=missing,
        reasoning=_str_or_none(raw_data.get("reasoning")) or "",
        source="llm_analysis",
        status="ready",
        error=None,
        downgraded=False,
    )


# ---------------------------------------------------------------------------
# Post-processing: validation, downgrade, needs_info
# ---------------------------------------------------------------------------


def _apply_web_search_downgrade(
    result: TaskAnalysisResult,
    dept_defaults: _DeptDefaults,
) -> TaskAnalysisResult:
    """Downgrade ``research_with_web``/``research_publish_confluence`` when
 the department disables web search."""
    if dept_defaults.web_search_enabled:
        return result
    if result.workflow_type not in WEB_SEARCH_WORKFLOW_TYPES:
        return result

    _logger.info(
        "task_analyzer: downgrading workflow_type %s -> research_basic "
        "(dept web_search_enabled=False)",
        result.workflow_type,
    )
    return _replace(
        result,
        workflow_type="research_basic",
        web_search=False,
        downgraded=True,
        reasoning=(
            (result.reasoning + " | " if result.reasoning else "")
            + f"Downgraded from {result.workflow_type} to research_basic "
            "because the department's web_search_enabled flag is false."
        ),
    )


def _replace(result: TaskAnalysisResult, **changes: Any) -> TaskAnalysisResult:
    """Helper around:func:`dataclasses.replace` for frozen results."""
    from dataclasses import replace as _dc_replace

    return _dc_replace(result, **changes)


# ---------------------------------------------------------------------------
# Comment templates
# ---------------------------------------------------------------------------


def _build_invalid_workflow_comment(
    bad_value: str,
    issue_key: str,
) -> str:
    """Build the Jira rejection comment for an invalid workflow_type."""
    allowed = ", ".join(sorted(VALID_WORKFLOW_TYPES))
    return (
        f"❌ Görev (`{issue_key}`) reddedildi: geçersiz workflow_type "
        f"`{bad_value}`.\n\n"
        f"Geçerli değerler:\n{allowed}\n\n"
        "Lütfen task description'ını veya YAML front-matter bloğunu "
        "düzeltip yeniden atayın."
    )


def _build_needs_info_comment(missing: list[str], issue_key: str) -> str:
    """Build the Jira comment posted when confidence < threshold."""
    if missing:
        bullets = "\n".join(f"• {field}" for field in missing)
        body = (
            "🤖 Görevi başlatabilmem için aşağıdaki bilgilere ihtiyacım var:\n\n"
            f"{bullets}\n\n"
            "Lütfen bu yorumun altına bilgileri yazın."
        )
    else:
        body = (
            "🤖 Görev tanımı yetersiz, devam edebilmem için ek bilgi "
            "gerekiyor. Lütfen bu yorumun altına detayları yazın."
        )
    return body


def _build_missing_execution_command_comment(
    workflow_type: str,
    issue_key: str,
) -> str:
    """Build the Jira comment for execution workflows with no command."""
    return (
        f"Bu gorev (`{issue_key}`) `{workflow_type}` olarak calisacak, "
        "ama calistirilacak komut belirtilmemis.\n\n"
        "Lutfen task aciklamasindaki `ai-bot` bloguna su alanlardan "
        "birini ekleyin: `test_command`, `execution_command`, `command` "
        "veya `script`.\n\n"
        "Ornek:\n"
        "```yaml\n"
        "ai-bot:\n"
        f"  workflow_type: {workflow_type}\n"
        " test_command: \"pytest -q\"\n"
        "```\n"
        "Bu yoruma cevap geldikten sonra webhook workflow'u tekrar "
        "tetikleyecek."
    )


def _build_downgrade_comment(original: str, issue_key: str) -> str:
    """Notify the reporter that web_search was disabled and we downgraded."""
    return (
        f"ℹ️ Görev (`{issue_key}`) `{original}` olarak istenmişti ancak "
        "departman için web araması devre dışı. Görev `research_basic` "
        "olarak çalıştırılacak (yalnızca dahili bilgi)."
    )


def _build_frontmatter_warning_comment(
    parse_errors: list[str],
    issue_key: str,
) -> str:
    """Build the Jira warning comment for invalid YAML override fields.

 Implements (sister contract): when
 the YAML front-matter contains one or more invalid field values
 the offending fields are silently dropped to ``None`` and we
 surface a single advisory comment so the reporter can spot the
 typo. The comment lists every entry in ``parse_errors`` verbatim
 — the parser already prefixes each line with the field name (eg.
 ``cleanup: 'yes' is not valid (allowed: ['always', 'never',
 'on_success'])``) so the comment body is human-readable as-is.

 The dept defaults take over for the dropped fields, so the
 comment also reminds the reporter that defaults will be used.
 """
    bullets = "\n".join(f"• {err}" for err in parse_errors)
    return (
        f"⚠️ Görev (`{issue_key}`) açıklamasındaki YAML override "
        "bloğunda geçersiz alanlar tespit edildi:\n\n"
        f"{bullets}\n\n"
        "Bu alanlar yok sayılacak ve departman varsayılanları "
        "kullanılacak. Lütfen değerleri düzelterek tekrar deneyin."
    )


def _build_epic_needs_subtasks_comment(issue_key: str) -> str:
    """Build the Jira comment when an Epic has no subtasks.

 Implements — when the issue is an Epic but has
 no subtasks, the analyzer posts a needs_info comment asking the
 reporter to add subtasks or confirm they want the Epic split.
 """
    return (
        f"🤖 Bu Epic (`{issue_key}`) için subtask eklemediniz — "
        "ayrı task'lara bölmek mi istersiniz?\n\n"
        "Lütfen Epic'e subtask ekleyin veya bu yorumun altına "
        "nasıl ilerlememi istediğinizi yazın."
    )


# ---------------------------------------------------------------------------
# Epic auto-detect 
# ---------------------------------------------------------------------------


def _is_epic_issue(input: TaskAnalysisInput) -> bool:
    """Check whether the issue is an Epic based on ``issue_meta``.

 Returns ``True`` when ``issue_meta["issuetype"]["name"]`` (case-
 insensitive) equals ``"epic"``. Gracefully returns ``False`` when
 the metadata is missing or malformed.
 """
    issue_meta = input.issue_meta
    if not issue_meta:
        return False
    issuetype = issue_meta.get("issuetype")
    if not isinstance(issuetype, dict):
        return False
    name = issuetype.get("name")
    if not isinstance(name, str):
        return False
    return name.strip().lower() == "epic"


def _get_epic_subtasks(input: TaskAnalysisInput) -> list[dict[str, Any]]:
    """Extract the subtask list from ``issue_meta``.

 Returns an empty list when the field is missing or not a list.
 """
    issue_meta = input.issue_meta
    if not issue_meta:
        return []
    subtasks = issue_meta.get("subtasks")
    if isinstance(subtasks, list):
        return [s for s in subtasks if isinstance(s, dict)]
    return []


def _result_for_epic_multi_step(input: TaskAnalysisInput) -> TaskAnalysisResult:
    """Build a deterministic ``multi_step`` result for an Epic with subtasks.

 Implements — Epic + non-empty subtask list →
 ``workflow_type="multi_step"``, ``confidence=1.0``,
 ``source="epic_auto_detect"``, ``accepted=True``.

 The subtask count is stored in ``output_actions`` as a metadata
 entry (``{"_meta": "epic_auto_detect", "subtask_count": N}``) so
 the workflow can include it in the ``epic_auto_detected_multi_step``
 audit payload.
 """
    subtask_count = len(_get_epic_subtasks(input))
    return TaskAnalysisResult(
        accepted=True,
        workflow_type="multi_step",
        needs_ssh=False,
        needs_docker=False,
        execution_command=None,
        repo=None,
        branch=None,
        cleanup_policy="on_success",
        timeout_seconds=None,
        web_search=False,
        output_actions=[
            {"_meta": "epic_auto_detect", "subtask_count": subtask_count},
        ],
        confidence=1.0,
        missing_fields=[],
        reasoning="Epic with subtasks — deterministic multi_step assignment.",
        source="epic_auto_detect",
        status="ready",
        error=None,
        downgraded=False,
    )


def _result_for_epic_needs_subtasks() -> TaskAnalysisResult:
    """Build a ``needs_info`` result for an Epic with no subtasks.

 Implements — Epic + empty subtask list →
 ``status="needs_info"``, ``missing_fields=["subtasks"]``,
 ``source="epic_auto_detect"``.
 """
    return TaskAnalysisResult(
        accepted=False,
        workflow_type="multi_step",
        needs_ssh=False,
        needs_docker=False,
        execution_command=None,
        repo=None,
        branch=None,
        cleanup_policy="on_success",
        timeout_seconds=None,
        web_search=False,
        output_actions=[],
        confidence=1.0,
        missing_fields=["subtasks"],
        reasoning="Epic without subtasks — needs_info to request subtask breakdown.",
        source="epic_auto_detect",
        status="needs_info",
        error=None,
        downgraded=False,
    )


# ---------------------------------------------------------------------------
# Core activity
# ---------------------------------------------------------------------------


@activity.defn(name="analyze_task")
async def analyze_task(input: TaskAnalysisInput) -> TaskAnalysisResult:
    """Analyse a Jira task and decide which workflow_type to run.

 Priority logic:

 1. **YAML front-matter** — if the description starts with a valid
 ``ai-bot:`` block, use those values directly.
 1b. **Epic auto-detect** — if no YAML front-matter specifies a
 workflow_type and the issue is an Epic (``issuetype.name ==
 "Epic"``), deterministically assign ``multi_step`` when
 subtasks exist, or ``needs_info`` when they don't.
 2. **LLM analysis** — render ``task_analysis.md``, call the LLM,
 parse JSON.
 3. **Validate** — the resolved ``workflow_type`` must be in:data:`VALID_WORKFLOW_TYPES`. Otherwise the task is rejected
 and a Jira comment is posted.
 4. **Web-search downgrade** — when the dept disables web_search,
 ``research_with_web`` / ``research_publish_confluence`` are
 downgraded to ``research_basic``.
 5. **Confidence gate** — if ``confidence < 0.7`` the activity
 transitions the task to ``needs_info`` and posts a comment
 listing ``missing_fields``..
 """
    # / — bind the inbound
    # ``trace_id`` onto the activity's contextvars context so every log
    # record emitted while the activity runs (including those from
    # nested helpers and the MCP-client request hook in
    # ``http_shared.client``) carries the same trace_id as the
    # originating webhook. The import is local so the activity module
    # stays importable even when ``observability`` is unavailable in
    # focused unit-test environments.
    if input.trace_id:
        try:
            from observability import set_trace_id

            set_trace_id(input.trace_id)
        except Exception:  # pragma: no cover - defensive
            pass

    activity.logger.info(
        "task_analyzer: analysing %s (dept=%s, trace=%s)",
        input.issue_key,
        input.dept_id,
        input.trace_id,
    )

    dept_defaults = _extract_dept_defaults(input.dept_config)

    # ------------------------------------------------------------------
    # Step 1: YAML front-matter 
    # ------------------------------------------------------------------
    parsed_fm = _try_parse_frontmatter(input.description)

    # — surface a single advisory comment when the YAML block
    # contained one or more invalid values. The parser drops each
    # offending field to ``None`` (see description_parser.py) and
    # records a per-field error in ``parse_errors``; we forward the
    # list to Jira so the reporter can fix the override block. The
    # comment is posted regardless of whether the YAML branch is
    # taken or we fall through to the LLM, because either way the
    # user supplied invalid override values.
    fm_parse_errors: list[str] = list(getattr(parsed_fm, "parse_errors", []) or [])
    if fm_parse_errors:
        activity.logger.info(
            "task_analyzer: YAML front-matter for %s carries %d parse "
            "error(s) — posting advisory comment",
            input.issue_key,
            len(fm_parse_errors),
        )
        await _safe_post_comment(
            input.issue_key,
            _build_frontmatter_warning_comment(
                fm_parse_errors, input.issue_key
            ),
            input.dept_id,
        )

    # ------------------------------------------------------------------
    # Priority invariant:
    # 1. Front-matter with workflow_type → use it (highest priority)
    # 2. Epic auto-detect (no front-matter workflow_type + issuetype=Epic)
    # → deterministic multi_step
    # 3. LLM analysis (fallback for everything else)
    # The elif chain below guarantees that Epic auto-detect NEVER
    # overrides an explicit front-matter workflow_type.
    # ------------------------------------------------------------------
    if _frontmatter_has_workflow_type(parsed_fm):
        activity.logger.info(
            "task_analyzer: YAML front-matter found for %s — "
            "skipping LLM (workflow_type=%s)",
            input.issue_key,
            getattr(parsed_fm, "workflow_type", None),
        )
        result = _result_from_frontmatter(
            parsed_fm,
            input=input,
            dept_defaults=dept_defaults,
        )
    elif _is_epic_issue(input):
        # ------------------------------------------------------------------
        # Step 1b: Epic auto-detect 
        # ------------------------------------------------------------------
        # When the issue is an Epic and no YAML front-matter specifies
        # a workflow_type, we bypass the LLM entirely and deterministically
        # assign ``multi_step`` (if subtasks exist) or ``needs_info``
        # (if subtasks are empty).
        subtasks = _get_epic_subtasks(input)
        if subtasks:
            activity.logger.info(
                "task_analyzer: Epic auto-detect for %s — %d subtask(s) "
                "found, assigning multi_step deterministically",
                input.issue_key,
                len(subtasks),
            )
            result = _result_for_epic_multi_step(input)
        else:
            activity.logger.info(
                "task_analyzer: Epic auto-detect for %s — no subtasks, "
                "posting needs_info comment",
                input.issue_key,
            )
            await _safe_post_comment(
                input.issue_key,
                _build_epic_needs_subtasks_comment(input.issue_key),
                input.dept_id,
            )
            result = _result_for_epic_needs_subtasks()
        # Epic auto-detect results skip validation steps 3-5 because
        # they are deterministic with confidence=1.0 and workflow_type
        # is always "multi_step" (a valid value).
        return result
    else:
        # ------------------------------------------------------------------
        # Step 2: LLM analysis 
        # ------------------------------------------------------------------
        result = await _run_llm_analysis(input, dept_defaults)

    # ------------------------------------------------------------------
    # Step 3: Validate workflow_type 
    # ------------------------------------------------------------------
    if (
        result.workflow_type is None
        or result.workflow_type not in VALID_WORKFLOW_TYPES
    ):
        bad = result.workflow_type or "<missing>"
        activity.logger.warning(
            "task_analyzer: invalid workflow_type %r for %s — rejecting",
            bad,
            input.issue_key,
        )
        await _safe_post_comment(
            input.issue_key,
            _build_invalid_workflow_comment(bad, input.issue_key),
            input.dept_id,
        )
        return _replace(
            result,
            accepted=False,
            status="rejected",
            error=f"Invalid workflow_type: {bad!r}",
        )

    # ------------------------------------------------------------------
    # Step 4: Web-search downgrade 
    # ------------------------------------------------------------------
    original_wf = result.workflow_type
    result = _apply_web_search_downgrade(result, dept_defaults)
    if result.downgraded:
        await _safe_post_comment(
            input.issue_key,
            _build_downgrade_comment(original_wf, input.issue_key),
            input.dept_id,
        )

    if (
        result.workflow_type in EXECUTION_COMMAND_WORKFLOW_TYPES
        and not result.execution_command
    ):
        missing = list(dict.fromkeys([*result.missing_fields, "test_command"]))
        await _safe_post_comment(
            input.issue_key,
            _build_missing_execution_command_comment(
                result.workflow_type, input.issue_key
            ),
            input.dept_id,
        )
        return _replace(
            result,
            accepted=False,
            status="needs_info",
            missing_fields=missing,
            reasoning=(
                (result.reasoning + " | " if result.reasoning else "")
                + "Execution workflow requires a non-empty test_command."
            ),
        )

    # ------------------------------------------------------------------
    # Step 5: Confidence gate 
    # ------------------------------------------------------------------
    if result.confidence < CONFIDENCE_THRESHOLD:
        activity.logger.info(
            "task_analyzer: confidence %.2f < %.2f for %s — needs_info",
            result.confidence,
            CONFIDENCE_THRESHOLD,
            input.issue_key,
        )
        await _safe_post_comment(
            input.issue_key,
            _build_needs_info_comment(result.missing_fields, input.issue_key),
            input.dept_id,
        )
        return _replace(
            result,
            accepted=False,
            status="needs_info",
        )

    activity.logger.info(
        "task_analyzer: %s ready — workflow_type=%s, confidence=%.2f, "
        "source=%s, downgraded=%s",
        input.issue_key,
        result.workflow_type,
        result.confidence,
        result.source,
        result.downgraded,
    )
    return result


# ---------------------------------------------------------------------------
# LLM branch
# ---------------------------------------------------------------------------


async def _run_llm_analysis(
    input: TaskAnalysisInput,
    dept_defaults: _DeptDefaults,
) -> TaskAnalysisResult:
    """Render the prompt, call the LLM, parse the response.

 Failures are converted into a low-confidence result so the caller
 sees a normal ``needs_info`` flow rather than an unhandled
 exception (— the "needs_info" path is the
 catch-all when the analyzer cannot be confident).
 """
    try:
        template = _prompt_cache.load(get_prompt_path())
    except FileNotFoundError as exc:
        _logger.error(
            "task_analyzer: prompt file missing at %s — cannot analyse %s",
            get_prompt_path(),
            input.issue_key,
        )
        # Without a prompt we cannot drive the LLM — surface a
        # rejection-shaped result so the caller posts a Jira comment.
        return TaskAnalysisResult(
            accepted=False,
            workflow_type=None,
            needs_ssh=False,
            needs_docker=False,
            execution_command=None,
            repo=None,
            branch=None,
            cleanup_policy=dept_defaults.cleanup_policy,
            timeout_seconds=dept_defaults.default_timeout_seconds,
            web_search=False,
            output_actions=[],
            confidence=0.0,
            missing_fields=["task_analysis_prompt"],
            reasoning=f"Prompt file not found: {exc}",
            source="llm_analysis",
            status="rejected",
            error=f"Prompt file not found: {exc}",
        )

    prompt = _render_prompt(template, input)

    caller = get_llm_caller()

    try:
        raw_response = await caller.complete(prompt, dept_id=input.dept_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "task_analyzer: LLM call failed for %s: %s",
            input.issue_key,
            exc,
        )
        return TaskAnalysisResult(
            accepted=True,  # caller may still post needs_info comment
            workflow_type=None,
            needs_ssh=False,
            needs_docker=False,
            execution_command=None,
            repo=None,
            branch=None,
            cleanup_policy=dept_defaults.cleanup_policy,
            timeout_seconds=dept_defaults.default_timeout_seconds,
            web_search=False,
            output_actions=[],
            confidence=0.0,
            missing_fields=["llm_analysis_failed"],
            reasoning=f"LLM call failed: {exc}",
            source="llm_analysis",
            status="ready",
            error=str(exc),
        )

    try:
        parsed = _parse_json_response(raw_response)
    except TaskAnalysisError as exc:
        _logger.warning(
            "task_analyzer: invalid LLM JSON for %s: %s",
            input.issue_key,
            exc,
        )
        return TaskAnalysisResult(
            accepted=True,
            workflow_type=None,
            needs_ssh=False,
            needs_docker=False,
            execution_command=None,
            repo=None,
            branch=None,
            cleanup_policy=dept_defaults.cleanup_policy,
            timeout_seconds=dept_defaults.default_timeout_seconds,
            web_search=False,
            output_actions=[],
            confidence=0.0,
            missing_fields=["invalid_llm_json"],
            reasoning=str(exc),
            source="llm_analysis",
            status="ready",
            error=str(exc),
        )

    return _result_from_llm(parsed, dept_defaults=dept_defaults)


# ---------------------------------------------------------------------------
# Comment helper — never lets a comment failure abort the analysis
# ---------------------------------------------------------------------------


async def _safe_post_comment(issue_key: str, body: str, dept_id: str) -> None:
    """Post a Jira comment without bubbling failures back to the caller."""
    try:
        commenter = get_jira_commenter()
    except RuntimeError:
        # Commenter not wired (e.g. running in a unit test that
        # only exercises pure logic). Skip silently.
        _logger.debug(
            "task_analyzer: Jira commenter not wired — skipping "
            "comment for %s",
            issue_key,
        )
        return
    try:
        await commenter.add_comment(issue_key, body, dept_id=dept_id)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "task_analyzer: failed to post comment to %s: %s",
            issue_key,
            exc,
        )
