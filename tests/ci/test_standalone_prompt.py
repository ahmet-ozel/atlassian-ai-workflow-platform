"""CI gate — standalone task creation prompt (`platform-mimari-ops` task 15.8).

**Validates: Requirement 9.3, R3.5**

The standalone task creation prompt lives at
``platform/prompts/task_creation_assistant.md`` (canonical source per
``platform-real-usage-gaps`` R3). The CI gate confirms the file ships
and contains the design-mandated behaviour markers so a future edit
cannot silently strip them.
"""

from __future__ import annotations

from pathlib import Path

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "task_creation_assistant.md"
)


def test_standalone_prompt_exists() -> None:
    assert _PROMPT_PATH.is_file(), (
        f"Missing standalone task creation prompt at {_PROMPT_PATH}. "
        "Task 15.3 ships this as the user-facing prompt for the "
        "Streamlit Task Creator page (R3.1 / Y10)."
    )


def test_standalone_prompt_carries_required_markers() -> None:
    body = _PROMPT_PATH.read_text(encoding="utf-8")
    assert len(body) > 1000, (
        f"Standalone prompt is too short ({len(body)} bytes) to be "
        "useful as a system prompt."
    )
    # The prompt must reference the canonical "single question" /
    # smart-defaults shape the chat assistant invokes when the user
    # opts into write-action via redirect_to_task_creator.
    needles = (
        "task",
        "departman",
        "workflow",
    )
    for needle in needles:
        assert needle.lower() in body.lower(), (
            f"Standalone prompt missing reference to {needle!r}; "
            "the document should anchor the user / assistant on "
            "the task-creator vocabulary."
        )
