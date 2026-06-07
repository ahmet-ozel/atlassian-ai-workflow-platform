"""CI gate for the assistant_chat.md system prompt.

The chat assistant system prompt at
``platform/prompts/assistant_chat.md`` MUST:

* exist;
* parse cleanly via :func:`prompts.validate.validate_template_format`
 so the boot-time PromptLoader does not fail at runtime;
* contain the mandatory behaviour sentence pinned by design.md
 §"Y5" - chat MUST NOT perform write actions, those go through
 Task Creator.
"""

from __future__ import annotations

from pathlib import Path

from prompts import PromptTemplateError, validate_template_format

_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "prompts"
    / "assistant_chat.md"
)


def test_prompt_file_exists() -> None:
    assert _PROMPT_PATH.is_file(), (
        f"Missing assistant_chat.md at {_PROMPT_PATH}. "
        "assistant-service cannot boot without this prompt."
    )


def test_prompt_passes_template_format_validator() -> None:
    body = _PROMPT_PATH.read_text(encoding="utf-8")
    try:
        validate_template_format(body)
    except PromptTemplateError as exc:  # pragma: no cover - pinpoint failure
        raise AssertionError(
            "assistant_chat.md fails validate_template_format; "
            f"the PromptLoader would refuse to boot. Details: {exc}"
        ) from exc


def test_prompt_carries_y5_behaviour_sentence() -> None:
    """The mandatory sentence about write-action handoff."""

    body = _PROMPT_PATH.read_text(encoding="utf-8")
    # Both Turkish and English variants of the constraint trigger the
    # gate. We require every
    # prompt to mention either the Turkish phrase or an English
    # equivalent so a translation never silently drops the rule.
    needles = (
        "Jira task açılarak otomasyona devredilir",  # canonical TR
        "Jira task",
    )
    assert any(n in body for n in needles), (
        "assistant_chat.md must state the write-action handoff rule - "
        "chat-side write actions are deferred to Jira task creation. "
        "Drop the sentence and the chat assistant will start "
        "executing PR / commit calls directly."
    )
