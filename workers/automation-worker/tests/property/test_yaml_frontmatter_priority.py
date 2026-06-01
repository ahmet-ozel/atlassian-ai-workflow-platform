"""Property test: YAML front-matter priority over LLM (Property 7).

Feature: platform-gap-fill, Property 7 — *For any* task description
containing a valid YAML front-matter block (with ``workflow_type``),
the parsed values SHALL be used directly and the LLM SHALL NOT be
invoked.

The complementary direction (Requirement 5.4 — "if YAML cannot be
parsed or is absent, the LLM SHALL be invoked") is exercised by a
second property in this module so the implication is tested in both
directions: ``yaml_present ⇒ ¬llm_called`` *and*
``yaml_absent ⇒ llm_called``.

**Validates: Requirements 5.3, 5.4, 11.1**
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror sibling property tests)
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities import task_analyzer  # noqa: E402
from automation_worker.activities.description_parser import (  # noqa: E402
    TIMEOUT_SECONDS_MAX,
    TIMEOUT_SECONDS_MIN,
    VALID_CLEANUP_POLICIES,
    VALID_WORKFLOW_TYPES,
)
from automation_worker.activities.task_analyzer import (  # noqa: E402
    TaskAnalysisInput,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
)


# ---------------------------------------------------------------------------
# In-memory fakes — record interactions so the property can assert on them
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Records LLM ``complete`` invocations.

    The property is precisely "no call" when YAML is present; we keep
    a list rather than just a counter so a counter-example is easy
    to inspect.
    """

    response: str = (
        '{"workflow_type": "code_change_with_test", "needs_ssh": true, '
        '"needs_docker": false, "repo": null, "branch": null, '
        '"test_command": "pytest -q", '
        '"cleanup_policy": "on_success", "timeout_seconds": 1800, '
        '"web_search": false, "output_actions": [], "confidence": 0.9, '
        '"missing_fields": [], "reasoning": "fake"}'
    )
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        return self.response


@dataclass
class _FakeCommenter:
    """No-op commenter — tests don't assert on Jira comments here."""

    comments: list[tuple[str, str, str]] = field(default_factory=list)

    async def add_comment(
        self,
        issue_key: str,
        body: str,
        *,
        dept_id: str,
    ) -> None:
        self.comments.append((issue_key, body, dept_id))


# ---------------------------------------------------------------------------
# Department config + prompt file used by every example
# ---------------------------------------------------------------------------


_DEFAULT_DEPT_CONFIG: dict[str, Any] = {
    "available_repos": ["org/backend", "org/frontend"],
    "available_spaces": ["TEAM"],
    "available_capabilities": ["jira_read", "jira_write"],
    "default_language": "tr",
    # Web search enabled so research_with_web doesn't downgrade —
    # the downgrade itself doesn't affect Property 7 but keeping the
    # flag on means the result objects survive equality checks.
    "web_search_enabled": True,
    "docker_defaults": {
        "cleanup_policy": "on_success",
        "default_timeout_seconds": 1800,
    },
}


# Create a tmp prompt file once at module import time so the LLM branch
# can read *something* if we ever land there (the negative test for
# R5.4 explicitly requires the LLM branch to succeed). We keep this
# file outside any pytest tmp_path because Hypothesis re-uses the same
# database across runs and the path must outlive any single example.
_TMP_PROMPT_FILE: Path = (
    Path(__file__).resolve().parent
    / "_property_tmp_task_analysis.md"
)
_TMP_PROMPT_FILE.write_text(
    "# Property test prompt\n\nReturn JSON.\n",
    encoding="utf-8",
)
set_prompt_path(_TMP_PROMPT_FILE)


# ---------------------------------------------------------------------------
# Hypothesis strategies — generators for valid YAML front-matter blocks
# ---------------------------------------------------------------------------


# Workflow types are drawn from the authoritative closed set so the
# YAML block is always *valid* (Property 7 requires a valid block).
_workflow_type_strategy = st.sampled_from(sorted(VALID_WORKFLOW_TYPES))

# Cleanup policies likewise.
_cleanup_strategy = st.sampled_from(sorted(VALID_CLEANUP_POLICIES))

# Simple identifier-shaped strings for repo / branch — kept narrow so
# we never accidentally produce YAML syntax inside the value (eg. a
# quote, ``---``, leading dash). The parser also strips whitespace, so
# we exclude leading/trailing spaces from the alphabet.
_repo_strategy = st.from_regex(
    r"[a-z][a-z0-9-]{0,15}/[a-z][a-z0-9-]{0,15}",
    fullmatch=True,
)
_branch_strategy = st.from_regex(
    r"[a-z][a-z0-9/_-]{0,30}",
    fullmatch=True,
)

# Timeout values inside the [60, 7200] window so the parser keeps them.
_timeout_strategy = st.integers(
    min_value=TIMEOUT_SECONDS_MIN,
    max_value=TIMEOUT_SECONDS_MAX,
)

# Free-form description body that follows the YAML block. Restricted to
# printable text without ``---`` so we can't accidentally introduce a
# second front-matter delimiter further down — that would be invisible
# to the parser anyway, but it keeps generated examples readable.
_body_text_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="-",
    ),
    max_size=200,
)

