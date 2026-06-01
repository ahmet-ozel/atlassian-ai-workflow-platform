"""Property test: Workflow type validation.

Feature: platform-gap-fill, Property 9 — For any ``workflow_type`` value
(from YAML or LLM), the ``analyze_task`` activity SHALL accept it iff
the value exists in :data:`VALID_WORKFLOW_TYPES`; all other values
SHALL cause task rejection with a Jira comment posted.

The two complementary properties checked here are:

* **Acceptance** — every value drawn from ``VALID_WORKFLOW_TYPES``
  results in ``status == "ready"`` (when confidence ≥ 0.7 and the
  department has ``web_search_enabled = True`` so the downgrade rule
  is bypassed).
* **Rejection** — every text value whose stripped form is NOT in
  ``VALID_WORKFLOW_TYPES`` results in ``status == "rejected"`` and a
  Jira comment is posted reporting the invalid type.

**Validates: Requirements 5.7, 5.8**
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (matches the pattern used by other property tests
# in this package — keeps the module importable without requiring an
# editable install).
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.task_analyzer import (  # noqa: E402
    VALID_WORKFLOW_TYPES,
    WEB_SEARCH_WORKFLOW_TYPES,
    TaskAnalysisInput,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
)


# ---------------------------------------------------------------------------
# One-time module setup: create a stub prompt file and point the analyzer
# at it.  The activity reads the prompt body to render the LLM input,
# but the fake LLM ignores it — so the file just needs to exist.
# ---------------------------------------------------------------------------

_TMP_PROMPT_DIR: Path = Path(tempfile.mkdtemp(prefix="task_analyzer_pbt_p9_"))
_PROMPT_PATH: Path = _TMP_PROMPT_DIR / "task_analysis.md"
_PROMPT_PATH.write_text(
    "# Stub prompt for Property 9 (workflow_type validation)\n",
    encoding="utf-8",
)
set_prompt_path(_PROMPT_PATH)


# ---------------------------------------------------------------------------
# In-memory fakes (same shape as the unit-test fakes — kept local so
# the property module is self-contained).
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Returns a scripted JSON payload, records every call."""

    response: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        return self.response


@dataclass
class _FakeCommenter:
    """Records Jira comments posted by the analyzer."""

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
# Test inputs
# ---------------------------------------------------------------------------


# A dept config with web_search_enabled=True keeps the analyzer on the
# straight path — the research_with_web / research_publish_confluence
# downgrade only fires when the dept disables web search.  Setting it
# to True here makes the *acceptance* property a pure validation test.
_DEPT_CONFIG: dict[str, Any] = {
    "available_repos": ["org/backend"],
    "available_capabilities": ["jira_read", "jira_write"],
    "web_search_enabled": True,
    "docker_defaults": {
        "cleanup_policy": "on_success",
        "default_timeout_seconds": 1800,
    },
}


def _make_input(issue_key: str = "PAY-9") -> TaskAnalysisInput:
    """Build a minimal TaskAnalysisInput with no YAML front-matter.

    The description is plain text so the YAML branch never fires —
    every example flows through the LLM path where the scripted fake
    controls ``workflow_type``.
    """
    return TaskAnalysisInput(
        issue_key=issue_key,
        title="Property 9 — workflow_type validation",
        description="Plain description with no YAML front-matter.",
        labels=[],
        custom_fields={},
        dept_id="payments",
        dept_config=dict(_DEPT_CONFIG),
        trace_id="trace-pbt-property-9",
    )


def _llm_payload(workflow_type: str, *, confidence: float = 0.9) -> str:
    """Render a full LLM JSON payload with the given workflow_type."""
    body = {
        "workflow_type": workflow_type,
        "needs_ssh": False,
        "needs_docker": False,
        "repo": "org/backend",
        "branch": "develop",
        "test_command": "pytest -q",
        "cleanup_policy": "on_success",
        "timeout_seconds": 1800,
        "web_search": False,
        "output_actions": [],
        "confidence": confidence,
        "missing_fields": [],
        "reasoning": "PBT scripted response",
    }
    return json.dumps(body)


