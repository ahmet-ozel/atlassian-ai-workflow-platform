"""CI gate — Jira issue template (ops work).


The standard Jira issue template ships under
``platform/docs/jira-templates/`` (or, when the dedicated directory
is absent, ``platform/prompts/task_creation_assistant.md`` is
the canonical fallback per ``real-usage gap work`` ). The gate
asserts at least one of the two ships and references the mandatory
fields the ``task-creator`` flow expects.
"""

from __future__ import annotations

from pathlib import Path

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_DOCS_DIR = _PLATFORM_ROOT / "docs"


def _candidate_templates() -> list[Path]:
    candidates: list[Path] = []
    template_dir = _DOCS_DIR / "jira-templates"
    if template_dir.is_dir():
        candidates.extend(p for p in template_dir.glob("*.md") if p.is_file())
    # Canonical prompt location (real-usage gap work )
    canonical = _PLATFORM_ROOT / "prompts" / "task_creation_assistant.md"
    if canonical.is_file():
        candidates.append(canonical)
    return candidates


def test_at_least_one_jira_template_ships() -> None:
    candidates = _candidate_templates()
    assert candidates, (
        "No Jira issue template found. the implementation ships either "
        "platform/docs/jira-templates/*.md or "
        "platform/prompts/task_creation_assistant.md."
    )


def test_jira_template_references_required_fields() -> None:
    candidates = _candidate_templates()
    assert candidates, "No Jira template candidates."
    combined = "\n".join(p.read_text(encoding="utf-8") for p in candidates)
    # Field names mandated by design.md §"task-creator" and the
    # assistant chat redirect_to_task_creator payload.
    for field in ("workflow_type", "department", "summary"):
        assert field in combined.lower(), (
            f"Jira template(s) do not reference the canonical field "
            f"{field!r}; without it the task-creator flow cannot map "
            "the user's intent to a Temporal workflow input."
        )
