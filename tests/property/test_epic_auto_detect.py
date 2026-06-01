"""Property test 13 — Epic Auto-Detect.

Spec: ``platform-real-usage-gaps`` — Property 13.

**Validates: Requirements 12.1, 12.2, 12.5, 12.7**

Background
----------

The ``analyze_task`` activity in
``automation_worker.activities.task_analyzer`` implements an Epic
auto-detect branch (Requirements 12.1, 12.2):

* When the issue's ``issuetype.name`` is ``"Epic"`` and no YAML
  front-matter specifies a ``workflow_type``, the activity bypasses
  the LLM entirely.
* If the Epic has ≥1 subtask → deterministic
  ``workflow_type="multi_step"``, ``confidence=1.0``,
  ``source="epic_auto_detect"``, ``status="ready"``.
* If the Epic has 0 subtasks → ``status="needs_info"``,
  ``missing_fields=["subtasks"]``, ``source="epic_auto_detect"``.
* Non-Epic issue types (Story, Task, Bug) → existing LLM path.
* If YAML front-matter sets ``workflow_type``, the auto-detect is
  bypassed regardless of issue type (front-matter > Epic auto-detect
  > LLM priority).

Strategy
--------

We use Hypothesis to generate random Jira issue metadata and verify
the four invariants:

(a) Epic + ≥1 subtask → ``multi_step`` with ``source="epic_auto_detect"``
(b) Epic + 0 subtask → ``needs_info`` with ``source="epic_auto_detect"``
(c) Story/Task/Bug → LLM path (``source="llm_analysis"``)
(d) Front-matter ``workflow_type`` set → auto-detect bypass
    (``source="yaml_frontmatter"``)

The activity is invoked as a plain coroutine (``@activity.defn`` does
not change the calling contract for direct invocation). Fake LLM and
Jira commenter collaborators are injected via module-level setters.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_WORKER_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "workers" / "automation-worker"
)
_WORKER_SRC: Final[Path] = _WORKER_ROOT / "src"

for _p in (_WORKER_ROOT, _WORKER_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

# Also add temporal-shared for the TaskAnalysisInput import
_TEMPORAL_SHARED_SRC: Final[Path] = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
if _TEMPORAL_SHARED_SRC.is_dir() and str(_TEMPORAL_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_TEMPORAL_SHARED_SRC))

from automation_worker.activities.task_analyzer import (  # noqa: E402
    TaskAnalysisInput,
    TaskAnalysisResult,
    VALID_WORKFLOW_TYPES,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
    _is_epic_issue,
    _get_epic_subtasks,
    _result_for_epic_multi_step,
    _result_for_epic_needs_subtasks,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Records LLM calls and returns a scripted JSON response."""

    response: str = ""
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        return self.response


@dataclass
class _FakeCommenter:
    """Records Jira comments posted by the activity."""

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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _setup_fakes(tmp_path: Path) -> None:
    """Wire fake LLM + commenter + prompt path for every test."""
    # Create a minimal prompt file so the LLM path doesn't crash
    prompt_file = tmp_path / "task_analysis.md"
    prompt_file.write_text(
        "# Task Analysis Prompt\nAnalyze the task.\n",
        encoding="utf-8",
    )
    set_prompt_path(prompt_file)

    # Default LLM response for non-Epic cases
    llm_response = json.dumps({
        "workflow_type": "code_change_with_test",
        "confidence": 0.9,
        "needs_ssh": False,
        "needs_docker": False,
        "repo": "test-repo",
        "branch": "main",
        "web_search": False,
        "output_actions": [],
        "reasoning": "LLM analysis result",
    })
    fake_llm = _FakeLLM(response=llm_response)
    set_llm_caller(fake_llm)

    fake_commenter = _FakeCommenter()
    set_jira_commenter(fake_commenter)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Issue key strategy — matches Jira key format
_issue_key_strategy = st.from_regex(r"^[A-Z]{2,5}-\d{1,5}$", fullmatch=True)

#: Department ID strategy
_dept_id_strategy = st.from_regex(r"^[a-z][a-z0-9\-]{1,10}$", fullmatch=True)

#: Non-Epic issue type names
_non_epic_types = st.sampled_from(["Story", "Task", "Bug", "Sub-task", "Improvement"])

