"""Property test: Task analyzer confidence threshold gate.

Feature: platform-gap-fill, Property 8.

*For any* LLM analysis result with confidence < 0.7, the workflow
SHALL enter ``needs_info`` state and SHALL NOT proceed to execution.
Conversely, any confidence ≥ 0.7 SHALL produce ``status == "ready"``.

**Validates: Requirements 5.5, 5.6**

Strategy
--------
Generate ``confidence`` values uniformly in ``[0.0, 1.0]`` and feed
them to ``analyze_task`` via a fake LLM caller that returns a
syntactically valid analysis payload with the generated confidence.
The other LLM fields (workflow_type, repo, branch, ...) are kept
constant and valid so the only branch under test is the confidence
gate (Step 5 of the activity — Requirements 5.5, 5.6).

The threshold is exposed as
``task_analyzer.CONFIDENCE_THRESHOLD`` and the test compares against
it directly so a future tweak to the constant does not silently
invalidate the property.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirrors the unit-test layout)
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities import task_analyzer  # noqa: E402
from automation_worker.activities.task_analyzer import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    TaskAnalysisInput,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
)


# ---------------------------------------------------------------------------
# In-memory fakes — same shape as the unit-test fixtures
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Returns a scripted LLM response and records calls."""

    response: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        return self.response


@dataclass
class _FakeCommenter:
    """Records Jira comments without raising."""

    comments: list[tuple[str, str, str]] = field(default_factory=list)

    async def add_comment(
        self, issue_key: str, body: str, *, dept_id: str
    ) -> None:
        self.comments.append((issue_key, body, dept_id))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEPT_CONFIG: dict[str, Any] = {
    "available_repos": ["org/backend"],
    "available_capabilities": ["jira_read", "jira_write"],
    "default_language": "tr",
    # Keep web_search enabled so research_with_web does NOT downgrade
    # — but our payload uses a non-web type anyway so this is belt
    # and braces.
    "web_search_enabled": True,
    "docker_defaults": {
        "cleanup_policy": "on_success",
        "default_timeout_seconds": 1800,
    },
}


@pytest.fixture
def fake_llm() -> _FakeLLM:
    llm = _FakeLLM()
    set_llm_caller(llm)
    return llm


@pytest.fixture
def fake_commenter() -> _FakeCommenter:
    commenter = _FakeCommenter()
    set_jira_commenter(commenter)
    return commenter


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    """Tiny tmp prompt file so the analyzer does not need the real one."""
    p = tmp_path / "task_analysis.md"
    p.write_text(
        "# Test prompt\nReturn JSON with workflow_type field.\n",
        encoding="utf-8",
    )
    set_prompt_path(p)
    yield p
    set_prompt_path(task_analyzer.DEFAULT_PROMPT_PATH)


def _make_input() -> TaskAnalysisInput:
    return TaskAnalysisInput(
        issue_key="PAY-42",
        title="Add retry mechanism",
        description="Add a retry mechanism with exponential backoff.",
        labels=[],
        custom_fields={},
        dept_id="payments",
        dept_config=dict(_DEPT_CONFIG),
        trace_id="trace-prop-8",
    )


def _llm_payload(confidence: float) -> str:
    """Valid analysis JSON parameterised by ``confidence`` only."""
    return json.dumps(
        {
            "workflow_type": "code_change_with_test",
            "needs_ssh": True,
            "needs_docker": False,
            "repo": "org/backend",
            "branch": "develop",
            "test_command": "pytest -q",
            "cleanup_policy": "on_success",
            "timeout_seconds": 1800,
            "web_search": False,
            "output_actions": [
                {"type": "jira_comment", "payload": {"body": "✅ Done."}},
            ],
            "confidence": confidence,
            "missing_fields": ["repo"] if confidence < CONFIDENCE_THRESHOLD else [],
            "reasoning": "fixture-generated for property test",
        }
    )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


class TestConfidenceThresholdProperty:
    """**Property 8** — ``confidence`` decides ``needs_info`` vs ``ready``.

    **Validates: Requirements 5.5, 5.6**
    """

    @given(
        confidence=st.floats(
            min_value=0.0,
            max_value=1.0,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    @settings(
        max_examples=200,
        deadline=None,
        # The pytest fixtures are function-scoped (one fake LLM per
        # test invocation).  We explicitly clear ``calls`` /
        # ``comments`` at the start of every generated example so the
        # shared fixture is safe.  Suppress the Hypothesis health
        # check that would otherwise reject this pattern.
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_confidence_threshold_gates_status(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
        confidence: float,
    ) -> None:
        """For all c in [0,1]: c < 0.7 ⇒ needs_info; c ≥ 0.7 ⇒ ready.

        **Validates: Requirements 5.5, 5.6**
        """
        # Reset call/comment history for this example so a single
        # run does not see state from a previous example.
        fake_llm.calls.clear()
        fake_commenter.comments.clear()
        fake_llm.response = _llm_payload(confidence)

        result = asyncio.run(analyze_task(_make_input()))

        # The clamped confidence must round-trip: the LLM returned a
        # value in [0,1] and the analyzer leaves it untouched in that
        # range (Requirement 5.5 — confidence is the gate).
        assert 0.0 <= result.confidence <= 1.0
        assert result.confidence == pytest.approx(confidence)

        # The workflow_type is valid in every example so the only
        # branch under test is the confidence gate.
        assert result.workflow_type == "code_change_with_test"

        if confidence < CONFIDENCE_THRESHOLD:
            # Requirement 5.5 — below threshold ⇒ needs_info, not ready.
            assert result.status == "needs_info", (
                f"confidence={confidence!r} should route to needs_info "
                f"(threshold={CONFIDENCE_THRESHOLD}), got {result.status!r}"
            )
            assert result.accepted is False
            # The activity must post the needs_info comment so the
            # user can supply the missing fields (Requirement 4.1).
            assert len(fake_commenter.comments) == 1
            issue_key, _body, dept_id = fake_commenter.comments[0]
            assert issue_key == "PAY-42"
            assert dept_id == "payments"
        else:
            # Requirement 5.6 — at/above threshold ⇒ ready.
            assert result.status == "ready", (
                f"confidence={confidence!r} should proceed (ready) "
                f"(threshold={CONFIDENCE_THRESHOLD}), got {result.status!r}"
            )
            assert result.accepted is True
            # No comment should be posted on the happy path.
            assert fake_commenter.comments == []
