"""LLM activity module for AgentRunnerWorkflow.

This module provides Temporal activities that interact with the LLM provider
for task analysis, code generation, PR description, documentation, code review,
and research. The LLM provider is obtained from :class:`LLMProviderFactory`
(``LLM_PROVIDER`` selects the configured real provider).

Activities:
- ``llm_analyze_task``: Renders the task_analysis.md Jinja2 prompt and parses
  the LLM response into a validated :class:`TaskAnalysis`.
- ``llm_generate_code``: Generates code based on a plan and context.
- ``llm_generate_pr_description``: Generates a PR description from a diff.
- ``llm_generate_doc``: Generates documentation content.
- ``llm_review_code``: Reviews code from a PR diff.
- ``llm_research``: Performs research with optional Firecrawl web search.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from temporalio import activity

from llm_orchestrator import LLMProviderFactory
from src.prompts.parser import (
    TaskAnalysis,
    TaskAnalysisError,
    parse_task_analysis,
    render_prompt,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Path to the prompts directory (relative to the worker package root).
_PROMPTS_DIR: Path = Path(__file__).resolve().parent.parent.parent / "prompts"

#: Default Firecrawl endpoint. Overridable via FIRECRAWL_ENDPOINT env var.
_DEFAULT_FIRECRAWL_ENDPOINT: str = "http://firecrawl:3002"

#: Firecrawl request timeout in seconds.
_FIRECRAWL_TIMEOUT: float = 30.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueData:
    """Jira issue data passed to LLM activities.

    Attributes
    ----------
    issue_key : str
        The Jira issue key (e.g. ``PAY-4211``).
    summary : str
        Issue summary/title.
    description : str
        Full issue description text.
    issue_type : str
        Issue type (e.g. ``Story``, ``Bug``, ``Task``).
    project_key : str
        The Jira project key (e.g. ``PAY``).
    """

    issue_key: str
    summary: str
    description: str
    issue_type: str
    project_key: str


@dataclass(frozen=True)
class DeptContext:
    """Department context for LLM task analysis.

    Attributes
    ----------
    available_repos : list[str]
        List of repository names available to the department.
    available_spaces : list[str]
        List of Confluence space keys available.
    available_capabilities : list[str]
        List of capability strings the department has.
    default_language : str
        Default language for output (e.g. ``"tr"``).
    """

    available_repos: list[str] = field(default_factory=list)
    available_spaces: list[str] = field(default_factory=list)
    available_capabilities: list[str] = field(default_factory=list)
    default_language: str = "tr"


@dataclass(frozen=True)
class CodePlan:
    """Plan for code generation.

    Attributes
    ----------
    issue_key : str
        The Jira issue key.
    prompt : str
        Detailed instruction for code generation.
    target_repo : str | None
        Target repository name.
    target_branch : str | None
        Target branch name.
    """

    issue_key: str
    prompt: str
    target_repo: str | None = None
    target_branch: str | None = None


@dataclass(frozen=True)
class CodeContext:
    """Context for code generation.

    Attributes
    ----------
    existing_files : list[str]
        List of relevant existing file paths.
    language : str
        Primary programming language.
    framework : str
        Primary framework in use.
    """

    existing_files: list[str] = field(default_factory=list)
    language: str = ""
    framework: str = ""


@dataclass(frozen=True)
class CodeOutput:
    """Output from LLM code generation.

    Attributes
    ----------
    code : str
        Generated code content.
    explanation : str
        Explanation of what was generated.
    files : list[dict[str, str]]
        List of file changes with path and content.
    """

    code: str
    explanation: str
    files: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PRDiff:
    """Pull request diff data.

    Attributes
    ----------
    diff_content : str
        Unified diff content.
    files_changed : list[str]
        List of changed file paths.
    additions : int
        Number of lines added.
    deletions : int
        Number of lines deleted.
    """

    diff_content: str
    files_changed: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class DocPlan:
    """Plan for documentation generation.

    Attributes
    ----------
    title : str
        Document title.
    outline : str
        Document outline or structure description.
    target_space : str | None
        Target Confluence space key.
    target_page_id : str | None
        Target page ID for updates.
    """

    title: str
    outline: str
    target_space: str | None = None
    target_page_id: str | None = None


@dataclass(frozen=True)
class DocOutput:
    """Output from LLM documentation generation.

    Attributes
    ----------
    title : str
        Final document title.
    body : str
        Generated document body (Confluence storage format or markdown).
    summary : str
        Brief summary of what was generated.
    """

    title: str
    body: str
    summary: str = ""


@dataclass(frozen=True)
class ReviewOutput:
    """Output from LLM code review.

    Attributes
    ----------
    summary : str
        Overall review summary.
    comments : list[dict[str, str]]
        List of review comments with file, line, and body.
    approval : str
        Review verdict: ``"approve"``, ``"request_changes"``, or ``"comment"``.
    """

    summary: str
    comments: list[dict[str, str]] = field(default_factory=list)
    approval: str = "comment"


@dataclass(frozen=True)
class ResearchData:
    """Output from LLM research activity.

    Attributes
    ----------
    summary : str
        Research summary text.
    sources : list[dict[str, str]]
        List of sources with url and title.
    raw_content : str
        Raw research content for further processing.
    web_search_used : bool
        Whether web search (Firecrawl) was used.
    """

    summary: str
    sources: list[dict[str, str]] = field(default_factory=list)
    raw_content: str = ""
    web_search_used: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_llm_provider():
    """Get the LLM provider instance from the factory.

    Uses ``LLM_PROVIDER`` env var (defaults to the production provider).
    """
    return LLMProviderFactory.from_env()


def _get_firecrawl_endpoint() -> str:
    """Get the Firecrawl endpoint from environment."""
    return os.environ.get("FIRECRAWL_ENDPOINT", _DEFAULT_FIRECRAWL_ENDPOINT)


async def _firecrawl_search(query: str) -> list[dict[str, str]]:
    """Perform a web search via Firecrawl with graceful degradation.

    If Firecrawl is unreachable or returns an error (403, timeout, etc.),
    returns an empty list instead of raising.

    Parameters
    ----------
    query : str
        The search query string.

    Returns
    -------
    list[dict[str, str]]
        List of search results with ``url``, ``title``, and ``content`` keys.
        Empty list on any failure (graceful degradation).
    """
    endpoint = _get_firecrawl_endpoint()

    try:
        async with httpx.AsyncClient(timeout=_FIRECRAWL_TIMEOUT) as client:
            response = await client.post(
                f"{endpoint}/v0/search",
                json={"query": query},
            )
            if response.status_code != 200:
                activity.logger.warning(
                    "Firecrawl search returned status %d for query: %s",
                    response.status_code,
                    query,
                )
                return []

            data = response.json()
            results: list[dict[str, str]] = []
            for item in data.get("data", []):
                results.append({
                    "url": item.get("url", ""),
                    "title": item.get("title", ""),
                    "content": item.get("content", item.get("markdown", "")),
                })
            return results

    except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
        # Graceful degradation: log warning and return empty results
        activity.logger.warning(
            "Firecrawl search failed (graceful degradation): %s", exc
        )
        return []


def _parse_json_from_llm(raw: str) -> dict[str, Any]:
    """Attempt to parse JSON from LLM output, handling markdown fencing.

    LLMs sometimes wrap JSON in ```json ... ``` blocks. This helper
    strips that wrapping before parsing.
    """
    text = raw.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last line if it's ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    return json.loads(text)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


@activity.defn(name="llm_analyze_task")
async def llm_analyze_task(
    issue_data: IssueData,
    dept_context: DeptContext,
) -> TaskAnalysis:
    """Analyze a Jira task using LLM and return a structured execution plan.

    This activity:
    1. Renders the ``prompts/task_analysis.md`` Jinja2 template with issue
       and department context.
    2. Calls the LLM provider to generate a response.
    3. Parses and validates the response using :func:`parse_task_analysis`.

    Parameters
    ----------
    issue_data : IssueData
        The Jira issue data to analyze.
    dept_context : DeptContext
        Department context (repos, spaces, capabilities, language).

    Returns
    -------
    TaskAnalysis
        Validated task analysis result.

    Raises
    ------
    TaskAnalysisError
        If the LLM output fails validation (propagated to caller workflow).
    """
    activity.heartbeat("rendering task analysis prompt")

    # Build template context
    template_context = {
        "issue_key": issue_data.issue_key,
        "issue_summary": issue_data.summary,
        "issue_description": issue_data.description,
        "issue_type": issue_data.issue_type,
        "project_key": issue_data.project_key,
        "department_context": {
            "available_repos": dept_context.available_repos,
            "available_spaces": dept_context.available_spaces,
            "available_capabilities": dept_context.available_capabilities,
            "default_language": dept_context.default_language,
        },
    }

    # Render the prompt template
    template_path = _PROMPTS_DIR / "task_analysis.md"
    prompt = render_prompt(template_path, template_context)

    activity.heartbeat("calling LLM provider for task analysis")

    # Call LLM provider
    provider = _get_llm_provider()
    raw_response = provider.complete(prompt)

    activity.heartbeat("parsing LLM response")

    # Parse the response
    try:
        parsed_data = _parse_json_from_llm(raw_response)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TaskAnalysisError(
            f"LLM returned invalid JSON: {exc}. Raw response: {raw_response[:200]}"
        ) from exc

    # Validate and return
    return parse_task_analysis(parsed_data)


@activity.defn(name="llm_generate_code")
async def llm_generate_code(plan: CodePlan, context: CodeContext) -> CodeOutput:
    """Generate code using the LLM provider.

    Parameters
    ----------
    plan : CodePlan
        The code generation plan with issue key and prompt.
    context : CodeContext
        Context about existing files, language, and framework.

    Returns
    -------
    CodeOutput
        Generated code with explanation and file list.
    """
    activity.heartbeat("generating code via LLM")

    # Build the prompt for code generation
    prompt_parts = [
        f"Generate code for issue {plan.issue_key}.",
        f"\nTask: {plan.prompt}",
    ]

    if plan.target_repo:
        prompt_parts.append(f"\nTarget repository: {plan.target_repo}")
    if plan.target_branch:
        prompt_parts.append(f"\nTarget branch: {plan.target_branch}")
    if context.language:
        prompt_parts.append(f"\nPrimary language: {context.language}")
    if context.framework:
        prompt_parts.append(f"\nFramework: {context.framework}")
    if context.existing_files:
        prompt_parts.append(
            f"\nRelevant existing files:\n"
            + "\n".join(f"- {f}" for f in context.existing_files)
        )

    prompt = "\n".join(prompt_parts)

    # Try to render from template if it exists
    template_path = _PROMPTS_DIR / "code_generation.md"
    if template_path.is_file() and template_path.read_text().strip() not in (
        "",
        "<!-- TODO: prompt content -->",
    ):
        prompt = render_prompt(
            template_path,
            {
                "issue_key": plan.issue_key,
                "task_prompt": plan.prompt,
                "target_repo": plan.target_repo,
                "target_branch": plan.target_branch,
                "language": context.language,
                "framework": context.framework,
                "existing_files": context.existing_files,
            },
        )

    provider = _get_llm_provider()
    raw_response = provider.complete(prompt)

    # Parse response — attempt JSON, fall back to raw text
    try:
        data = _parse_json_from_llm(raw_response)
        return CodeOutput(
            code=data.get("code", raw_response),
            explanation=data.get("explanation", ""),
            files=data.get("files", []),
        )
    except (json.JSONDecodeError, TypeError):
        # LLM returned plain text code
        return CodeOutput(
            code=raw_response,
            explanation="",
            files=[],
        )


@activity.defn(name="llm_generate_pr_description")
async def llm_generate_pr_description(
    diff: PRDiff,
    issue_data: IssueData,
) -> str:
    """Generate a pull request description from a diff and issue data.

    Parameters
    ----------
    diff : PRDiff
        The PR diff content and metadata.
    issue_data : IssueData
        The related Jira issue data.

    Returns
    -------
    str
        Generated PR description in markdown format.
    """
    activity.heartbeat("generating PR description via LLM")

    prompt_parts = [
        f"Generate a pull request description for issue {issue_data.issue_key}.",
        f"\nIssue summary: {issue_data.summary}",
        f"\nIssue description: {issue_data.description}",
        f"\nFiles changed: {', '.join(diff.files_changed)}",
        f"\nAdditions: {diff.additions}, Deletions: {diff.deletions}",
        f"\nDiff:\n{diff.diff_content[:4000]}",  # Truncate large diffs
    ]

    prompt = "\n".join(prompt_parts)

    # Try to render from template if it exists
    template_path = _PROMPTS_DIR / "pr_description.md"
    if template_path.is_file() and template_path.read_text().strip() not in (
        "",
        "<!-- TODO: prompt content -->",
    ):
        prompt = render_prompt(
            template_path,
            {
                "issue_key": issue_data.issue_key,
                "issue_summary": issue_data.summary,
                "issue_description": issue_data.description,
                "files_changed": diff.files_changed,
                "additions": diff.additions,
                "deletions": diff.deletions,
                "diff_content": diff.diff_content[:4000],
            },
        )

    provider = _get_llm_provider()
    return provider.complete(prompt)


@activity.defn(name="llm_generate_doc")
async def llm_generate_doc(
    plan: DocPlan,
    research_data: ResearchData | None,
) -> DocOutput:
    """Generate documentation content using the LLM provider.

    Parameters
    ----------
    plan : DocPlan
        The documentation plan with title and outline.
    research_data : ResearchData | None
        Optional research data to incorporate into the document.

    Returns
    -------
    DocOutput
        Generated document with title, body, and summary.
    """
    activity.heartbeat("generating documentation via LLM")

    prompt_parts = [
        f"Generate documentation with the following plan:",
        f"\nTitle: {plan.title}",
        f"\nOutline: {plan.outline}",
    ]

    if plan.target_space:
        prompt_parts.append(f"\nTarget Confluence space: {plan.target_space}")

    if research_data and research_data.raw_content:
        prompt_parts.append(f"\nResearch context:\n{research_data.raw_content[:4000]}")
        if research_data.sources:
            sources_text = "\n".join(
                f"- [{s.get('title', 'Source')}]({s.get('url', '')})"
                for s in research_data.sources
            )
            prompt_parts.append(f"\nSources:\n{sources_text}")

    prompt = "\n".join(prompt_parts)

    # Try to render from template if it exists
    template_path = _PROMPTS_DIR / "doc_generation.md"
    if template_path.is_file() and template_path.read_text().strip() not in (
        "",
        "<!-- TODO: prompt content -->",
    ):
        prompt = render_prompt(
            template_path,
            {
                "title": plan.title,
                "outline": plan.outline,
                "target_space": plan.target_space,
                "research_content": (
                    research_data.raw_content[:4000] if research_data else ""
                ),
                "sources": research_data.sources if research_data else [],
            },
        )

    provider = _get_llm_provider()
    raw_response = provider.complete(prompt)

    # Parse response — attempt JSON, fall back to raw text as body
    try:
        data = _parse_json_from_llm(raw_response)
        return DocOutput(
            title=data.get("title", plan.title),
            body=data.get("body", raw_response),
            summary=data.get("summary", ""),
        )
    except (json.JSONDecodeError, TypeError):
        return DocOutput(
            title=plan.title,
            body=raw_response,
            summary="",
        )


@activity.defn(name="llm_review_code")
async def llm_review_code(
    diff: PRDiff,
    issue_data: IssueData,
) -> ReviewOutput:
    """Review code from a PR diff using the LLM provider.

    Parameters
    ----------
    diff : PRDiff
        The PR diff content and metadata.
    issue_data : IssueData
        The related Jira issue data for context.

    Returns
    -------
    ReviewOutput
        Review summary, comments, and approval verdict.
    """
    activity.heartbeat("reviewing code via LLM")

    prompt_parts = [
        f"Review the following pull request for issue {issue_data.issue_key}.",
        f"\nIssue summary: {issue_data.summary}",
        f"\nIssue description: {issue_data.description}",
        f"\nFiles changed: {', '.join(diff.files_changed)}",
        f"\nAdditions: {diff.additions}, Deletions: {diff.deletions}",
        f"\nDiff:\n{diff.diff_content[:8000]}",  # Allow more context for review
    ]

    prompt = "\n".join(prompt_parts)

    # Try to render from template if it exists
    template_path = _PROMPTS_DIR / "pr_review.md"
    if template_path.is_file() and template_path.read_text().strip() not in (
        "",
        "<!-- TODO: prompt content -->",
    ):
        prompt = render_prompt(
            template_path,
            {
                "issue_key": issue_data.issue_key,
                "issue_summary": issue_data.summary,
                "issue_description": issue_data.description,
                "files_changed": diff.files_changed,
                "additions": diff.additions,
                "deletions": diff.deletions,
                "diff_content": diff.diff_content[:8000],
            },
        )

    provider = _get_llm_provider()
    raw_response = provider.complete(prompt)

    # Parse response — attempt JSON, fall back to summary-only
    try:
        data = _parse_json_from_llm(raw_response)
        return ReviewOutput(
            summary=data.get("summary", raw_response),
            comments=data.get("comments", []),
            approval=data.get("approval", "comment"),
        )
    except (json.JSONDecodeError, TypeError):
        return ReviewOutput(
            summary=raw_response,
            comments=[],
            approval="comment",
        )


@activity.defn(name="llm_research")
async def llm_research(
    query: str,
    dept_id: str,
    web_search_enabled: bool,
) -> ResearchData:
    """Perform research using the LLM provider with optional web search.

    When ``web_search_enabled=True``, this activity first calls Firecrawl
    to gather web search results, then passes them to the LLM for synthesis.
    If Firecrawl is unreachable or returns an error (403, timeout, network
    error), the activity gracefully degrades to LLM-only research without
    web context.

    Parameters
    ----------
    query : str
        The research query string.
    dept_id : str
        Department ID for context.
    web_search_enabled : bool
        Whether to attempt web search via Firecrawl.

    Returns
    -------
    ResearchData
        Research results with summary, sources, and raw content.
    """
    activity.heartbeat("starting research")

    web_results: list[dict[str, str]] = []
    web_search_used = False

    # Optionally perform web search via Firecrawl
    if web_search_enabled:
        activity.heartbeat("performing web search via Firecrawl")
        web_results = await _firecrawl_search(query)
        web_search_used = len(web_results) > 0

    # Build the research prompt
    prompt_parts = [
        f"Research the following topic and provide a comprehensive summary:",
        f"\nQuery: {query}",
        f"\nDepartment: {dept_id}",
    ]

    if web_results:
        prompt_parts.append("\nWeb search results:")
        for i, result in enumerate(web_results[:10], 1):  # Limit to 10 results
            prompt_parts.append(
                f"\n{i}. [{result.get('title', 'Untitled')}]({result.get('url', '')})"
                f"\n   {result.get('content', '')[:500]}"
            )

    prompt = "\n".join(prompt_parts)

    # Try to render from template if it exists
    template_path = _PROMPTS_DIR / "research.md"
    if template_path.is_file() and template_path.read_text().strip() not in (
        "",
        "<!-- TODO: prompt content -->",
    ):
        prompt = render_prompt(
            template_path,
            {
                "query": query,
                "dept_id": dept_id,
                "web_results": web_results,
                "web_search_enabled": web_search_enabled,
            },
        )

    activity.heartbeat("calling LLM for research synthesis")

    provider = _get_llm_provider()
    raw_response = provider.complete(prompt)

    # Build sources from web results
    sources = [
        {"url": r.get("url", ""), "title": r.get("title", "")}
        for r in web_results
        if r.get("url")
    ]

    return ResearchData(
        summary=raw_response,
        sources=sources,
        raw_content=raw_response,
        web_search_used=web_search_used,
    )