#: Subtask dict strategy — minimal subtask shape
_subtask_strategy = st.fixed_dictionaries({
    "key": _issue_key_strategy,
    "fields": st.fixed_dictionaries({
        "summary": st.text(min_size=1, max_size=50),
        "status": st.fixed_dictionaries({
            "name": st.sampled_from(["To Do", "In Progress", "Done"]),
        }),
    }),
})


@st.composite
def _epic_issue_meta_with_subtasks(draw: st.DrawFn) -> dict[str, Any]:
    """Generate Epic issue_meta with at least 1 subtask."""
    subtasks = draw(st.lists(_subtask_strategy, min_size=1, max_size=10))
    return {
        "issuetype": {"name": draw(st.sampled_from(["Epic", "epic", "EPIC", " Epic "]))},
        "subtasks": subtasks,
    }


@st.composite
def _epic_issue_meta_no_subtasks(draw: st.DrawFn) -> dict[str, Any]:
    """Generate Epic issue_meta with 0 subtasks."""
    return {
        "issuetype": {"name": draw(st.sampled_from(["Epic", "epic", "EPIC"]))},
        "subtasks": [],
    }


@st.composite
def _non_epic_issue_meta(draw: st.DrawFn) -> dict[str, Any]:
    """Generate non-Epic issue_meta (Story/Task/Bug)."""
    return {
        "issuetype": {"name": draw(_non_epic_types)},
        "subtasks": draw(st.lists(_subtask_strategy, min_size=0, max_size=5)),
    }


@st.composite
def _task_analysis_input(
    draw: st.DrawFn,
    *,
    issue_meta: dict[str, Any] | None = None,
    description: str | None = None,
) -> TaskAnalysisInput:
    """Generate a TaskAnalysisInput with configurable issue_meta."""
    return TaskAnalysisInput(
        issue_key=draw(_issue_key_strategy),
        title=draw(st.text(min_size=1, max_size=100)),
        description=description or draw(st.text(min_size=0, max_size=200)),
        labels=draw(st.lists(st.text(min_size=1, max_size=20), max_size=3)),
        custom_fields={},
        dept_id=draw(_dept_id_strategy),
        dept_config={
            "web_search_enabled": False,
            "available_repos": ["test-repo"],
            "available_spaces": [],
            "available_capabilities": ["jira", "bitbucket"],
            "default_language": "tr",
            "docker_defaults": {
                "cleanup_policy": "on_success",
                "default_timeout_seconds": 3600,
            },
        },
        trace_id="test-trace-id",
        issue_meta=issue_meta or {},
    )


# ---------------------------------------------------------------------------
# Property 13a: Epic + ≥1 subtask → multi_step
# ---------------------------------------------------------------------------


