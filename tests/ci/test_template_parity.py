"""CI gate - PDF + notification template parity: file existence & Jinja2 syntax.


Verifies that the minimum required template set exists on disk,
each file has non-zero content, and each template is valid Jinja2 (can be
parsed by the Jinja2 Environment without syntax errors).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import BaseLoader, Environment, TemplateSyntaxError

# platform/ root (tests/ci/  tests/  platform/)
_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent

# minimum template set - relative to platform/prompts/
_REQUIRED_TEMPLATES: list[str] = [
    "pdf_templates/default.html.j2",
    "notifications/slack_failure.j2",
    "notifications/slack_success.j2",
    "notifications/email_failure.j2",
    "notifications/email_success.j2",
    "notifications/teams_failure.j2",
    "notifications/budget_alarm.j2",
]


def _template_path(relative: str) -> Path:
    """Resolve a template relative path to an absolute path."""
    return _PLATFORM_ROOT / "prompts" / relative


@pytest.mark.parametrize("template_rel", _REQUIRED_TEMPLATES)
def test_template_file_exists(template_rel: str) -> None:
    """Each required template file must exist on disk."""
    path = _template_path(template_rel)
    assert path.is_file(), (
        f"Required template file missing: {path}. "
        f"This file is part of the minimum template set."
    )


@pytest.mark.parametrize("template_rel", _REQUIRED_TEMPLATES)
def test_template_file_non_empty(template_rel: str) -> None:
    """Each required template file must have non-zero content."""
    path = _template_path(template_rel)
    if not path.is_file():
        pytest.skip(f"File does not exist: {path}")

    content = path.read_text(encoding="utf-8")
    assert len(content.strip()) > 0, (
        f"Template file is empty: {path}. "
        f"Each template must contain valid content."
    )


@pytest.mark.parametrize("template_rel", _REQUIRED_TEMPLATES)
def test_template_jinja2_syntax_valid(template_rel: str) -> None:
    """Each template must be parseable by Jinja2 without syntax errors.

 We parse the template source using a Jinja2 Environment. This catches
 syntax errors (unclosed blocks, invalid expressions, etc.) without
 requiring a full render context.
 """
    path = _template_path(template_rel)
    if not path.is_file():
        pytest.skip(f"File does not exist: {path}")

    source = path.read_text(encoding="utf-8")

    # Use a permissive environment - undefined variables are OK at parse time,
    # we only care about structural syntax validity.
    env = Environment(loader=BaseLoader(), autoescape=False)

    try:
        env.parse(source)
    except TemplateSyntaxError as exc:
        pytest.fail(
            f"Jinja2 syntax error in {template_rel}: {exc.message} "
            f"(line {exc.lineno}). all templates must be "
            f"syntactically valid Jinja2."
        )
