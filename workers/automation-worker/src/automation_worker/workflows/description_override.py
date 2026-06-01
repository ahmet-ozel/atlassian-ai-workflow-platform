"""Pure helpers that merge a :class:`TaskAnalysisResult` description
override on top of department defaults.

This module hosts the *deterministic, side-effect-free* logic used by
:class:`AutomationWorkflow` to lift the description-override fields the
analyser carries (``cleanup_policy``, ``timeout_seconds``, ``web_search``,
``repo``, ``branch``, ``output_actions``) into the structured envelopes
consumed by downstream child workflows.

Why a separate module?
----------------------

* The merge logic is completely pure — it operates on plain dataclasses
  and primitive values.  Keeping it out of
  ``automation_workflow.py`` lets the workflow body stay focused on
  Temporal-flavoured wiring (``execute_activity`` / signal handling),
  and lets unit tests cover the merge contract without instantiating
  the workflow.
* Temporal's determinism contract treats helpers imported by the
  workflow as ``unsafe.imports_passed_through()`` — splitting the
  helpers into a dedicated module documents the boundary explicitly
  and lets static AST scanners (eg. ``tests/property/
  test_workflow_determinism_static.py``) ignore the file.

Validates Requirements
----------------------

* **R5.1–R5.10** — analyser drives the workflow_type / capability
  routing that ``apply_description_override`` consumes.
* **R11.1–R11.8** — per-task description override fields applied on
  top of dept defaults; invalid YAML field values are filtered by
  the description parser, so this layer only performs the merge.

The companion module :mod:`automation_worker.activities.task_analyzer`
owns the *parsing* and *validation* of the override fields; this
module owns the *merge* (analyser output → child workflow envelope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — typing only
    # ``TaskAnalysisResult`` lives in the activity module; importing
    # it at runtime would force the workflow module to drag in the
    # activity-side deps.  Type-checking time is fine — the import is
    # eliminated at runtime by ``TYPE_CHECKING``.
    from automation_worker.activities.task_analyzer import (
        TaskAnalysisResult,
    )


__all__: tuple[str, ...] = (
    "DescriptionOverride",
    "build_description_override",
    "to_llm_analysis_result",
)


# ---------------------------------------------------------------------------
# Override envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DescriptionOverride:
    """Distilled set of override values consumed by child workflows.

    Each field carries either an analyser-supplied value (YAML
    front-matter or LLM output) or ``None`` to indicate the dept
    default should win.  The merge is *non-destructive* — the
    workflow keeps its dept defaults around and only overrides the
    specific fields the analyser produced.

    Attributes
    ----------
    cleanup_policy:
        ``"on_success"`` / ``"always"`` / ``"never"``.  Validated
        upstream by :mod:`description_parser` and the LLM result
        coercer; this layer only stores the already-validated value.
    timeout_seconds:
        Per-task timeout.  Validated to ``[60, 7200]`` upstream.
    web_search:
        Whether the workflow body should enable web search.  Only
        meaningful for research-flavoured workflow types; the dept
        ``web_search_enabled`` flag still wins (the analyser already
        downgrades :attr:`workflow_type` accordingly — see
        Requirement 5.10).
    target_repo:
        Repository slug or ``None``.  Mirrors the structured
        ``AI Bot Repo`` Jira custom field with equal priority — the
        analyser handles the ranking before populating this field.
    target_branch:
        Branch name or ``None``.  ``"auto"`` is materialised to the
        dept's :attr:`repo_mappings.default_branch` upstream of the
        merge (analyser side); this layer never sees the literal.
    output_actions:
        Tuple of ``(kind, payload-pairs)`` action descriptors.  Empty
        when the analyser did not surface explicit output actions
        (the LLM's ``output_actions`` field defaults to an empty
        list).  The workflow translates each entry to an
        :class:`temporal_shared.messages.OutputAction` at dispatch
        time.
    workflow_type:
        Resolved workflow_type — already routed through the
        web-search downgrade and the ``VALID_WORKFLOW_TYPES`` set.
    """

    workflow_type: str
    cleanup_policy: str | None = None
    timeout_seconds: int | None = None
    web_search: bool = False
    target_repo: str | None = None
    target_branch: str | None = None
    output_actions: tuple[tuple[str, tuple[tuple[str, object], ...]], ...] = ()
    execution_command: str | None = None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_description_override(
    analysis: "TaskAnalysisResult",
) -> DescriptionOverride:
    """Distill a :class:`TaskAnalysisResult` into a
    :class:`DescriptionOverride`.

    The analyser already merged dept defaults into its returned
    :class:`TaskAnalysisResult` (see ``_result_from_frontmatter`` /
    ``_result_from_llm`` in :mod:`task_analyzer`), so this helper is
    primarily a *projection* — it keeps the workflow body blissfully
    unaware of the activity-side data class shape.

    Pure / replay-safe: only reads attributes from the input.

    Validates Requirements: 11.1–11.7.
    """

    # ``output_actions`` is a list of dicts on the analyser side
    # (``{"type": "...", "params": {...}}`` shape — see
    # ``description_parser._coerce_output``).  We freeze the
    # mapping into a tuple-of-pairs envelope so the override
    # itself is immutable / hashable / replay-safe.
    actions: list[tuple[str, tuple[tuple[str, object], ...]]] = []
    alias_map = {
        "bitbucket_put_file": "bitbucket_commit",
        "bitbucket_pr": "bitbucket_create_pr",
        "confluence_page": "confluence_create_page",
    }
    for raw in analysis.output_actions or ():
        if not isinstance(raw, dict):
            # Defensive — the analyser already validated the shape but
            # a future change could regress.  Skip unknown entries
            # rather than crash the workflow.
            continue
        kind = raw.get("type")
        if not isinstance(kind, str) or not kind:
            continue
        kind = alias_map.get(kind, kind)
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        # Sort keys so the wire shape is deterministic across replays.
        param_pairs: tuple[tuple[str, object], ...] = tuple(
            (str(k), params[k]) for k in sorted(params.keys())
        )
        actions.append((kind, param_pairs))

    return DescriptionOverride(
        workflow_type=analysis.workflow_type or "",
        cleanup_policy=analysis.cleanup_policy or None,
        timeout_seconds=analysis.timeout_seconds,
        web_search=bool(analysis.web_search),
        target_repo=analysis.repo,
        target_branch=analysis.branch,
        output_actions=tuple(actions),
        execution_command=getattr(analysis, "execution_command", None),
    )


def to_llm_analysis_result(
    analysis: "TaskAnalysisResult",
) -> Any:
    """Adapt a :class:`TaskAnalysisResult` to the
    :class:`temporal_shared.messages.LlmAnalysisResult` shape consumed
    by the existing capability-gate / branch-rule / dispatch path.

    The two dataclasses describe the *same* logical decision but with
    different field naming and confidence representations — the
    analyser uses a numeric ``confidence ∈ [0, 1]`` (R5.5 threshold
    0.7), whereas :class:`LlmAnalysisResult` (older spec) uses the
    literal ``"high" | "medium" | "low"`` triple.  This bridge keeps
    the existing AutomationWorkflow downstream pipeline (capability
    gate, branch_pattern_rules, child dispatch) untouched.

    Confidence mapping
    ------------------

    * ``confidence ≥ 0.85`` → ``"high"``
    * ``0.7 ≤ confidence < 0.85`` → ``"medium"``
    * ``confidence < 0.7`` → ``"low"`` (the workflow does not normally
      reach this branch because the analyser intercepts low-
      confidence results into its own ``needs_info`` flow before this
      bridge is called)

    The mapping is inclusive at 0.85 to match the ``CONFIDENCE_THRESHOLD``
    semantics in the analyser (boundary value proceeds, see
    Requirement 5.6).

    Pure / replay-safe.
    """

    # Local imports inside the helper keep the static AST determinism
    # checks happy (no top-level activity / message imports here —
    # ``LlmAnalysisResult`` is only referenced through this function
    # and the workflow's existing sandbox-passed-through import).
    from temporal_shared.messages import (  # noqa: PLC0415
        BEST_EFFORT_OUTPUT_ACTION_KINDS,
        CRITICAL_OUTPUT_ACTION_KINDS,
        LlmAnalysisResult,
        OutputAction,
    )

    score = float(analysis.confidence or 0.0)
    if score >= 0.85:
        confidence_literal: str = "high"
    elif score >= 0.7:
        confidence_literal = "medium"
    else:
        confidence_literal = "low"

    alias_map = {
        "bitbucket_put_file": "bitbucket_commit",
        "bitbucket_pr": "bitbucket_create_pr",
        "confluence_page": "confluence_create_page",
    }

    # Map ``output_actions`` (list of dicts) onto the strongly-typed
    # :class:`OutputAction` tuple expected by ``LlmAnalysisResult``.
    # Severity is omitted from the analyser output (the description
    # parser does not gate on severity) — we default unknown action
    # kinds to ``"best_effort"`` so an upstream change introducing
    # critical actions has to opt in explicitly.
    out_actions: list[OutputAction] = []
    for raw in analysis.output_actions or ():
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if not isinstance(kind, str) or not kind:
            continue
        kind = alias_map.get(kind, kind)
        kind = alias_map.get(kind, kind)
        params = raw.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        # Sort for deterministic replay-safe wire shape.
        payload_pairs = tuple(
            (str(k), params[k]) for k in sorted(params.keys())
        )
        if kind in CRITICAL_OUTPUT_ACTION_KINDS:
            severity = "critical"
        elif kind in BEST_EFFORT_OUTPUT_ACTION_KINDS:
            severity = "best_effort"
        else:
            continue
        out_actions.append(
            OutputAction(
                kind=kind,  # type: ignore[arg-type]
                severity=severity,  # type: ignore[arg-type]
                payload=payload_pairs,
            )
        )

    # ``needs_info_questions`` is empty here because the analyser
    # already drained the needs_info loop before returning a ``ready``
    # result (see ``analyze_task`` post-processing — confidence < 0.7
    # paths short-circuit into a ``needs_info`` status which the
    # workflow handles via its own front-door branch).  We pass the
    # ``missing_fields`` through verbatim for completeness — operator
    # log lines downstream still benefit from the list.
    questions = tuple(analysis.missing_fields or ())

    return LlmAnalysisResult(
        workflow_type=analysis.workflow_type or "",
        confidence=confidence_literal,  # type: ignore[arg-type]
        target_repo=analysis.repo,
        target_branch=analysis.branch,
        target_space=None,
        target_page_id=None,
        title="",  # analyser does not surface a title
        rationale=analysis.reasoning or "",
        output_actions=tuple(out_actions),
        needs_info_questions=questions,
        token_usage=0,
        # EK2 propagate analyser decisions into the bridged shape so the
        # gateway's _child_args can wire them into ExecutionRunWorkflowInput.
        needs_docker=bool(getattr(analysis, "needs_docker", False)),
        needs_ssh=bool(getattr(analysis, "needs_ssh", False)),
        execution_command=getattr(analysis, "execution_command", None),
    )