class TestEpicWithSubtasksMultiStep:
    """**Validates: Requirement 12.1**

    When the issue is an Epic with at least one subtask and no YAML
    front-matter specifies workflow_type, the analyzer deterministically
    returns ``workflow_type="multi_step"``, ``confidence=1.0``,
    ``source="epic_auto_detect"``, ``status="ready"``.
    """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        issue_meta=_epic_issue_meta_with_subtasks(),
        issue_key=_issue_key_strategy,
        dept_id=_dept_id_strategy,
    )
    def test_epic_with_subtasks_returns_multi_step(
        self,
        issue_meta: dict[str, Any],
        issue_key: str,
        dept_id: str,
    ) -> None:
        """R12.1: Epic + ≥1 subtask → multi_step deterministically."""
        inp = TaskAnalysisInput(
            issue_key=issue_key,
            title="Epic task",
            description="Some epic description without YAML front-matter",
            labels=[],
            custom_fields={},
            dept_id=dept_id,
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        assert result.workflow_type == "multi_step"
        assert result.confidence == 1.0
        assert result.source == "epic_auto_detect"
        assert result.status == "ready"
        assert result.accepted is True
        assert result.missing_fields == []

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(issue_meta=_epic_issue_meta_with_subtasks())
    def test_epic_multi_step_does_not_call_llm(
        self,
        issue_meta: dict[str, Any],
    ) -> None:
        """R12.1: Epic auto-detect bypasses the LLM entirely."""
        fake_llm = _FakeLLM(response="{}")
        set_llm_caller(fake_llm)

        inp = TaskAnalysisInput(
            issue_key="EPIC-1",
            title="Epic task",
            description="No YAML here",
            labels=[],
            custom_fields={},
            dept_id="test-dept",
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        asyncio.run(analyze_task(inp))

        # LLM should NOT have been called
        assert len(fake_llm.calls) == 0

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(issue_meta=_epic_issue_meta_with_subtasks())
    def test_epic_multi_step_output_actions_carry_subtask_count(
        self,
        issue_meta: dict[str, Any],
    ) -> None:
        """R12.6: The result carries subtask_count in output_actions
        metadata for audit purposes."""
        inp = TaskAnalysisInput(
            issue_key="EPIC-2",
            title="Epic task",
            description="No YAML",
            labels=[],
            custom_fields={},
            dept_id="test-dept",
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        # Find the metadata entry in output_actions
        meta_entries = [
            oa for oa in result.output_actions
            if isinstance(oa, dict) and oa.get("_meta") == "epic_auto_detect"
        ]
        assert len(meta_entries) == 1
        assert meta_entries[0]["subtask_count"] == len(issue_meta["subtasks"])


# ---------------------------------------------------------------------------
# Property 13b: Epic + 0 subtask → needs_info
# ---------------------------------------------------------------------------


class TestEpicNoSubtasksNeedsInfo:
    """**Validates: Requirement 12.2**

    When the issue is an Epic with 0 subtasks and no YAML front-matter
    specifies workflow_type, the analyzer returns
    ``status="needs_info"``, ``missing_fields=["subtasks"]``,
    ``source="epic_auto_detect"``.
    """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        issue_meta=_epic_issue_meta_no_subtasks(),
        issue_key=_issue_key_strategy,
        dept_id=_dept_id_strategy,
    )
    def test_epic_no_subtasks_returns_needs_info(
        self,
        issue_meta: dict[str, Any],
        issue_key: str,
        dept_id: str,
    ) -> None:
        """R12.2: Epic + 0 subtask → needs_info."""
        inp = TaskAnalysisInput(
            issue_key=issue_key,
            title="Epic without subtasks",
            description="Some description",
            labels=[],
            custom_fields={},
            dept_id=dept_id,
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        assert result.status == "needs_info"
        assert result.source == "epic_auto_detect"
        assert "subtasks" in result.missing_fields
        assert result.accepted is False

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(issue_meta=_epic_issue_meta_no_subtasks())
    def test_epic_no_subtasks_posts_comment(
        self,
        issue_meta: dict[str, Any],
    ) -> None:
        """R12.2: Epic with no subtasks posts a needs_info comment."""
        fake_commenter = _FakeCommenter()
        set_jira_commenter(fake_commenter)

        inp = TaskAnalysisInput(
            issue_key="EPIC-3",
            title="Epic without subtasks",
            description="Some description",
            labels=[],
            custom_fields={},
            dept_id="test-dept",
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        asyncio.run(analyze_task(inp))

        # A comment should have been posted
        assert len(fake_commenter.comments) >= 1
        # The comment should mention subtasks
        _, body, _ = fake_commenter.comments[0]
        assert "subtask" in body.lower() or "Epic" in body

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(issue_meta=_epic_issue_meta_no_subtasks())
    def test_epic_no_subtasks_does_not_call_llm(
        self,
        issue_meta: dict[str, Any],
    ) -> None:
        """R12.2: Epic auto-detect (no subtasks) bypasses the LLM."""
        fake_llm = _FakeLLM(response="{}")
        set_llm_caller(fake_llm)

        inp = TaskAnalysisInput(
            issue_key="EPIC-4",
            title="Epic without subtasks",
            description="No YAML",
            labels=[],
            custom_fields={},
            dept_id="test-dept",
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        asyncio.run(analyze_task(inp))

        assert len(fake_llm.calls) == 0


# ---------------------------------------------------------------------------
# Property 13c: Story/Task/Bug → LLM path
# ---------------------------------------------------------------------------


class TestNonEpicUsesLLMPath:
    """**Validates: Requirement 12.7**

    Non-Epic issue types (Story, Task, Bug, Sub-task, Improvement)
    follow the existing LLM analysis path — ``source="llm_analysis"``.
    """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        issue_meta=_non_epic_issue_meta(),
        issue_key=_issue_key_strategy,
        dept_id=_dept_id_strategy,
    )
    def test_non_epic_uses_llm_path(
        self,
        issue_meta: dict[str, Any],
        issue_key: str,
        dept_id: str,
    ) -> None:
        """R12.7: Non-Epic types use the LLM analysis path."""
        # Set up LLM to return a valid response
        llm_response = json.dumps({
            "workflow_type": "code_change_with_test",
            "confidence": 0.9,
            "needs_ssh": False,
            "needs_docker": False,
            "repo": "test-repo",
            "branch": "main",
            "web_search": False,
            "output_actions": [],
            "reasoning": "LLM decided this",
        })
        fake_llm = _FakeLLM(response=llm_response)
        set_llm_caller(fake_llm)

        inp = TaskAnalysisInput(
            issue_key=issue_key,
            title="Regular task",
            description="No YAML front-matter here",
            labels=[],
            custom_fields={},
            dept_id=dept_id,
            dept_config={
                "web_search_enabled": False,
                "available_repos": ["test-repo"],
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        # Should use LLM path, not epic_auto_detect
        assert result.source == "llm_analysis"
        # LLM should have been called
        assert len(fake_llm.calls) >= 1

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(issue_meta=_non_epic_issue_meta())
    def test_non_epic_never_returns_epic_auto_detect_source(
        self,
        issue_meta: dict[str, Any],
    ) -> None:
        """R12.7: Non-Epic types never produce source='epic_auto_detect'."""
        llm_response = json.dumps({
            "workflow_type": "research_basic",
            "confidence": 0.85,
            "needs_ssh": False,
            "needs_docker": False,
            "web_search": False,
            "output_actions": [],
            "reasoning": "Research task",
        })
        fake_llm = _FakeLLM(response=llm_response)
        set_llm_caller(fake_llm)

        inp = TaskAnalysisInput(
            issue_key="TASK-1",
            title="Regular task",
            description="No YAML",
            labels=[],
            custom_fields={},
            dept_id="test-dept",
            dept_config={
                "web_search_enabled": False,
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        assert result.source != "epic_auto_detect"


# ---------------------------------------------------------------------------
# Property 13d: Front-matter workflow_type set → auto-detect bypass
# ---------------------------------------------------------------------------


class TestFrontMatterBypassesAutoDetect:
    """**Validates: Requirement 12.5**

    When the YAML front-matter in the description sets a
    ``workflow_type``, the Epic auto-detect is bypassed — even if the
    issue is an Epic with subtasks. The front-matter > Epic auto-detect
    > LLM priority is maintained.
    """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        issue_meta=_epic_issue_meta_with_subtasks(),
        issue_key=_issue_key_strategy,
        dept_id=_dept_id_strategy,
        workflow_type=st.sampled_from([
            "code_change_with_test",
            "research_basic",
            "script_execute",
            "noop_test",
        ]),
    )
    def test_frontmatter_overrides_epic_auto_detect(
        self,
        issue_meta: dict[str, Any],
        issue_key: str,
        dept_id: str,
        workflow_type: str,
    ) -> None:
        """R12.5: Front-matter workflow_type overrides Epic auto-detect."""
        # Description with YAML front-matter
        description = (
            f"---\n"
            f"ai-bot:\n"
            f"  workflow_type: {workflow_type}\n"
            f"  repo: test-repo\n"
            f"  branch: main\n"
            f"---\n"
            f"Some epic description with subtasks"
        )

        inp = TaskAnalysisInput(
            issue_key=issue_key,
            title="Epic with front-matter override",
            description=description,
            labels=[],
            custom_fields={},
            dept_id=dept_id,
            dept_config={
                "web_search_enabled": False,
                "available_repos": ["test-repo"],
                "docker_defaults": {"cleanup_policy": "on_success"},
            },
            trace_id="test",
            issue_meta=issue_meta,
        )

        result: TaskAnalysisResult = asyncio.run(analyze_task(inp))

        # Front-matter should take priority over Epic auto-detect
        assert result.source == "yaml_frontmatter"
        assert result.workflow_type == workflow_type
        # Should NOT be epic_auto_detect
        assert result.source != "epic_auto_detect"


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Unit tests for the Epic auto-detect helper functions."""

    def test_is_epic_issue_true_for_epic(self) -> None:
        """_is_epic_issue returns True for Epic issuetype."""
        inp = TaskAnalysisInput(
            issue_key="EPIC-1",
            title="Test",
            description="",
            labels=[],
            custom_fields={},
            dept_id="test",
            dept_config={},
            issue_meta={"issuetype": {"name": "Epic"}},
        )
        assert _is_epic_issue(inp) is True

    def test_is_epic_issue_case_insensitive(self) -> None:
        """_is_epic_issue is case-insensitive."""
        for name in ("Epic", "epic", "EPIC", " Epic "):
            inp = TaskAnalysisInput(
                issue_key="EPIC-1",
                title="Test",
                description="",
                labels=[],
                custom_fields={},
                dept_id="test",
                dept_config={},
                issue_meta={"issuetype": {"name": name}},
            )
            assert _is_epic_issue(inp) is True

    def test_is_epic_issue_false_for_non_epic(self) -> None:
        """_is_epic_issue returns False for non-Epic types."""
        for name in ("Story", "Task", "Bug", "Sub-task"):
            inp = TaskAnalysisInput(
                issue_key="TASK-1",
                title="Test",
                description="",
                labels=[],
                custom_fields={},
                dept_id="test",
                dept_config={},
                issue_meta={"issuetype": {"name": name}},
            )
            assert _is_epic_issue(inp) is False

    def test_is_epic_issue_false_for_missing_meta(self) -> None:
        """_is_epic_issue returns False when issue_meta is empty."""
        inp = TaskAnalysisInput(
            issue_key="TASK-1",
            title="Test",
            description="",
            labels=[],
            custom_fields={},
            dept_id="test",
            dept_config={},
            issue_meta={},
        )
        assert _is_epic_issue(inp) is False

    def test_is_epic_issue_false_for_malformed_meta(self) -> None:
        """_is_epic_issue returns False for malformed issue_meta."""
        for meta in (
            {"issuetype": "Epic"},  # not a dict
            {"issuetype": {"name": 123}},  # name not a string
            {"issuetype": {}},  # missing name
        ):
            inp = TaskAnalysisInput(
                issue_key="TASK-1",
                title="Test",
                description="",
                labels=[],
                custom_fields={},
                dept_id="test",
                dept_config={},
                issue_meta=meta,
            )
            assert _is_epic_issue(inp) is False

    def test_get_epic_subtasks_returns_list(self) -> None:
        """_get_epic_subtasks returns subtask dicts."""
        subtasks = [{"key": "SUB-1", "fields": {"summary": "s1"}}]
        inp = TaskAnalysisInput(
            issue_key="EPIC-1",
            title="Test",
            description="",
            labels=[],
            custom_fields={},
            dept_id="test",
            dept_config={},
            issue_meta={"issuetype": {"name": "Epic"}, "subtasks": subtasks},
        )
        assert _get_epic_subtasks(inp) == subtasks

    def test_get_epic_subtasks_empty_when_missing(self) -> None:
        """_get_epic_subtasks returns [] when subtasks field is missing."""
        inp = TaskAnalysisInput(
            issue_key="EPIC-1",
            title="Test",
            description="",
            labels=[],
            custom_fields={},
            dept_id="test",
            dept_config={},
            issue_meta={"issuetype": {"name": "Epic"}},
        )
        assert _get_epic_subtasks(inp) == []

    def test_get_epic_subtasks_filters_non_dicts(self) -> None:
        """_get_epic_subtasks filters out non-dict entries."""
        inp = TaskAnalysisInput(
            issue_key="EPIC-1",
            title="Test",
            description="",
            labels=[],
            custom_fields={},
            dept_id="test",
            dept_config={},
            issue_meta={
                "issuetype": {"name": "Epic"},
                "subtasks": [{"key": "SUB-1"}, "invalid", 123, None],
            },
        )
        result = _get_epic_subtasks(inp)
        assert result == [{"key": "SUB-1"}]
