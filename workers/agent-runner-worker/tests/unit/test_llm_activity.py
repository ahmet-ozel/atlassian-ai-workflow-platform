"""Unit tests for the LLM activity module.

Tests verify:
- llm_analyze_task renders prompt and parses LLM response correctly.
- llm_generate_code returns CodeOutput from LLM response.
- llm_generate_pr_description returns a string.
- llm_generate_doc returns DocOutput.
- llm_review_code returns ReviewOutput.
- llm_research performs graceful degradation when Firecrawl is unavailable.
- TaskAnalysisError propagates from invalid LLM responses.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure paths are set up for imports
_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_PLATFORM_ROOT = _WORKER_ROOT.parent.parent
sys.path.insert(0, str(_WORKER_ROOT))
sys.path.insert(0, str(_WORKER_ROOT / "src"))
sys.path.insert(0, str(_PLATFORM_ROOT / "libs" / "http-shared" / "src"))
sys.path.insert(0, str(_PLATFORM_ROOT / "libs" / "llm-orchestrator" / "src"))
sys.path.insert(0, str(_PLATFORM_ROOT / "libs" / "temporal-shared" / "src"))

from src.activities.llm import (
    CodeContext,
    CodeOutput,
    CodePlan,
    DeptContext,
    DocOutput,
    DocPlan,
    IssueData,
    PRDiff,
    ResearchData,
    ReviewOutput,
    _firecrawl_search,
    _get_llm_provider,
    _parse_json_from_llm,
    llm_analyze_task,
    llm_generate_code,
    llm_generate_doc,
    llm_generate_pr_description,
    llm_research,
    llm_review_code,
)
from src.prompts.parser import TaskAnalysisError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_issue_data() -> IssueData:
    return IssueData(
        issue_key="PAY-4211",
        summary="Fix callback error handling",
        description="The callback handler does not properly handle 5xx errors.",
        issue_type="Bug",
        project_key="PAY",
    )


@pytest.fixture
def sample_dept_context() -> DeptContext:
    return DeptContext(
        available_repos=["payment-service", "callback-gateway"],
        available_spaces=["PAYDOCS"],
        available_capabilities=["jira", "bitbucket", "execution", "confluence"],
        default_language="tr",
    )


@pytest.fixture
def valid_task_analysis_json() -> str:
    return json.dumps({
        "workflow_type": "code_change_with_test",
        "target_repo": "payment-service",
        "target_branch": "develop",
        "output_actions": [
            {"type": "bitbucket_pr", "payload": {"title": "Fix callback", "draft": True}},
            {"type": "jira_comment", "payload": {"body": "Done"}},
        ],
        "confidence": "high",
        "needs_info_question": None,
    })


# ---------------------------------------------------------------------------
# Tests: _parse_json_from_llm
# ---------------------------------------------------------------------------


class TestParseJsonFromLlm:
    def test_plain_json(self):
        raw = '{"key": "value"}'
        result = _parse_json_from_llm(raw)
        assert result == {"key": "value"}

    def test_json_with_markdown_fencing(self):
        raw = '```json\n{"key": "value"}\n```'
        result = _parse_json_from_llm(raw)
        assert result == {"key": "value"}

    def test_json_with_generic_fencing(self):
        raw = '```\n{"key": "value"}\n```'
        result = _parse_json_from_llm(raw)
        assert result == {"key": "value"}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_json_from_llm("not json at all")


# ---------------------------------------------------------------------------
# Tests: _get_llm_provider
# ---------------------------------------------------------------------------


class TestGetLlmProvider:
    def test_default_is_openai(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        provider = _get_llm_provider()
        assert provider.name == "openai"

    def test_default_openai_fails_closed_without_key(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = _get_llm_provider()
        with pytest.raises(Exception, match="OpenAI API key"):
            provider.complete("Hello world")


# ---------------------------------------------------------------------------
# Tests: llm_analyze_task
# ---------------------------------------------------------------------------


class TestLlmAnalyzeTask:
    @pytest.mark.asyncio
    async def test_successful_analysis(
        self, sample_issue_data, sample_dept_context, valid_task_analysis_json
    ):
        """Test that llm_analyze_task renders prompt and parses response."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = valid_task_analysis_json

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_analyze_task(sample_issue_data, sample_dept_context)

        assert result.workflow_type == "code_change_with_test"
        assert result.target_repo == "payment-service"
        assert result.target_branch == "develop"
        assert result.confidence == "high"
        assert len(result.output_actions) == 2
        # Verify draft is coerced to True
        assert result.output_actions[0].payload["draft"] is True

    @pytest.mark.asyncio
    async def test_invalid_json_raises_task_analysis_error(
        self, sample_issue_data, sample_dept_context
    ):
        """Test that invalid LLM JSON raises TaskAnalysisError."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "This is not JSON"

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                with pytest.raises(TaskAnalysisError):
                    await llm_analyze_task(sample_issue_data, sample_dept_context)

    @pytest.mark.asyncio
    async def test_invalid_workflow_type_raises(
        self, sample_issue_data, sample_dept_context
    ):
        """Test that invalid workflow_type raises TaskAnalysisError."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = json.dumps({
            "workflow_type": "invalid_type",
            "target_repo": "payment-service",
            "target_branch": "develop",
            "output_actions": [{"type": "jira_comment", "payload": {"body": "x"}}],
            "confidence": "high",
            "needs_info_question": None,
        })

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                with pytest.raises(TaskAnalysisError):
                    await llm_analyze_task(sample_issue_data, sample_dept_context)


