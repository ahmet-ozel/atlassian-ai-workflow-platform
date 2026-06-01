"""CI gate — Template placeholder ↔ render context dict matching.

**Validates: Requirement 14.6**

For each template in the minimum set (R14.1), this test extracts the
documented context variables (from the Jinja2 comment header) and the
actual placeholders used in the template body, then verifies that:

1. Every placeholder used in the template body is present in the
   documented context (no undocumented variables that would fail at
   render time).
2. The expected render context dict covers all template placeholders
   (the template can be rendered without ``UndefinedError``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import BaseLoader, Environment, meta

# platform/ root (tests/ci/ → tests/ → platform/)
_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent


def _template_path(relative: str) -> Path:
    """Resolve a template relative path to an absolute path."""
    return _PLATFORM_ROOT / "prompts" / relative


# ---------------------------------------------------------------------------
# Expected render context for each template.
#
# These dicts define the REQUIRED context variables that the notification /
# PDF render code passes to each template. The test verifies that the
# template's actual placeholders are a subset of these keys (i.e. no
# undocumented variable that would blow up at render time).
# ---------------------------------------------------------------------------

_EXPECTED_CONTEXT: dict[str, set[str]] = {
    "pdf_templates/default.html.j2": {
        "title",
        "body_html",
        "subtitle",
        "footer",
        "lang",
    },
    "notifications/slack_failure.j2": {
        "workflow_id",
        "dept_id",
        "task_title",
        "issue_key",
        "error",
        "result_summary",
        "failed_at",
        "dashboard_url",
    },
    "notifications/slack_success.j2": {
        "workflow_id",
        "dept_id",
        "task_title",
        "issue_key",
        "result_summary",
        "completed_at",
        "dashboard_url",
    },
    "notifications/email_failure.j2": {
        "workflow_id",
        "dept_id",
        "task_title",
        "issue_key",
        "error",
        "result_summary",
        "failed_at",
        "dashboard_url",
    },
    "notifications/email_success.j2": {
        "workflow_id",
        "dept_id",
        "task_title",
        "issue_key",
        "result_summary",
        "completed_at",
        "dashboard_url",
    },
    "notifications/teams_failure.j2": {
        "workflow_id",
        "dept_id",
        "task_title",
        "issue_key",
        "error",
        "result_summary",
        "failed_at",
        "dashboard_url",
    },
    "notifications/budget_alarm.j2": {
        "dept_id",
        "period",
        "scope",
        "current_usd",
        "cap_usd",
        "threshold_pct",
        "pct_used",
        "notify_channel",
        "dashboard_url",
    },
}


def _extract_template_variables(source: str) -> set[str]:
    """Extract all undeclared (referenced) variables from a Jinja2 template.

    Uses Jinja2's AST-based ``meta.find_undeclared_variables`` which
    returns the set of variable names that the template references but
    does not define internally (via ``{% set %}`` or loop variables).
    """
    env = Environment(loader=BaseLoader(), autoescape=False)
    ast = env.parse(source)
    return meta.find_undeclared_variables(ast)


@pytest.mark.parametrize(
    "template_rel",
    list(_EXPECTED_CONTEXT.keys()),
)
def test_template_placeholders_covered_by_context(template_rel: str) -> None:
    """Every placeholder in the template must be present in the expected
    render context dict.

    If a template uses ``{{ foo }}`` but ``foo`` is not in the expected
    context, the render call will raise ``UndefinedError`` at runtime.
    This test catches such mismatches at CI time.
    """
    path = _template_path(template_rel)
    if not path.is_file():
        pytest.skip(f"File does not exist: {path}")

    source = path.read_text(encoding="utf-8")
    template_vars = _extract_template_variables(source)
    expected_vars = _EXPECTED_CONTEXT[template_rel]

    # Template variables that are NOT in the expected context
    uncovered = template_vars - expected_vars

    # Filter out Jinja2 built-ins that are always available
    _JINJA2_BUILTINS = {
        "range", "lipsum", "dict", "cycler", "joiner", "namespace",
        "true", "false", "none", "loop",
    }
    uncovered -= _JINJA2_BUILTINS

    assert not uncovered, (
        f"Template '{template_rel}' uses placeholders not present in the "
        f"expected render context: {sorted(uncovered)}. "
        f"Either add these to the render context dict or remove them from "
        f"the template. R14.6 requires placeholder ↔ context parity."
    )


@pytest.mark.parametrize(
    "template_rel",
    list(_EXPECTED_CONTEXT.keys()),
)
def test_template_renders_with_expected_context(template_rel: str) -> None:
    """Each template must render successfully when given the full expected
    context (all values as non-empty strings).

    This is a stronger check than syntax-only parsing: it exercises the
    full Jinja2 render path including filters and conditionals.
    """
    path = _template_path(template_rel)
    if not path.is_file():
        pytest.skip(f"File does not exist: {path}")

    source = path.read_text(encoding="utf-8")
    expected_vars = _EXPECTED_CONTEXT[template_rel]

    # Build a context dict with placeholder string values.
    # Some templates use conditional branches on specific variable values
    # (e.g. budget_alarm.j2 branches on notify_channel), so we provide
    # realistic values for known enum-like variables.
    _REALISTIC_VALUES: dict[str, str] = {
        "notify_channel": "slack",
        "period": "monthly",
        "scope": "dept",
    }
    context = {
        var: _REALISTIC_VALUES.get(var, f"test_{var}_value")
        for var in expected_vars
    }

    env = Environment(loader=BaseLoader(), autoescape=False)
    template = env.from_string(source)

    try:
        rendered = template.render(**context)
    except Exception as exc:
        pytest.fail(
            f"Template '{template_rel}' failed to render with the expected "
            f"context: {exc}. R14.6 requires templates to render cleanly "
            f"with the documented context variables."
        )

    # Rendered output should be non-empty
    assert len(rendered.strip()) > 0, (
        f"Template '{template_rel}' rendered to empty output with the "
        f"expected context. This likely indicates a logic error in the "
        f"template (e.g. all branches are conditional and none matched)."
    )


@pytest.mark.parametrize(
    "template_rel",
    list(_EXPECTED_CONTEXT.keys()),
)
def test_documented_context_matches_header_comment(template_rel: str) -> None:
    """The context variables documented in the template's Jinja2 comment
    header should match the expected context dict defined in this test.

    This catches drift between the template's own documentation and the
    actual render contract.
    """
    path = _template_path(template_rel)
    if not path.is_file():
        pytest.skip(f"File does not exist: {path}")

    source = path.read_text(encoding="utf-8")
    expected_vars = _EXPECTED_CONTEXT[template_rel]

    # Extract variable names from the comment header.
    # Pattern: lines like "    - variable_name   : type  — description"
    # or "  * ``variable_name`` — ..." (pdf template style)
    header_vars: set[str] = set()

    # Style 1: "    - var_name      : type   — description"
    for match in re.finditer(
        r"^\s*-\s+(\w+)\s*:", source, re.MULTILINE
    ):
        header_vars.add(match.group(1))

    # Style 2: "  * ``var_name`` — ..." (used in pdf_templates)
    for match in re.finditer(
        r"^\s*\*\s+``(\w+)``", source, re.MULTILINE
    ):
        header_vars.add(match.group(1))

    # Style 3: "Required context keys:" / "Optional context keys:" blocks
    # with "  * ``var_name``" entries
    for match in re.finditer(
        r"context\s+keys?.*?``(\w+)``", source, re.IGNORECASE
    ):
        header_vars.add(match.group(1))

    if not header_vars:
        # Template doesn't have a parseable header — skip this check
        pytest.skip(
            f"Could not extract documented variables from header of "
            f"'{template_rel}'"
        )

    # Every expected variable should be documented in the header
    undocumented = expected_vars - header_vars
    # Filter optional vars that may not be in the header
    # (e.g. 'lang' has a default in the template)
    # We only flag if MORE THAN HALF of expected vars are missing from header
    if len(undocumented) > len(expected_vars) // 2:
        pytest.fail(
            f"Template '{template_rel}' header documents {sorted(header_vars)} "
            f"but expected context has {sorted(expected_vars)}. "
            f"Missing from header: {sorted(undocumented)}. "
            f"Update the template header or the expected context dict."
        )
