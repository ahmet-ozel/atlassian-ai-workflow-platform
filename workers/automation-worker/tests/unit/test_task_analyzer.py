"""Unit tests for the ``task_analyzer`` activity.

Strategy
--------

The activity has two collaborators:

* an LLM caller (asynchronous ``complete(prompt, dept_id)`` returning
  raw response text);
* a Jira commenter (asynchronous ``add_comment(issue_key, body, dept_id)``).

Both are replaced with in-memory fakes registered through the module-
level setters declared on ``task_analyzer``.  The activity is exercised
as a plain coroutine - ``@activity.defn`` does not change the calling
contract for direct invocation.

The prompt file is created in a tmp directory and ``set_prompt_path``
is used so the tests don't depend on the real ``platform/prompts/``
location.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]

for _candidate in (_SRC_DIR,):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities import task_analyzer  # noqa: E402
from automation_worker.activities.task_analyzer import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    VALID_WORKFLOW_TYPES,
    WEB_SEARCH_WORKFLOW_TYPES,
    PromptCache,
    TaskAnalysisInput,
    TaskAnalysisResult,
    analyze_task,
    set_jira_commenter,
    set_llm_caller,
    set_prompt_path,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLM:
    """Records LLM ``complete`` calls and returns scripted responses."""

    response: str = ""
    error: Exception | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete(self, prompt: str, *, dept_id: str) -> str:
        self.calls.append((prompt, dept_id))
        if self.error:
            raise self.error
        return self.response


@dataclass
class _FakeCommenter:
    """Records Jira comments posted by the activity."""

    error: Exception | None = None
    comments: list[tuple[str, str, str]] = field(default_factory=list)

    async def add_comment(
        self,
        issue_key: str,
        body: str,
        *,
        dept_id: str,
    ) -> None:
        self.comments.append((issue_key, body, dept_id))
        if self.error:
            raise self.error


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_DEFAULT_DEPT_CONFIG: dict[str, Any] = {
    "available_repos": ["org/backend", "org/frontend"],
    "available_spaces": ["TEAM"],
    "available_capabilities": ["jira_read", "jira_write"],
    "default_language": "tr",
    "web_search_enabled": True,
    "docker_defaults": {
        "cleanup_policy": "on_success",
        "default_timeout_seconds": 1800,
    },
    "approvers": ["alice"],
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
    """Create a tmp prompt file and point the analyzer at it."""
    p = tmp_path / "task_analysis.md"
    p.write_text(
        "# Test prompt\n\nReturn JSON with workflow_type field.\n",
        encoding="utf-8",
    )
    set_prompt_path(p)
    yield p
    # Reset the prompt path / cache after each test.
    set_prompt_path(task_analyzer.DEFAULT_PROMPT_PATH)


@pytest.fixture(autouse=True)
def _wire_fakes(fake_llm: _FakeLLM, fake_commenter: _FakeCommenter) -> None:
    """Ensure both fakes are wired before every test."""


def _make_input(
    *,
    description: str = "Add a retry mechanism with exponential backoff.",
    title: str = "Add retry mechanism",
    issue_key: str = "PAY-42",
    dept_id: str = "payments",
    dept_config: dict[str, Any] | None = None,
    custom_fields: dict[str, str | None] | None = None,
    labels: list[str] | None = None,
) -> TaskAnalysisInput:
    return TaskAnalysisInput(
        issue_key=issue_key,
        title=title,
        description=description,
        labels=labels or [],
        custom_fields=custom_fields or {},
        dept_id=dept_id,
        dept_config=dept_config if dept_config is not None else dict(_DEFAULT_DEPT_CONFIG),
        trace_id="trace-test-001",
    )


def _llm_payload(
    *,
    workflow_type: str = "code_change_with_test",
    confidence: float = 0.9,
    needs_ssh: bool = True,
    needs_docker: bool = False,
    repo: str | None = "org/backend",
    branch: str | None = "develop",
    cleanup_policy: str = "on_success",
    timeout_seconds: int | None = None,
    web_search: bool = False,
    output_actions: list[dict[str, Any]] | None = None,
    test_command: str | None = "pytest -q",
    missing_fields: list[str] | None = None,
    reasoning: str = "Looks like a typical code change.",
    fence: bool = False,
) -> str:
    payload = {
        "workflow_type": workflow_type,
        "needs_ssh": needs_ssh,
        "needs_docker": needs_docker,
        "repo": repo,
        "branch": branch,
        "cleanup_policy": cleanup_policy,
        "timeout_seconds": timeout_seconds,
        "test_command": test_command,
        "web_search": web_search,
        "output_actions": output_actions
        or [
            {"type": "jira_comment", "payload": {"body": "✅ Done."}},
        ],
        "confidence": confidence,
        "missing_fields": missing_fields or [],
        "reasoning": reasoning,
    }
    body = json.dumps(payload)
    if fence:
        return f"```json\n{body}\n```"
    return body


# ---------------------------------------------------------------------------
# Tests: LLM happy path
# ---------------------------------------------------------------------------


class TestLLMAnalysis:
    def test_llm_path_produces_ready_result(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """LLM analysis happy path."""
        fake_llm.response = _llm_payload(
            workflow_type="code_change_with_test",
            confidence=0.9,
        )
        result = asyncio.run(analyze_task(_make_input()))

        assert result.accepted is True
        assert result.status == "ready"
        assert result.workflow_type == "code_change_with_test"
        assert result.confidence == 0.9
        assert result.source == "llm_analysis"
        assert result.repo == "org/backend"
        assert result.branch == "develop"
        # Prompt was rendered with the issue context
        assert len(fake_llm.calls) == 1
        rendered_prompt, dept_id = fake_llm.calls[0]
        assert dept_id == "payments"
        assert "PAY-42" in rendered_prompt
        assert "Add retry mechanism" in rendered_prompt
        # No comments posted on the happy path
        assert fake_commenter.comments == []

    def test_llm_response_with_markdown_fence_is_parsed(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """LLM responses wrapped in ```json fences are unwrapped."""
        fake_llm.response = _llm_payload(fence=True)
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "ready"
        assert result.workflow_type == "code_change_with_test"

    def test_llm_invalid_json_marked_needs_info(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """Invalid LLM JSON → confidence 0 → reject (workflow_type missing)."""
        fake_llm.response = "this is not JSON at all"
        result = asyncio.run(analyze_task(_make_input()))

        # With workflow_type=None the validator rejects.
        assert result.status == "rejected"
        assert result.accepted is False
        # Reject comment posted to Jira.
        assert len(fake_commenter.comments) == 1
        issue_key, body, _ = fake_commenter.comments[0]
        assert issue_key == "PAY-42"
        assert "geçersiz workflow_type" in body or "<missing>" in body

    def test_llm_call_failure_does_not_raise(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """LLM exceptions are converted into a rejection (workflow_type=None)."""
        fake_llm.error = RuntimeError("provider down")
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "rejected"
        assert result.workflow_type is None
        # The validation rejection wins on ``error`` but the original LLM
        # failure is preserved in ``reasoning`` for diagnosis.
        assert "provider down" in result.reasoning
        assert len(fake_commenter.comments) == 1


# ---------------------------------------------------------------------------
# Tests: Confidence threshold
# ---------------------------------------------------------------------------


class TestConfidenceThreshold:
    def test_low_confidence_routes_to_needs_info(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """Confidence < 0.7 → needs_info."""
        fake_llm.response = _llm_payload(
            confidence=0.5,
            missing_fields=["repo", "branch"],
        )
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "needs_info"
        assert result.accepted is False
        assert result.confidence == 0.5
        assert result.missing_fields == ["repo", "branch"]
        # Comment posted listing missing fields.
        assert len(fake_commenter.comments) == 1
        body = fake_commenter.comments[0][1]
        assert "repo" in body
        assert "branch" in body

    def test_threshold_boundary_07_proceeds(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Confidence == 0.7 proceeds at the boundary."""
        fake_llm.response = _llm_payload(confidence=0.7)
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "ready"

    def test_threshold_just_below_07_needs_info(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """confidence 0.69 → needs_info."""
        fake_llm.response = _llm_payload(confidence=0.69)
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "needs_info"


# ---------------------------------------------------------------------------
# Tests: Workflow type validation
# ---------------------------------------------------------------------------


class TestWorkflowTypeValidation:
    def test_invalid_workflow_type_rejected(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """Unknown workflow_type → reject + comment."""
        fake_llm.response = _llm_payload(
            workflow_type="not_a_real_type",
            confidence=0.95,
        )
        result = asyncio.run(analyze_task(_make_input()))

        assert result.status == "rejected"
        assert result.accepted is False
        assert "Invalid workflow_type" in (result.error or "")
        assert len(fake_commenter.comments) == 1
        body = fake_commenter.comments[0][1]
        assert "not_a_real_type" in body
        assert "code_change_with_test" in body  # allowed list shown

    def test_all_valid_workflow_types_accepted(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Every entry in VALID_WORKFLOW_TYPES is actually accepted."""
        # Use a dept config with web_search disabled to bypass the
        # downgrade for research_with_web/research_publish_confluence
        # - those types remain valid (they will be downgraded but
        # status still ready).
        for wf in sorted(VALID_WORKFLOW_TYPES):
            fake_llm.response = _llm_payload(workflow_type=wf, confidence=0.9)
            inp = _make_input()
            result = asyncio.run(analyze_task(inp))
            assert result.status == "ready", f"workflow_type={wf} not accepted"


# ---------------------------------------------------------------------------
# Tests: Web search downgrade
# ---------------------------------------------------------------------------


class TestWebSearchDowngrade:
    def test_research_with_web_downgraded_when_dept_disables(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """research_with_web → research_basic."""
        fake_llm.response = _llm_payload(
            workflow_type="research_with_web",
            confidence=0.9,
        )
        dept = dict(_DEFAULT_DEPT_CONFIG)
        dept["web_search_enabled"] = False
        result = asyncio.run(analyze_task(_make_input(dept_config=dept)))

        assert result.status == "ready"
        assert result.workflow_type == "research_basic"
        assert result.downgraded is True
        assert result.web_search is False
        # Informational comment posted.
        assert len(fake_commenter.comments) == 1
        body = fake_commenter.comments[0][1]
        assert "research_with_web" in body
        assert "research_basic" in body

    def test_research_publish_confluence_downgraded(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """research_publish_confluence is downgraded too."""
        fake_llm.response = _llm_payload(
            workflow_type="research_publish_confluence",
            confidence=0.9,
        )
        dept = dict(_DEFAULT_DEPT_CONFIG)
        dept["web_search_enabled"] = False
        result = asyncio.run(analyze_task(_make_input(dept_config=dept)))

        assert result.workflow_type == "research_basic"
        assert result.downgraded is True

    def test_no_downgrade_when_dept_enables_web_search(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """When web_search_enabled=True, the workflow_type is preserved."""
        fake_llm.response = _llm_payload(
            workflow_type="research_with_web",
            confidence=0.9,
        )
        # Default dept config has web_search_enabled=True.
        result = asyncio.run(analyze_task(_make_input()))

        assert result.workflow_type == "research_with_web"
        assert result.downgraded is False
        assert fake_commenter.comments == []

    def test_non_web_search_types_never_downgraded(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Only WEB_SEARCH_WORKFLOW_TYPES are subject to the downgrade rule."""
        for wf in sorted(VALID_WORKFLOW_TYPES - WEB_SEARCH_WORKFLOW_TYPES):
            fake_llm.response = _llm_payload(workflow_type=wf, confidence=0.9)
            dept = dict(_DEFAULT_DEPT_CONFIG)
            dept["web_search_enabled"] = False
            result = asyncio.run(analyze_task(_make_input(dept_config=dept)))
            assert result.downgraded is False, f"{wf} should not downgrade"
            assert result.workflow_type == wf


# ---------------------------------------------------------------------------
# Tests: Hot-reload prompt cache
# ---------------------------------------------------------------------------


class TestPromptHotReload:
    def test_prompt_loaded_once_when_unchanged(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Same mtime → cached body is reused."""
        # Use a fresh PromptCache to count loads precisely.
        cache = PromptCache()
        body1 = cache.load(prompt_path)
        body2 = cache.load(prompt_path)
        assert body1 == body2
        # The cached_mtime accessor reflects the file's mtime.
        assert cache.cached_mtime == prompt_path.stat().st_mtime

    def test_prompt_reloaded_when_mtime_changes(
        self,
        prompt_path: Path,
    ) -> None:
        """File change → cache invalidated."""
        cache = PromptCache()
        body_v1 = cache.load(prompt_path)

        # Bump mtime explicitly so the test does not depend on filesystem
        # resolution (Windows often only stores mtime to 1s precision).
        new_text = body_v1 + "\n# v2 marker\n"
        prompt_path.write_text(new_text, encoding="utf-8")
        new_mtime = cache.cached_mtime + 5.0  # type: ignore[operator]
        import os as _os

        _os.utime(prompt_path, (new_mtime, new_mtime))

        body_v2 = cache.load(prompt_path)
        assert body_v2 != body_v1
        assert "v2 marker" in body_v2

    def test_missing_prompt_file_raises_filenotfounderror(
        self,
        tmp_path: Path,
    ) -> None:
        """Missing prompt file → FileNotFoundError surfaces from PromptCache."""
        cache = PromptCache()
        missing = tmp_path / "does-not-exist.md"
        with pytest.raises(FileNotFoundError):
            cache.load(missing)

    def test_analyze_task_handles_missing_prompt_gracefully(
        self,
        tmp_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """When the prompt is missing, the activity rejects the task."""
        missing = tmp_path / "missing-prompt.md"
        set_prompt_path(missing)
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            set_prompt_path(task_analyzer.DEFAULT_PROMPT_PATH)

        assert result.status == "rejected"
        # Reject comment is posted because workflow_type is None.
        assert len(fake_commenter.comments) >= 1


# ---------------------------------------------------------------------------
# Tests: YAML front-matter priority - uses real parser
# ---------------------------------------------------------------------------


class TestYamlFrontMatter:
    """Exercises the YAML branch using a stub description_parser injected
    via ``sys.modules``.  We *do not* depend on the real parser because
    the stub lets us prove the analyzer would skip the LLM when the parser
    yields a workflow_type.
    """

    def _install_stub_parser(self, parsed_obj: object) -> None:
        """Install an in-memory description_parser module."""
        import sys as _sys
        import types as _types

        mod = _types.ModuleType("automation_worker.activities.description_parser")
        mod.parse_description_frontmatter = lambda _desc: parsed_obj  # type: ignore[attr-defined]
        _sys.modules["automation_worker.activities.description_parser"] = mod

    def _uninstall_stub_parser(self) -> None:
        import sys as _sys

        _sys.modules.pop(
            "automation_worker.activities.description_parser", None
        )

    def test_yaml_path_skips_llm(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Valid YAML block → LLM is not called."""

        @dataclass
        class _ParsedFM:
            workflow_type: str | None = "code_change_with_test"
            repo: str | None = "org/backend"
            branch: str | None = "develop"
            needs_ssh: bool | None = True
            needs_docker: bool | None = False
            execution_command: str | None = "pytest -q"
            cleanup: str | None = "always"
            timeout_seconds: int | None = 600
            web_search: bool | None = False
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_ParsedFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.status == "ready"
        assert result.source == "yaml_frontmatter"
        assert result.workflow_type == "code_change_with_test"
        assert result.confidence == 1.0
        assert result.cleanup_policy == "always"
        assert result.timeout_seconds == 600
        assert "Task details:" in result.reasoning
        assert "Add a retry mechanism with exponential backoff." in result.reasoning
        # LLM never called.
        assert fake_llm.calls == []

    def test_no_yaml_block_falls_through_to_llm(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """No YAML / parser returns None → LLM fires."""
        # Stub returns None → no front-matter detected.
        self._install_stub_parser(None)
        try:
            fake_llm.response = _llm_payload(confidence=0.9)
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.source == "llm_analysis"
        assert len(fake_llm.calls) == 1

    def test_yaml_workflow_type_none_falls_through_to_llm(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """Parsed YAML without workflow_type → still uses LLM."""

        @dataclass
        class _PartialFM:
            workflow_type: str | None = None
            repo: str | None = "org/backend"
            branch: str | None = None
            needs_ssh: bool | None = None
            needs_docker: bool | None = None
            execution_command: str | None = "pytest -q"
            cleanup: str | None = None
            timeout_seconds: int | None = None
            web_search: bool | None = None
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_PartialFM())
        try:
            fake_llm.response = _llm_payload(confidence=0.9)
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.source == "llm_analysis"
        assert len(fake_llm.calls) == 1

    def test_yaml_with_invalid_workflow_type_rejected(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """YAML with garbage workflow_type → rejection (validation runs after)."""

        @dataclass
        class _BadFM:
            workflow_type: str | None = "💀_invalid"
            repo: str | None = None
            branch: str | None = None
            needs_ssh: bool | None = None
            needs_docker: bool | None = None
            execution_command: str | None = "pytest -q"
            cleanup: str | None = None
            timeout_seconds: int | None = None
            web_search: bool | None = None
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_BadFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.status == "rejected"
        # LLM is bypassed completely.
        assert fake_llm.calls == []
        # Reject comment.
        assert len(fake_commenter.comments) == 1


# ---------------------------------------------------------------------------
# Tests: Field defaults / dept_config integration
# ---------------------------------------------------------------------------


class TestFieldDefaults:
    def test_timeout_defaulted_from_dept_config_when_llm_omits(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """LLM omits timeout_seconds → dept default fills in."""
        fake_llm.response = _llm_payload(timeout_seconds=None)
        result = asyncio.run(analyze_task(_make_input()))

        assert result.timeout_seconds == 1800

    def test_cleanup_policy_defaulted_when_llm_omits(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """LLM omits cleanup_policy → dept default fills in."""
        # Empty string for cleanup_policy still uses the default.
        payload = json.loads(_llm_payload())
        payload.pop("cleanup_policy", None)
        fake_llm.response = json.dumps(payload)
        result = asyncio.run(analyze_task(_make_input()))

        assert result.cleanup_policy == "on_success"

    def test_confidence_clamped_to_unit_interval(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """LLM may emit garbage confidence values; we clamp to [0, 1]."""
        fake_llm.response = _llm_payload(confidence=1.7)  # type: ignore[arg-type]
        result = asyncio.run(analyze_task(_make_input()))

        assert 0.0 <= result.confidence <= 1.0


# ---------------------------------------------------------------------------
# Tests: Description override integration
# ---------------------------------------------------------------------------


class TestDescriptionOverrideIntegration:
    """Per-task YAML overrides applied on top of dept_config.

    The contract is that the YAML front-matter values win over
    department defaults for ``cleanup``, ``timeout_seconds``,
    ``web_search``, ``repo``, ``branch`` and ``output``; missing /
    invalid YAML fields fall back to the department defaults; and
    invalid values trigger an advisory Jira comment that lists the
    parse errors so the reporter can fix the override block.
    """

    def _install_stub_parser(self, parsed_obj: object) -> None:
        import sys as _sys
        import types as _types

        mod = _types.ModuleType(
            "automation_worker.activities.description_parser"
        )
        mod.parse_description_frontmatter = lambda _desc: parsed_obj  # type: ignore[attr-defined]
        _sys.modules[
            "automation_worker.activities.description_parser"
        ] = mod

    def _uninstall_stub_parser(self) -> None:
        import sys as _sys

        _sys.modules.pop(
            "automation_worker.activities.description_parser", None
        )

    def test_yaml_timeout_omitted_falls_back_to_dept_default(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """YAML omits timeout_seconds → dept default fills in.

        The YAML branch used to leave ``timeout_seconds`` as ``None``, which
        is inconsistent with the LLM branch and would
        force the workflow to invent its own fallback. The override
        contract requires falling back to ``docker_defaults.default_timeout_seconds``.
        """

        @dataclass
        class _ParsedFM:
            workflow_type: str | None = "code_change_with_test"
            repo: str | None = None
            branch: str | None = None
            needs_ssh: bool | None = None
            needs_docker: bool | None = None
            execution_command: str | None = "pytest -q"
            cleanup: str | None = None
            timeout_seconds: int | None = None  # YAML omitted
            web_search: bool | None = None
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_ParsedFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        # Dept default for ``docker_defaults.default_timeout_seconds`` is 1800.
        assert result.timeout_seconds == 1800
        # And ``cleanup_policy`` still falls back to the dept default.
        assert result.cleanup_policy == "on_success"

    def test_yaml_overrides_win_over_dept_defaults(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
    ) -> None:
        """YAML values win over dept_config when valid."""

        @dataclass
        class _ParsedFM:
            workflow_type: str | None = "code_change_with_test"
            repo: str | None = "org/backend"
            branch: str | None = "feature/x"
            needs_ssh: bool | None = True
            needs_docker: bool | None = False
            execution_command: str | None = "pytest -q"
            cleanup: str | None = "always"  # overrides on_success
            timeout_seconds: int | None = 600  # overrides 1800
            web_search: bool | None = False
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_ParsedFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.cleanup_policy == "always"
        assert result.timeout_seconds == 600
        assert result.repo == "org/backend"
        assert result.branch == "feature/x"
        # LLM is bypassed when YAML carries a workflow_type.
        assert fake_llm.calls == []

    def test_parse_errors_trigger_warning_comment(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """Invalid YAML field → warning comment listing the errors.

        The parser drops the offending fields to ``None`` and records
        per-field error strings in ``parse_errors``. ``analyze_task``
        forwards them to Jira as a single advisory comment so the
        reporter can fix the override block.
        """
        errors = [
            "cleanup: 'maybe' is not valid (allowed: ['always', 'never', 'on_success'])",
            "timeout_seconds: 5 out of range [60, 7200]",
        ]

        @dataclass
        class _ParsedFM:
            workflow_type: str | None = "code_change_with_test"
            repo: str | None = "org/backend"
            branch: str | None = None
            needs_ssh: bool | None = None
            needs_docker: bool | None = None
            execution_command: str | None = "pytest -q"
            cleanup: str | None = None  # invalid → dropped to None
            timeout_seconds: int | None = None  # invalid → dropped to None
            web_search: bool | None = None
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=lambda: list(errors))

        self._install_stub_parser(_ParsedFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        # The task still proceeds: invalid fields fall back to dept defaults.
        assert result.status == "ready"
        assert result.cleanup_policy == "on_success"
        assert result.timeout_seconds == 1800

        # Exactly one warning comment was posted, and it names every
        # parse error so the reporter can spot the typos.
        assert len(fake_commenter.comments) == 1
        _issue, body, _dept = fake_commenter.comments[0]
        for err in errors:
            assert err in body, f"warning comment missing error {err!r}: {body}"

    def test_no_warning_comment_when_yaml_is_valid(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """A clean YAML block must not trigger a warning comment."""

        @dataclass
        class _ParsedFM:
            workflow_type: str | None = "code_change_with_test"
            repo: str | None = "org/backend"
            branch: str | None = "develop"
            needs_ssh: bool | None = True
            needs_docker: bool | None = False
            execution_command: str | None = "pytest -q"
            cleanup: str | None = "always"
            timeout_seconds: int | None = 600
            web_search: bool | None = False
            output: list[dict] | None = None
            parse_errors: list[str] = field(default_factory=list)

        self._install_stub_parser(_ParsedFM())
        try:
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        assert result.status == "ready"
        assert fake_commenter.comments == []

    def test_parse_errors_warning_posted_even_when_falling_through_to_llm(
        self,
        prompt_path: Path,
        fake_llm: _FakeLLM,
        fake_commenter: _FakeCommenter,
    ) -> None:
        """parse_errors warning is posted whether or not the
        YAML branch is taken.

        When the YAML block carries invalid values *and* omits
        ``workflow_type``, the analyzer falls through to the LLM but
        the user still made a mistake in the override block; the
        warning comment must surface either way.
        """

        @dataclass
        class _PartialFM:
            workflow_type: str | None = None  # missing → LLM branch wins
            repo: str | None = None
            branch: str | None = None
            needs_ssh: bool | None = None
            needs_docker: bool | None = None
            execution_command: str | None = "pytest -q"
            cleanup: str | None = None
            timeout_seconds: int | None = None
            web_search: bool | None = None
            output: list[dict] | None = None
            parse_errors: list[str] = field(
                default_factory=lambda: [
                    "cleanup: 'maybe' is not valid",
                ]
            )

        self._install_stub_parser(_PartialFM())
        try:
            fake_llm.response = _llm_payload(confidence=0.9)
            result = asyncio.run(analyze_task(_make_input()))
        finally:
            self._uninstall_stub_parser()

        # LLM branch produced the analysis...
        assert result.source == "llm_analysis"
        assert result.status == "ready"
        # ...but the warning comment was still posted.
        assert any(
            "cleanup" in body for _issue, body, _dept in fake_commenter.comments
        ), (
            "expected an advisory comment listing the YAML parse error "
            f"but got {fake_commenter.comments!r}"
        )