# ---------------------------------------------------------------------------
# Tests: llm_generate_code
# ---------------------------------------------------------------------------


class TestLlmGenerateCode:
    @pytest.mark.asyncio
    async def test_json_response(self):
        """Test code generation with JSON response from LLM."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = json.dumps({
            "code": "def hello(): pass",
            "explanation": "Added hello function",
            "files": [{"path": "src/hello.py", "content": "def hello(): pass"}],
        })

        plan = CodePlan(issue_key="PAY-1", prompt="Add hello function")
        context = CodeContext(language="python")

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_generate_code(plan, context)

        assert isinstance(result, CodeOutput)
        assert result.code == "def hello(): pass"
        assert result.explanation == "Added hello function"

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        """Test code generation with plain text response (no JSON)."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "def hello(): pass"

        plan = CodePlan(issue_key="PAY-1", prompt="Add hello function")
        context = CodeContext()

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_generate_code(plan, context)

        assert isinstance(result, CodeOutput)
        assert result.code == "def hello(): pass"


# ---------------------------------------------------------------------------
# Tests: llm_generate_pr_description
# ---------------------------------------------------------------------------


class TestLlmGeneratePrDescription:
    @pytest.mark.asyncio
    async def test_returns_string(self, sample_issue_data):
        """Test PR description generation returns a string."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "## Summary\nFixed callback handling"

        diff = PRDiff(
            diff_content="--- a/src/handler.py\n+++ b/src/handler.py",
            files_changed=["src/handler.py"],
            additions=5,
            deletions=2,
        )

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_generate_pr_description(diff, sample_issue_data)

        assert isinstance(result, str)
        assert "Summary" in result


# ---------------------------------------------------------------------------
# Tests: llm_generate_doc
# ---------------------------------------------------------------------------


class TestLlmGenerateDoc:
    @pytest.mark.asyncio
    async def test_json_response(self):
        """Test doc generation with JSON response."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = json.dumps({
            "title": "API Guide",
            "body": "<h1>API Guide</h1><p>Content here</p>",
            "summary": "Created API guide",
        })

        plan = DocPlan(title="API Guide", outline="Overview, endpoints, examples")

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_generate_doc(plan, None)

        assert isinstance(result, DocOutput)
        assert result.title == "API Guide"
        assert "<h1>" in result.body

    @pytest.mark.asyncio
    async def test_plain_text_response(self):
        """Test doc generation with plain text response."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "# API Guide\n\nContent here"

        plan = DocPlan(title="API Guide", outline="Overview")

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_generate_doc(plan, None)

        assert isinstance(result, DocOutput)
        assert result.title == "API Guide"
        assert result.body == "# API Guide\n\nContent here"


# ---------------------------------------------------------------------------
# Tests: llm_review_code
# ---------------------------------------------------------------------------


class TestLlmReviewCode:
    @pytest.mark.asyncio
    async def test_json_response(self, sample_issue_data):
        """Test code review with JSON response."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = json.dumps({
            "summary": "Code looks good overall",
            "comments": [
                {"file": "src/handler.py", "line": "10", "body": "Consider error handling"}
            ],
            "approval": "approve",
        })

        diff = PRDiff(
            diff_content="--- a/src/handler.py\n+++ b/src/handler.py",
            files_changed=["src/handler.py"],
        )

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_review_code(diff, sample_issue_data)

        assert isinstance(result, ReviewOutput)
        assert result.approval == "approve"
        assert len(result.comments) == 1

    @pytest.mark.asyncio
    async def test_plain_text_response(self, sample_issue_data):
        """Test code review with plain text response."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "LGTM, no issues found"

        diff = PRDiff(diff_content="some diff")

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_review_code(diff, sample_issue_data)

        assert isinstance(result, ReviewOutput)
        assert result.summary == "LGTM, no issues found"
        assert result.approval == "comment"


# ---------------------------------------------------------------------------
# Tests: llm_research
# ---------------------------------------------------------------------------


class TestLlmResearch:
    @pytest.mark.asyncio
    async def test_without_web_search(self):
        """Test research without web search."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "Research summary about topic X"

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm.activity"):
                result = await llm_research("topic X", "payment", False)

        assert isinstance(result, ResearchData)
        assert result.summary == "Research summary about topic X"
        assert result.web_search_used is False
        assert result.sources == []

    @pytest.mark.asyncio
    async def test_with_web_search_firecrawl_unavailable(self):
        """Test research with web search when Firecrawl is unavailable (graceful degradation)."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "Research without web context"

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm._firecrawl_search", return_value=[]):
                with patch("src.activities.llm.activity"):
                    result = await llm_research("topic X", "payment", True)

        assert isinstance(result, ResearchData)
        assert result.web_search_used is False  # No results returned
        assert result.sources == []

    @pytest.mark.asyncio
    async def test_with_web_search_success(self):
        """Test research with successful web search results."""
        fake_provider = MagicMock()
        fake_provider.complete.return_value = "Research with web context"

        web_results = [
            {"url": "https://example.com", "title": "Example", "content": "Content"},
        ]

        with patch("src.activities.llm._get_llm_provider", return_value=fake_provider):
            with patch("src.activities.llm._firecrawl_search", return_value=web_results):
                with patch("src.activities.llm.activity"):
                    result = await llm_research("topic X", "payment", True)

        assert isinstance(result, ResearchData)
        assert result.web_search_used is True
        assert len(result.sources) == 1
        assert result.sources[0]["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# Tests: _firecrawl_search graceful degradation
# ---------------------------------------------------------------------------


class TestFirecrawlSearch:
    @pytest.mark.asyncio
    async def test_connection_error_returns_empty(self):
        """Test that connection errors return empty list (graceful degradation)."""
        import httpx

        with patch("src.activities.llm.activity"):
            with patch("httpx.AsyncClient.post", side_effect=httpx.ConnectError("unreachable")):
                result = await _firecrawl_search("test query")

        assert result == []

    @pytest.mark.asyncio
    async def test_403_returns_empty(self):
        """Test that 403 response returns empty list (graceful degradation)."""
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("src.activities.llm.activity"):
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                result = await _firecrawl_search("test query")

        assert result == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        """Test that timeout returns empty list (graceful degradation)."""
        import httpx

        with patch("src.activities.llm.activity"):
            with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
                result = await _firecrawl_search("test query")

        assert result == []