# Issue keys mirror the regex used elsewhere in the property suite.
_issue_key_strategy = st.from_regex(
    r"[A-Z]{2,5}-[1-9][0-9]{0,4}",
    fullmatch=True,
)


@st.composite
def _valid_frontmatter_description(draw: st.DrawFn) -> str:
    """Build a description starting with a *valid* ai-bot YAML block.

    ``workflow_type`` is always present so
    ``_frontmatter_has_workflow_type()`` returns True and the
    analyzer takes the deterministic path. All other fields are
    independently drawn so we exercise a broad slice of input space.
    """
    workflow_type = draw(_workflow_type_strategy)
    include_repo = draw(st.booleans())
    include_branch = draw(st.booleans())
    include_needs_ssh = draw(st.booleans())
    include_needs_docker = draw(st.booleans())
    include_cleanup = draw(st.booleans())
    include_timeout = draw(st.booleans())
    include_web_search = draw(st.booleans())
    body = draw(_body_text_strategy)

    lines: list[str] = ["ai-bot:", f"  workflow_type: {workflow_type}"]
    if workflow_type in {
        "code_change_with_test",
        "remote_ssh_test_only",
        "script_execute",
    }:
        lines.append("  test_command: pytest -q")
    if include_repo:
        lines.append(f"  repo: {draw(_repo_strategy)}")
    if include_branch:
        lines.append(f"  branch: {draw(_branch_strategy)}")
    if include_needs_ssh:
        lines.append(f"  needs_ssh: {str(draw(st.booleans())).lower()}")
    if include_needs_docker:
        lines.append(f"  needs_docker: {str(draw(st.booleans())).lower()}")
    if include_cleanup:
        lines.append(f"  cleanup: {draw(_cleanup_strategy)}")
    if include_timeout:
        lines.append(f"  timeout_seconds: {draw(_timeout_strategy)}")
    if include_web_search:
        lines.append(f"  web_search: {str(draw(st.booleans())).lower()}")

    yaml_body = "\n".join(lines)
    return f"---\n{yaml_body}\n---\n\n{body}"


# Descriptions guaranteed *not* to start with a front-matter block.
# We forbid the description from beginning with ``---`` so the regex
# in description_parser refuses to match.
_no_frontmatter_description = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="-",
    ),
    min_size=1,
    max_size=400,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wire_fakes() -> _FakeLLM:
    """Install fresh fakes and return the LLM recorder."""
    llm = _FakeLLM()
    set_llm_caller(llm)
    set_jira_commenter(_FakeCommenter())
    return llm


def _make_input(
    description: str,
    *,
    issue_key: str,
) -> TaskAnalysisInput:
    return TaskAnalysisInput(
        issue_key=issue_key,
        title="Hypothesis-generated task",
        description=description,
        labels=[],
        custom_fields={},
        dept_id="payments",
        dept_config=dict(_DEFAULT_DEPT_CONFIG),
        trace_id="trace-property-7",
    )


# ---------------------------------------------------------------------------
# Property 7 — valid YAML block ⇒ LLM not invoked, source=yaml_frontmatter
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    description=_valid_frontmatter_description(),
    issue_key=_issue_key_strategy,
)
def test_yaml_frontmatter_skips_llm(
    description: str,
    issue_key: str,
) -> None:
    """Validates: Requirements 5.3, 11.1.

    For any description starting with a valid ``ai-bot`` YAML block
    (carrying ``workflow_type``), the analyzer SHALL take the
    deterministic path: ``source == "yaml_frontmatter"`` and the LLM
    caller SHALL NOT be invoked.
    """
    fake_llm = _wire_fakes()

    result = asyncio.run(analyze_task(_make_input(description, issue_key=issue_key)))

    # Property 7 — LLM was never called.
    assert fake_llm.calls == [], (
        f"LLM was called {len(fake_llm.calls)} time(s) despite a valid "
        f"YAML front-matter block. Description prefix: "
        f"{description[:120]!r}"
    )

    # The deterministic path always reports its source as YAML.
    assert result.source == "yaml_frontmatter", (
        f"Expected source='yaml_frontmatter', got {result.source!r}. "
        f"Description prefix: {description[:120]!r}"
    )

    # YAML path is implicit-confidence 1.0 (no LLM uncertainty).
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# Requirement 5.4 — no front-matter ⇒ LLM IS invoked
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    description=_no_frontmatter_description,
    issue_key=_issue_key_strategy,
)
def test_no_frontmatter_invokes_llm(
    description: str,
    issue_key: str,
) -> None:
    """Validates: Requirement 5.4.

    The converse direction of Property 7: when the description does
    *not* contain a YAML front-matter block, the analyzer SHALL fall
    through to the LLM branch. This guarantees Property 7 is not
    trivially satisfied by an analyzer that simply never calls the LLM.
    """
    fake_llm = _wire_fakes()

    result = asyncio.run(analyze_task(_make_input(description, issue_key=issue_key)))

    # Exactly one LLM call per analysis when the YAML branch is bypassed.
    assert len(fake_llm.calls) == 1, (
        f"Expected exactly 1 LLM call when no YAML block is present, "
        f"got {len(fake_llm.calls)}. Description: {description[:120]!r}"
    )
    assert result.source == "llm_analysis"
