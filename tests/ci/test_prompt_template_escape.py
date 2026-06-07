"""CI gate - prompt template escape (ops work).


Every Markdown file under any ``prompts/`` directory MUST pass
:func:`prompts.validate.validate_template_format` so the boot-time
PromptLoader cannot reject it. A failure here means a prompt
edit slipped past local linting and would crash the service on
the next restart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prompts import PromptTemplateError, validate_template_format

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


def _prompt_files() -> list[Path]:
    """Return only the prompt files that go through PromptLoader.

 The validator pins the chat-assistant template variable contract
 (``KNOWN_TEMPLATE_VARS``); agent-runner / execution-runner
 prompts follow a richer Jinja2 dialect that the loader renders
 differently and is out of scope for this CI gate. Notification
 templates carry workflow-completion variables (``workflow_id``,
 ``error``, ``result_summary``) that the notification service's
 own renderer accepts; this gate deliberately keeps to the
 chat / orchestration prompts under ``platform/prompts/`` whose
 boot-time validator is :func:`validate_template_format`.
 """

    candidates: list[Path] = []
    chat_prompts = _PLATFORM_ROOT / "prompts"
    if not chat_prompts.is_dir():
        return []
    for md in chat_prompts.rglob("*.md"):
        rel_parts = md.relative_to(_PLATFORM_ROOT).parts
        # Skip the notifications subtree - its templates use a
        # different placeholder vocabulary owned by libs/notification.
        if "notifications" in rel_parts:
            continue
        candidates.append(md)
    return sorted(candidates)


@pytest.mark.parametrize("path", _prompt_files(), ids=lambda p: p.name)
def test_prompt_template_format(path: Path) -> None:
    body = path.read_text(encoding="utf-8")
    try:
        validate_template_format(body)
    except PromptTemplateError as exc:
        raise AssertionError(
            f"prompt template at {path.relative_to(_PLATFORM_ROOT)} "
            f"fails validate_template_format(): {exc}"
        ) from exc
