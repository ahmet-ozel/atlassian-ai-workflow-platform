"""Test-local ``@workflow.defn`` stubs for end-to-end integration tests.

The production ``AgentRunnerWorkflow`` (under
``platform/workers/agent-runner-worker/src/workflows/agent_runner_workflow.py``)
is currently an empty stub. The end-to-end integration tests need a
registered child workflow under the name
``"AgentRunnerWorkflow"`` so AutomationWorkflow's
``execute_child_workflow("AgentRunnerWorkflow", ...)`` dispatch resolves.

This module provides three minimal child-workflow stubs, each registered
under the same Temporal workflow name (``"AgentRunnerWorkflow"``) but
exported under distinct Python class names so individual tests can
import only the branch they exercise. **The Temporal SDK requires that
only ONE class registered under a given workflow name be passed to a
worker at a time** — every test that uses a stub here registers exactly
one of these classes per ``Worker(...)`` invocation.

Why a dedicated module?
-----------------------

The Temporal workflow sandbox imports every module containing a
``@workflow.defn`` to validate it for non-deterministic side effects.
If the test module also performs filesystem work at import time
(``Path.resolve``, environment reads, etc.), the sandbox rejects the
module with ``RestrictedWorkflowAccessError``. Keeping the workflow
classes in this dedicated, side-effect-free module avoids that.

Compatibility with the production input shape
---------------------------------------------

``AutomationWorkflow`` constructs a child input via its private
``_AgentRunnerInputShape`` dataclass (see
``automation_workflow.py``). Temporal's data converter serialises
dataclasses field-by-field, so a *structurally* identical local
dataclass on the child side decodes the payload cleanly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow

__all__ = [
    "AgentRunnerInputShape",
    "ConfluenceDocUpdateAgentRunnerStub",
    "ResearchSummaryJiraAgentRunnerStub",
    "PRReviewAgentRunnerStub",
]


@dataclass(frozen=True)
class AgentRunnerInputShape:
    """Mirror of the child input shape AutomationWorkflow constructs.

 Must remain structurally identical to
 ``automation_workflow._AgentRunnerInputShape``.
 """

    parent_workflow_id: str
    issue_key: str
    department_id: str
    workflow_type: str
    target_repo: str | None
    target_branch: str
    output_actions: tuple[dict[str, Any], ...]
    iteration: int = 1


# ---------------------------------------------------------------------------
# 15.2: confluence_doc_update branch
# ---------------------------------------------------------------------------


@workflow.defn(name="AgentRunnerWorkflow")
class ConfluenceDocUpdateAgentRunnerStub:
    """Test-local AgentRunnerWorkflow exercising the confluence branch.

 Flow: ``confluence_search`` → ``confluence_get_page`` →
 ``llm_generate_doc`` → ``confluence_update_page``.

 Returns a short success summary; the parent's
 ``_stringify_child_result`` flattens it into the completion comment.
 """

    @workflow.run
    async def run(self, inp: AgentRunnerInputShape) -> str:
        assert inp.workflow_type in (
            "confluence_doc_update",
            "confluence_doc_create",
        ), f"unexpected workflow_type for confluence stub: {inp.workflow_type}"

        # 1. Search for the existing page.
        pages = await workflow.execute_activity(
            "confluence_search",
            args=["DOCS", "API guidelines", inp.department_id],
            start_to_close_timeout=timedelta(minutes=2),
        )
        page_id = pages[0]["id"]

        # 2. Read the current page body.
        page_data = await workflow.execute_activity(
            "confluence_get_page",
            args=[page_id, inp.department_id],
            start_to_close_timeout=timedelta(minutes=2),
        )

        # 3. LLM generates the updated body.
        doc_plan = {
            "title": page_data["title"],
            "outline": "Update API guidelines section",
            "target_space": "DOCS",
            "target_page_id": page_id,
        }
        doc_output = await workflow.execute_activity(
            "llm_generate_doc",
            args=[doc_plan, None],
            start_to_close_timeout=timedelta(minutes=5),
        )

        # 4. Persist the update.
        await workflow.execute_activity(
            "confluence_update_page",
            args=[
                page_id,
                doc_output["title"],
                doc_output["body"],
                inp.department_id,
            ],
            start_to_close_timeout=timedelta(minutes=2),
        )

        return f"Updated Confluence page {page_id}"


# ---------------------------------------------------------------------------
# 15.3: research_summary_jira branch
# ---------------------------------------------------------------------------


@workflow.defn(name="AgentRunnerWorkflow")
class ResearchSummaryJiraAgentRunnerStub:
    """Test-local AgentRunnerWorkflow exercising the research_summary branch.

 Flow: ``llm_research`` → ``jira_add_comment`` (with the research summary).
 """

    @workflow.run
    async def run(self, inp: AgentRunnerInputShape) -> str:
        assert inp.workflow_type == "research_summary_jira", (
            f"unexpected workflow_type for research stub: {inp.workflow_type}"
        )

        # 1. Run LLM research (web_search may or may not be enabled).
        research = await workflow.execute_activity(
            "llm_research",
            args=[
                f"Research findings for {inp.issue_key}",
                inp.department_id,
                True,  # web_search_enabled
            ],
            start_to_close_timeout=timedelta(minutes=10),
        )

        # 2. Post the summary as a Jira comment.
        summary = research["summary"]
        await workflow.execute_activity(
            "jira_add_comment",
            args=[inp.issue_key, summary, inp.department_id],
            start_to_close_timeout=timedelta(minutes=2),
        )

        return f"Posted research summary on {inp.issue_key}"


# ---------------------------------------------------------------------------
# 15.4: pr_review branch
# ---------------------------------------------------------------------------


@workflow.defn(name="AgentRunnerWorkflow")
class PRReviewAgentRunnerStub:
    """Test-local AgentRunnerWorkflow exercising the pr_review branch.

 Flow: ``bitbucket_fetch_pr_diff`` → ``llm_review_code`` →
 ``bitbucket_add_pr_comment``.

 The child input encodes the target PR via ``output_actions`` — the
 first action's payload carries ``workspace``, ``repo_slug``, and
 ``pr_id``. This mirrors how the bitbucket webhook handler
 populates the child input for ``pullrequest:reviewer_added`` events.
 """

    @workflow.run
    async def run(self, inp: AgentRunnerInputShape) -> str:
        assert inp.workflow_type == "pr_review", (
            f"unexpected workflow_type for pr_review stub: {inp.workflow_type}"
        )
        assert inp.output_actions, "pr_review stub requires output_actions"

        target = inp.output_actions[0]["payload"]
        repo = {"workspace": target["workspace"], "repo_slug": target["repo_slug"]}
        pr_id = int(target["pr_id"])

        # 1. Fetch the PR diff.
        diff = await workflow.execute_activity(
            "bitbucket_fetch_pr_diff",
            args=[repo, pr_id, inp.department_id],
            start_to_close_timeout=timedelta(minutes=5),
        )

        # 2. LLM reviews the diff.
        issue_data = {
            "key": inp.issue_key,
            "summary": "PR review",
            "description": "",
            "issue_type": "Story",
            "project_key": inp.issue_key.split("-", 1)[0]
            if "-" in inp.issue_key
            else "BB",
        }
        review = await workflow.execute_activity(
            "llm_review_code",
            args=[diff, issue_data],
            start_to_close_timeout=timedelta(minutes=10),
        )

        # 3. Post the review summary as a PR comment.
        await workflow.execute_activity(
            "bitbucket_add_pr_comment",
            args=[repo, pr_id, review["summary"], inp.department_id],
            start_to_close_timeout=timedelta(minutes=2),
        )

        return f"Reviewed PR #{pr_id}"