# ---------------------------------------------------------------------------
# Property 9 (acceptance): every value in VALID_WORKFLOW_TYPES is accepted.
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(workflow_type=st.sampled_from(sorted(VALID_WORKFLOW_TYPES)))
def test_valid_workflow_types_are_accepted(workflow_type: str) -> None:
    """Validates: Requirements 5.7

    For any value drawn from VALID_WORKFLOW_TYPES (with confidence
    ≥ 0.7 and a dept that does not disable web search), ``analyze_task``
    produces ``status == "ready"`` and posts no rejection comment.
    """
    llm = _FakeLLM(response=_llm_payload(workflow_type))
    commenter = _FakeCommenter()
    set_llm_caller(llm)
    set_jira_commenter(commenter)

    result = asyncio.run(analyze_task(_make_input()))

    assert result.status == "ready", (
        f"valid workflow_type={workflow_type!r} should be accepted, "
        f"got status={result.status!r}"
    )
    assert result.accepted is True
    assert result.error is None
    # workflow_type is preserved exactly (no downgrade because the
    # dept enables web_search; the WEB_SEARCH_WORKFLOW_TYPES branch
    # only triggers when web_search_enabled is False).
    assert result.workflow_type == workflow_type
    # No rejection comment posted on the happy path.  Other comments
    # (e.g. downgrade notice) cannot be produced when web_search is
    # enabled, so the comment list must be empty.
    assert commenter.comments == [], (
        f"expected no comments for valid workflow_type={workflow_type!r}, "
        f"got {commenter.comments!r}"
    )


# ---------------------------------------------------------------------------
# Property 9 (rejection): every text value NOT in VALID_WORKFLOW_TYPES
# is rejected with a Jira comment.
# ---------------------------------------------------------------------------


# Generator: arbitrary text whose *stripped* form is not a valid
# workflow_type.  We strip because ``_result_from_llm`` itself trims
# whitespace before validating — without the filter, hypothesis would
# generate counter-examples like ``"  multi_step  "`` that are
# legitimately valid after normalisation.
_INVALID_WORKFLOW_TYPE = st.text(min_size=0, max_size=80).filter(
    lambda v: v.strip() not in VALID_WORKFLOW_TYPES
)


@settings(max_examples=100, deadline=None)
@given(workflow_type=_INVALID_WORKFLOW_TYPE)
def test_invalid_workflow_types_are_rejected_with_comment(
    workflow_type: str,
) -> None:
    """Validates: Requirements 5.8

    For any text value NOT in VALID_WORKFLOW_TYPES, ``analyze_task``
    produces ``status == "rejected"`` and posts a Jira comment listing
    the invalid value.  The original LLM confidence is irrelevant —
    validation runs *before* the confidence gate.
    """
    llm = _FakeLLM(response=_llm_payload(workflow_type))
    commenter = _FakeCommenter()
    set_llm_caller(llm)
    set_jira_commenter(commenter)

    result = asyncio.run(analyze_task(_make_input()))

    assert result.status == "rejected", (
        f"invalid workflow_type={workflow_type!r} should be rejected, "
        f"got status={result.status!r}"
    )
    assert result.accepted is False
    assert result.error is not None and "Invalid workflow_type" in result.error
    # Exactly one rejection comment is posted to the issue under test.
    assert len(commenter.comments) == 1, (
        f"expected one rejection comment, got {commenter.comments!r}"
    )
    issue_key, body, dept_id = commenter.comments[0]
    assert issue_key == "PAY-9"
    assert dept_id == "payments"
    # The comment text must mention rejection — either the literal
    # invalid value or the ``<missing>`` marker when the stripped form
    # is empty (``_str_or_none`` collapses whitespace-only inputs to
    # ``None`` so the comment shows ``<missing>``).
    expected_marker = workflow_type.strip() or "<missing>"
    assert expected_marker in body, (
        f"comment body {body!r} should mention {expected_marker!r}"
    )


# ---------------------------------------------------------------------------
# Sanity check — make sure the VALID_WORKFLOW_TYPES set still matches
# the spec (Requirement 5.7).  Catches accidental drift in the source
# of truth.
# ---------------------------------------------------------------------------


def test_valid_workflow_types_matches_spec() -> None:
    """The closed set in source matches Requirement 5.7's enumeration."""
    expected = frozenset({
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
    assert VALID_WORKFLOW_TYPES == expected
    # Sanity: web-search subset is a strict subset of the full set.
    assert WEB_SEARCH_WORKFLOW_TYPES <= VALID_WORKFLOW_TYPES
