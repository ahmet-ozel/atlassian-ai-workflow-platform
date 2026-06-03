"""Unit tests for the ``repo_resolver`` activity.

Strategy
--------

The activity depends on three collaborators:
- LLM parser (for description-based repo parsing)
- Jira commenter (for posting comments)
- Jira transitioner (for status transitions)

We replace them with in-memory fakes registered through the module-level
setters. The activity runs as a plain coroutine.
"""

from __future__ import annotations

import asyncio
import sys
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
_DB_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "db-shared" / "src"

for _candidate in (_SRC_DIR, _DB_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities.repo_resolver import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    RepoResolveInput,
    RepoResolveResult,
    resolve_repo_field,
    set_jira_commenter,
    set_jira_transitioner,
    set_llm_parser,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeLLMParser:
    """Records LLM parse calls and returns scripted responses."""

    calls: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    response: dict[str, Any] = field(
        default_factory=lambda: {"repo_url": None, "confidence": 0.0}
    )
    error: Exception | None = None

    async def parse_repo_from_description(
        self,
        description: str,
        repo_mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.calls.append((description, repo_mappings))
        if self.error:
            raise self.error
        return self.response


@dataclass
class _FakeJiraCommenter:
    """Records Jira comment calls."""

    comments: list[tuple[str, str, str]] = field(default_factory=list)
    error: Exception | None = None

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


@dataclass
class _FakeJiraTransitioner:
    """Records Jira transition calls."""

    transitions: list[tuple[str, str, str]] = field(default_factory=list)
    error: Exception | None = None

    async def transition_issue(
        self,
        issue_key: str,
        target_status: str,
        *,
        dept_id: str,
    ) -> None:
        self.transitions.append((issue_key, target_status, dept_id))
        if self.error:
            raise self.error


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_REPO_MAPPINGS = [
    {"bitbucket_repo": "org/backend-api", "name": "Backend API"},
    {"bitbucket_repo": "org/frontend-app", "name": "Frontend App"},
    {"bitbucket_repo": "org/infra-tools", "name": "Infra Tools"},
]


@pytest.fixture
def fake_llm_parser() -> _FakeLLMParser:
    parser = _FakeLLMParser()
    set_llm_parser(parser)
    return parser


@pytest.fixture
def fake_jira_commenter() -> _FakeJiraCommenter:
    commenter = _FakeJiraCommenter()
    set_jira_commenter(commenter)
    return commenter


@pytest.fixture
def fake_jira_transitioner() -> _FakeJiraTransitioner:
    transitioner = _FakeJiraTransitioner()
    set_jira_transitioner(transitioner)
    return transitioner


@pytest.fixture(autouse=True)
def _setup_all_fakes(
    fake_llm_parser: _FakeLLMParser,
    fake_jira_commenter: _FakeJiraCommenter,
    fake_jira_transitioner: _FakeJiraTransitioner,
) -> None:
    """Ensure all fakes are registered before each test."""


def _make_input(
    structured_field_value: str | None = None,
    description: str = "Fix the login bug in the backend API",
    issue_key: str = "PAY-42",
    dept_id: str = "payments",
    workflow_id: str = "wf-test-001",
    repo_mappings: list[dict[str, Any]] | None = None,
    labels: list[str] | None = None,
) -> RepoResolveInput:
    return RepoResolveInput(
        issue_key=issue_key,
        dept_id=dept_id,
        workflow_id=workflow_id,
        structured_field_value=structured_field_value,
        description=description,
        repo_mappings=repo_mappings if repo_mappings is not None else _SAMPLE_REPO_MAPPINGS,
        labels=labels or [],
    )


# ---------------------------------------------------------------------------
# Tests: Structured field priority
# ---------------------------------------------------------------------------


class TestStructuredFieldPriority:
    """When structured field is non-empty, use it directly."""

    def test_structured_field_resolves_directly(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Non-empty structured field skips description parsing."""
        inp = _make_input(structured_field_value="org/backend-api")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert result.repo_url == "org/backend-api"
        assert result.confidence == 1.0
        assert result.needs_user_input is False
        assert result.error is None
        # LLM parser should NOT be called
        assert fake_llm_parser.calls == []

    def test_structured_field_with_whitespace_is_trimmed(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Structured field with leading/trailing whitespace is trimmed."""
        inp = _make_input(structured_field_value="  org/backend-api  ")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert result.repo_url == "org/backend-api"
        assert fake_llm_parser.calls == []

    def test_structured_field_case_insensitive_match(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Repo validation is case-insensitive."""
        inp = _make_input(structured_field_value="ORG/Backend-API")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert result.repo_url == "ORG/Backend-API"

    def test_empty_string_field_triggers_llm_parse(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Empty string structured field → falls through to LLM parse."""
        fake_llm_parser.response = {
            "repo_url": "org/backend-api",
            "confidence": 0.95,
        }
        inp = _make_input(structured_field_value="")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert fake_llm_parser.calls != []

    def test_none_field_triggers_llm_parse(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """None structured field → falls through to LLM parse."""
        fake_llm_parser.response = {
            "repo_url": "org/frontend-app",
            "confidence": 0.9,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert fake_llm_parser.calls != []

    def test_whitespace_only_field_triggers_llm_parse(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Whitespace-only structured field → falls through to LLM parse."""
        fake_llm_parser.response = {
            "repo_url": "org/infra-tools",
            "confidence": 0.85,
        }
        inp = _make_input(structured_field_value="   ")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert fake_llm_parser.calls != []


# ---------------------------------------------------------------------------
# Tests: Repo validation against allowed list
# ---------------------------------------------------------------------------


class TestRepoValidation:
    """Repo must exist in department's repo_mappings."""

    def test_structured_field_not_in_allowed_list_rejects(
        self,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """Repo not in mappings → reject + comment."""
        inp = _make_input(structured_field_value="org/unknown-repo")
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.repo_url is None
        assert result.needs_user_input is True
        assert "not in the allowed" in (result.error or "")

        # Rejection comment posted
        assert len(fake_jira_commenter.comments) == 1
        issue_key, body, dept_id = fake_jira_commenter.comments[0]
        assert issue_key == "PAY-42"
        assert "org/unknown-repo" in body
        assert "org/backend-api" in body  # allowed list shown
        assert "org/frontend-app" in body
        assert dept_id == "payments"

    def test_llm_parsed_repo_not_in_allowed_list_rejects(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """LLM-parsed repo not in mappings → reject."""
        fake_llm_parser.response = {
            "repo_url": "org/secret-repo",
            "confidence": 0.95,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True
        assert "not in the allowed" in (result.error or "")

        # Rejection comment posted
        assert len(fake_jira_commenter.comments) == 1
        assert "org/secret-repo" in fake_jira_commenter.comments[0][1]

    def test_empty_repo_mappings_always_rejects(
        self,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """Empty repo_mappings → any repo value is rejected."""
        inp = _make_input(
            structured_field_value="org/backend-api",
            repo_mappings=[],
        )
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True


# ---------------------------------------------------------------------------
# Tests: LLM parsing and confidence
# ---------------------------------------------------------------------------


class TestLLMParsing:
    """LLM parsing with confidence threshold."""

    def test_repo_label_resolves_before_llm(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        inp = _make_input(
            structured_field_value=None,
            labels=["team:payments", "repo:org/frontend-app"],
        )
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert result.repo_url == "org/frontend-app"
        assert fake_llm_parser.calls == []

    def test_high_confidence_resolves_successfully(
        self,
        fake_llm_parser: _FakeLLMParser,
    ) -> None:
        """Confidence >= 0.8 → accept."""
        fake_llm_parser.response = {
            "repo_url": "org/backend-api",
            "confidence": 0.9,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is True
        assert result.repo_url == "org/backend-api"
        assert result.confidence == 0.9
        assert result.needs_user_input is False

    def test_low_confidence_asks_user(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
        fake_jira_transitioner: _FakeJiraTransitioner,
    ) -> None:
        """Confidence < 0.8 → ask user via comment."""
        fake_llm_parser.response = {
            "repo_url": "org/backend-api",
            "confidence": 0.6,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True
        assert result.confidence == 0.6

        # Comment posted asking user
        assert len(fake_jira_commenter.comments) == 1
        assert "repository" in fake_jira_commenter.comments[0][1].lower()

    def test_exactly_threshold_asks_user(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """Confidence exactly at threshold (0.8) is below threshold → ask user.

        Values below 0.8 trigger asking. Exactly 0.8 should
        be accepted (>= 0.8 is the acceptance condition).
        """
        fake_llm_parser.response = {
            "repo_url": "org/backend-api",
            "confidence": 0.8,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        # 0.8 is >= threshold, so it should resolve
        assert result.resolved is True
        assert result.repo_url == "org/backend-api"
        assert result.confidence == 0.8

    def test_no_repo_parsed_asks_user(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """No repo parsed from description → ask user."""
        fake_llm_parser.response = {
            "repo_url": None,
            "confidence": 0.0,
        }
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True
        assert len(fake_jira_commenter.comments) == 1

    def test_llm_parse_failure_asks_user(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """LLM parse exception → ask user, return error."""
        fake_llm_parser.error = RuntimeError("LLM service unavailable")
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True
        assert result.confidence == 0.0
        assert "LLM parsing failed" in (result.error or "")
        assert len(fake_jira_commenter.comments) == 1


# ---------------------------------------------------------------------------
# Tests: Needs info transition
# ---------------------------------------------------------------------------


class TestNeedsInfoTransition:
    """Task transitions to needs_info when user input is needed."""

    def test_low_confidence_transitions_to_needs_info(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_transitioner: _FakeJiraTransitioner,
    ) -> None:
        """Confidence < 0.8 → transition to needs_info."""
        fake_llm_parser.response = {
            "repo_url": "org/backend-api",
            "confidence": 0.5,
        }
        inp = _make_input(structured_field_value=None)
        asyncio.run(resolve_repo_field(inp))

        assert len(fake_jira_transitioner.transitions) == 1
        issue_key, status, dept_id = fake_jira_transitioner.transitions[0]
        assert issue_key == "PAY-42"
        assert status == "needs_info"
        assert dept_id == "payments"

    def test_transition_failure_does_not_crash(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_transitioner: _FakeJiraTransitioner,
    ) -> None:
        """Transition failure is handled gracefully."""
        fake_llm_parser.response = {
            "repo_url": None,
            "confidence": 0.0,
        }
        fake_jira_transitioner.error = RuntimeError("Jira API error")
        inp = _make_input(structured_field_value=None)

        # Should not raise
        result = asyncio.run(resolve_repo_field(inp))
        assert result.resolved is False
        assert result.needs_user_input is True


# ---------------------------------------------------------------------------
# Tests: Comment posting resilience
# ---------------------------------------------------------------------------


class TestCommentResilience:
    """Comment posting failures don't crash the activity."""

    def test_rejection_comment_failure_still_returns_result(
        self,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """Comment failure on rejection doesn't crash."""
        fake_jira_commenter.error = RuntimeError("Jira unavailable")
        inp = _make_input(structured_field_value="org/unknown-repo")
        result = asyncio.run(resolve_repo_field(inp))

        # Still returns the rejection result
        assert result.resolved is False
        assert result.needs_user_input is True

    def test_ask_comment_failure_still_returns_result(
        self,
        fake_llm_parser: _FakeLLMParser,
        fake_jira_commenter: _FakeJiraCommenter,
    ) -> None:
        """Comment failure when asking user doesn't crash."""
        fake_llm_parser.response = {"repo_url": None, "confidence": 0.0}
        fake_jira_commenter.error = RuntimeError("Jira unavailable")
        inp = _make_input(structured_field_value=None)
        result = asyncio.run(resolve_repo_field(inp))

        assert result.resolved is False
        assert result.needs_user_input is True
