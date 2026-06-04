"""Bitbucket MCP activities for AgentRunnerWorkflow."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from temporalio import activity

from . import get_credential_resolver
from .mcp_tool import MCPToolError, call_mcp_tool, result_data, result_text


@dataclass(frozen=True)
class RepoRef:
    workspace: str
    repo_slug: str


@dataclass(frozen=True)
class BranchInfo:
    name: str
    target_hash: str
    already_existed: bool = False


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str = ""
    action: str = "update"


@dataclass(frozen=True)
class CommitInfo:
    commit_hash: str
    message: str


@dataclass(frozen=True)
class PRInfo:
    pr_id: int
    title: str
    url: str
    draft: bool = True


@dataclass(frozen=True)
class PRDiff:
    pr_id: int
    diff_content: str
    files_changed: list[str] = field(default_factory=list)


class BitbucketActivityError(RuntimeError):
    """Raised when a Bitbucket MCP operation fails unexpectedly."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _repo_value(repo: RepoRef | dict[str, Any], *names: str) -> str:
    for name in names:
        value = repo.get(name) if isinstance(repo, dict) else getattr(repo, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _repo_parts(repo: RepoRef | dict[str, Any], dept_id: str) -> tuple[str, str]:
    project_key = _repo_value(repo, "workspace", "project_key", "project")
    repo_slug = _repo_value(repo, "repo_slug", "slug", "repository")
    if not project_key or not repo_slug:
        try:
            creds = await get_credential_resolver().get(
                dept_id,
                "bitbucket",
                scope="org",
            )
        except Exception:  # noqa: BLE001
            creds = {}
        if isinstance(creds, dict):
            project_key = project_key or str(
                creds.get("bitbucket_workspace")
                or creds.get("project_key")
                or creds.get("workspace")
                or ""
            )
            repo_slug = repo_slug or str(
                creds.get("bitbucket_repo") or creds.get("repo_slug") or ""
            )
    if not project_key or not repo_slug:
        raise BitbucketActivityError("missing Bitbucket project/repo")
    return project_key, repo_slug


def _tool_data(result: dict[str, Any]) -> Any:
    data = result_data(result)
    if isinstance(data, dict):
        return data
    text = result_text(result)
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed


def _commit_hash(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("hash", "commit_hash", "id", "latestCommit"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    commit = data.get("commit")
    if isinstance(commit, dict):
        return str(commit.get("hash") or commit.get("id") or "")
    return ""


def _file_change(raw: FileChange | dict[str, Any]) -> FileChange:
    if isinstance(raw, FileChange):
        return raw
    return FileChange(
        path=str(raw.get("path") or ""),
        content=str(raw.get("content") or ""),
        action=str(raw.get("action") or "update").lower(),
    )


async def _bitbucket_tool(tool_name: str, args: dict[str, Any], dept_id: str) -> dict[str, Any]:
    # Bitbucket Cloud addresses repositories by ``workspace`` slug, while the
    # activity helpers carry that slug under ``project_key`` (the Server/DC
    # term). Mirror it onto ``workspace`` so a single call shape satisfies
    # both the Cloud and Server/DC tool variants.
    if "project_key" in args and "workspace" not in args:
        args = {**args, "workspace": args["project_key"]}
    try:
        return await call_mcp_tool(
            tool_name,
            args,
            dept_id=dept_id,
            service="bitbucket",
            timeout=60.0,
        )
    except MCPToolError as exc:
        raise BitbucketActivityError(
            f"{tool_name} failed: {exc.detail}",
            status_code=exc.status_code,
        ) from exc


@activity.defn(name="bitbucket_create_branch")
async def bitbucket_create_branch(
    repo: RepoRef | dict[str, Any],
    source_branch: str,
    new_branch: str,
    dept_id: str,
) -> BranchInfo:
    project_key, repo_slug = await _repo_parts(repo, dept_id)
    activity.heartbeat(f"creating branch {new_branch} in {project_key}/{repo_slug}")
    try:
        result = await _bitbucket_tool(
            "bitbucket_create_branch",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "branch_name": new_branch,
                "start_point": source_branch,
            },
            dept_id,
        )
    except BitbucketActivityError as exc:
        if "already" in str(exc).lower() or "409" in str(exc):
            return BranchInfo(new_branch, "", already_existed=True)
        raise
    data = _tool_data(result)
    return BranchInfo(new_branch, _commit_hash(data), already_existed=False)


@activity.defn(name="bitbucket_open_pr")
async def bitbucket_open_pr(
    repo: RepoRef | dict[str, Any],
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    dept_id: str,
) -> PRInfo:
    workspace, repo_slug = await _repo_parts(repo, dept_id)
    activity.heartbeat(f"opening PR {source_branch} to {target_branch}")
    data = _tool_data(
        await _bitbucket_tool(
            "bitbucket_create_pull_request",
            {
                "workspace": workspace,
                "repo_slug": repo_slug,
                "title": title,
                "source_branch": source_branch,
                "destination_branch": target_branch,
                "description": description,
            },
            dept_id,
        )
    )
    if not isinstance(data, dict):
        data = {}
    links = data.get("links")
    html = links.get("html") if isinstance(links, dict) else {}
    url = html.get("href") if isinstance(html, dict) else data.get("url", "")
    return PRInfo(
        pr_id=int(data.get("id") or data.get("pr_id") or 0),
        title=str(data.get("title") or title),
        url=str(url or ""),
        draft=True,
    )


@activity.defn(name="bitbucket_create_pull_request_cloud")
async def bitbucket_create_pull_request_cloud(
    repo: RepoRef | dict[str, Any],
    source_branch: str,
    target_branch: str,
    title: str,
    description: str,
    dept_id: str,
) -> PRInfo:
    """Compatibility alias for deployment-router selected cloud PR activity."""

    return await bitbucket_open_pr(
        repo,
        source_branch,
        target_branch,
        title,
        description,
        dept_id,
    )


@activity.defn(name="bitbucket_delete_branch")
async def bitbucket_delete_branch(
    repo: RepoRef | dict[str, Any],
    branch: str,
    dept_id: str,
) -> None:
    project_key, repo_slug = await _repo_parts(repo, dept_id)
    try:
        await _bitbucket_tool(
            "bitbucket_delete_branch",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "branch_name": branch,
            },
            dept_id,
        )
    except BitbucketActivityError as exc:
        if "404" in str(exc) or "not found" in str(exc).lower():
            return
        raise


@activity.defn(name="bitbucket_fetch_pr_diff")
async def bitbucket_fetch_pr_diff(
    repo: RepoRef | dict[str, Any],
    pr_id: int,
    dept_id: str,
) -> PRDiff:
    project_key, repo_slug = await _repo_parts(repo, dept_id)
    result = await _bitbucket_tool(
        "bitbucket_get_pull_request_diff",
        {"project_key": project_key, "repo_slug": repo_slug, "pr_id": pr_id},
        dept_id,
    )
    data = _tool_data(result)
    diff = data.get("diff") if isinstance(data, dict) else None
    if not diff:
        diff = result_text(result)
    files = data.get("files_changed", []) if isinstance(data, dict) else []
    if not isinstance(files, list):
        files = []
    return PRDiff(
        pr_id=pr_id,
        diff_content=str(diff or ""),
        files_changed=[str(item) for item in files],
    )


@activity.defn(name="bitbucket_add_pr_comment")
async def bitbucket_add_pr_comment(
    repo: RepoRef | dict[str, Any],
    pr_id: int,
    body: str,
    dept_id: str,
) -> None:
    project_key, repo_slug = await _repo_parts(repo, dept_id)
    await _bitbucket_tool(
        "bitbucket_add_pull_request_comment",
        {
            "project_key": project_key,
            "repo_slug": repo_slug,
            "pr_id": pr_id,
            "content": body,
        },
        dept_id,
    )
