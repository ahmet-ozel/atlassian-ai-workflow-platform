"""Runtime Atlassian probe client used by automation-service."""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .probe import AtlassianProbeClient as _AtlassianProbeClientProtocol

__all__ = ["AtlassianProbeClient"]


class AtlassianProbeClient:
    """Small production client for credential read/write probes.

    Jira and Confluence calls go through the stateless ``atlassian_unified``
    MCP endpoint with per-request credential headers. Bitbucket probe calls use
    Bitbucket's branch REST endpoints directly because the current MCP cloud
    repository tools reject the live app-password token even though the branch
    API accepts it.
    """

    __slots__ = ("_http_client", "_mcp_url", "_client_source")

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        mcp_base_url: str,
        client_source: str,
    ) -> None:
        self._http_client = http_client
        self._mcp_url = _normalise_mcp_url(mcp_base_url)
        self._client_source = client_source

    # ------------------------------------------------------------------
    # Jira
    # ------------------------------------------------------------------

    async def jira_myself(self, cred: Any) -> dict[str, Any]:
        # Prefer the dedicated current-user tool. Some pinned MCP image
        # builds predate ``jira_get_current_user_profile`` and only expose
        # ``jira_get_user_profile`` (which needs an explicit
        # ``user_identifier``). Fall back to the latter using the
        # credential's own username/email so the read probe authenticates
        # on both old and new MCP images.
        try:
            data = await self._call_tool(
                cred,
                "jira_get_current_user_profile",
                {},
                service="jira",
            )
        except RuntimeError as exc:
            if "unknown tool" not in str(exc).lower():
                raise
            identifier = str(getattr(cred, "username", "") or "").strip()
            if not identifier:
                raise
            data = await self._call_tool(
                cred,
                "jira_get_user_profile",
                {"user_identifier": identifier},
                service="jira",
            )
        user = data.get("user") if isinstance(data, dict) else None
        return user if isinstance(user, dict) else data

    async def jira_search_self_comments(
        self, cred: Any, author_account_id: str
    ) -> list[dict[str, Any]]:
        del cred, author_account_id
        return []

    async def jira_create_self_comment(
        self, cred: Any, body: str
    ) -> dict[str, Any]:
        issue_key = await self._find_jira_probe_issue(cred)
        created = await self._call_tool(
            cred,
            "jira_add_comment",
            {"issue_key": issue_key, "body": body},
            service="jira",
        )
        comment_id = created.get("id") if isinstance(created, dict) else None
        if not comment_id:
            raise RuntimeError("jira_add_comment_missing_id")
        return {"id": str(comment_id), "issue_key": issue_key}

    async def jira_delete_comment(
        self, cred: Any, issue_key: str, comment_id: str
    ) -> None:
        url = (
            f"{str(cred.url).rstrip('/')}/rest/api/3/issue/"
            f"{quote(issue_key, safe='')}/comment/{quote(comment_id, safe='')}"
        )
        response = await self._http_client.delete(
            url,
            headers={
                "Authorization": _basic_auth(cred.username, cred.personal_token),
                "Accept": "application/json",
            },
        )
        if response.status_code not in (200, 202, 204, 404):
            raise RuntimeError(f"jira_delete_comment_failed:{response.status_code}")

    # ------------------------------------------------------------------
    # Bitbucket
    # ------------------------------------------------------------------

    async def bitbucket_user(self, cred: Any) -> dict[str, Any]:
        del cred
        return {}

    async def bitbucket_list_probe_branches(
        self, cred: Any, workspace: str, repo: str
    ) -> list[str]:
        branches = await self._bitbucket_branches(cred, workspace, repo)
        return [
            str(item.get("name"))
            for item in branches
            if isinstance(item, dict)
            and str(item.get("name") or "").startswith("_AI_PROBE")
        ]

    async def bitbucket_create_branch(
        self, cred: Any, workspace: str, repo: str, branch_name: str
    ) -> str:
        repo_payload = await self._bitbucket_get(
            cred, f"/repositories/{workspace}/{repo}"
        )
        main_branch = (
            (repo_payload.get("mainbranch") or {}).get("name")
            if isinstance(repo_payload, dict)
            else None
        ) or "main"
        branch_payload = await self._bitbucket_get(
            cred,
            f"/repositories/{workspace}/{repo}/refs/branches/"
            f"{quote(str(main_branch), safe='')}",
        )
        start_hash = ((branch_payload.get("target") or {}).get("hash") or "").strip()
        if not start_hash:
            branches = await self._bitbucket_branches(cred, workspace, repo)
            if branches:
                start_hash = str((branches[0].get("target") or {}).get("hash") or "")
        if not start_hash:
            raise RuntimeError("bitbucket_start_point_not_found")

        created = await self._bitbucket_post(
            cred,
            f"/repositories/{workspace}/{repo}/refs/branches",
            {"name": branch_name, "target": {"hash": start_hash}},
        )
        return str((created.get("target") or {}).get("hash") or start_hash)

    async def bitbucket_delete_branch(
        self, cred: Any, workspace: str, repo: str, branch_name: str
    ) -> None:
        response = await self._http_client.delete(
            "https://api.bitbucket.org/2.0"
            f"/repositories/{workspace}/{repo}/refs/branches/"
            f"{quote(branch_name, safe='')}",
            headers=_bitbucket_headers(cred),
        )
        if response.status_code not in (200, 202, 204, 404):
            raise RuntimeError(f"bitbucket_delete_branch_failed:{response.status_code}")

    # ------------------------------------------------------------------
    # Confluence
    # ------------------------------------------------------------------

    async def confluence_user(self, cred: Any) -> dict[str, Any]:
        response = await self._http_client.get(
            f"{_confluence_origin(cred.url)}/wiki/rest/api/user/current",
            headers={
                "Authorization": _basic_auth(cred.username, cred.personal_token),
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        return response.json()

    async def confluence_list_probe_pages(
        self, cred: Any, space_key: str
    ) -> list[dict[str, Any]]:
        del cred, space_key
        return []

    async def confluence_create_draft_page(
        self, cred: Any, space_key: str, title: str
    ) -> dict[str, Any]:
        resolved_space_key = await self._resolve_confluence_space_key(
            cred,
            space_key,
        )
        response = await self._http_client.post(
            f"{_confluence_origin(cred.url)}/wiki/rest/api/content",
            headers={
                "Authorization": _basic_auth(cred.username, cred.personal_token),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={
                "type": "page",
                "title": title,
                "space": {"key": resolved_space_key},
                "body": {
                    "storage": {
                        "value": f"<p>{title}</p>",
                        "representation": "storage",
                    }
                },
            },
        )
        response.raise_for_status()
        data = response.json()
        page_id = data.get("id") if isinstance(data, dict) else None
        if not page_id:
            raise RuntimeError("confluence_create_page_missing_id")
        return {"id": str(page_id), "title": title}

    async def _resolve_confluence_space_key(self, cred: Any, space_key: str) -> str:
        if space_key and space_key not in {"__auto__", "auto"}:
            return space_key
        response = await self._http_client.get(
            f"{_confluence_origin(cred.url)}/wiki/rest/api/space?limit=1",
            headers={
                "Authorization": _basic_auth(cred.username, cred.personal_token),
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        results = response.json().get("results")
        if not isinstance(results, list) or not results:
            raise RuntimeError("confluence_probe_space_not_found")
        key = results[0].get("key") if isinstance(results[0], dict) else None
        if not key:
            raise RuntimeError("confluence_probe_space_missing_key")
        return str(key)

    async def confluence_delete_page(self, cred: Any, page_id: str) -> None:
        response = await self._http_client.delete(
            f"{_confluence_origin(cred.url)}/wiki/rest/api/content/"
            f"{quote(page_id, safe='')}",
            headers={
                "Authorization": _basic_auth(cred.username, cred.personal_token),
                "Accept": "application/json",
            },
        )
        if response.status_code not in (200, 202, 204, 404):
            raise RuntimeError(f"confluence_delete_page_failed:{response.status_code}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_jira_probe_issue(self, cred: Any) -> str:
        data = await self._call_tool(
            cred,
            "jira_search",
            {
                "jql": "reporter = currentUser() ORDER BY created DESC",
                "fields": "key,summary",
                "limit": 1,
            },
            service="jira",
        )
        issues = data.get("issues") if isinstance(data, dict) else None
        if not issues:
            raise RuntimeError("jira_probe_issue_not_found")
        issue = issues[0]
        key = issue.get("key") if isinstance(issue, dict) else None
        if not key:
            raise RuntimeError("jira_probe_issue_missing_key")
        return str(key)

    async def _call_tool(
        self,
        cred: Any,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        service: str,
    ) -> dict[str, Any]:
        headers = self._mcp_headers(cred, service)
        async with streamablehttp_client(
            self._mcp_url,
            headers=headers,
            timeout=30,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
        text = _tool_text(result)
        if getattr(result, "isError", False):
            raise RuntimeError(f"mcp_tool_error:{tool_name}:{text[:200]}")
        data = _loads(text)
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(f"mcp_tool_failed:{tool_name}")
        return data if isinstance(data, dict) else {"result": data}

    def _mcp_headers(self, cred: Any, service: str) -> dict[str, str]:
        headers = {"X-Client-Source": self._client_source}
        if service == "jira":
            headers.update(
                {
                    "X-Atlassian-Jira-Url": str(cred.url),
                    "X-Atlassian-Jira-Username": str(cred.username),
                    "X-Atlassian-Jira-Api-Token": str(cred.personal_token),
                }
            )
        elif service == "confluence":
            headers.update(
                {
                    "X-Atlassian-Confluence-Url": str(cred.url),
                    "X-Atlassian-Confluence-Username": str(cred.username),
                    "X-Atlassian-Confluence-Api-Token": str(cred.personal_token),
                }
            )
        return headers

    async def _bitbucket_get(
        self, cred: Any, path: str
    ) -> dict[str, Any]:
        response = await self._http_client.get(
            f"https://api.bitbucket.org/2.0{path}",
            headers=_bitbucket_headers(cred),
        )
        response.raise_for_status()
        return response.json()

    async def _bitbucket_post(
        self, cred: Any, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = await self._http_client.post(
            f"https://api.bitbucket.org/2.0{path}",
            headers={**_bitbucket_headers(cred), "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def _bitbucket_branches(
        self, cred: Any, workspace: str, repo: str
    ) -> list[dict[str, Any]]:
        data = await self._bitbucket_get(
            cred, f"/repositories/{workspace}/{repo}/refs/branches?pagelen=100"
        )
        values = data.get("values") if isinstance(data, dict) else None
        return list(values) if isinstance(values, list) else []

    # ------------------------------------------------------------------
    # Bitbucket — PO Review / orphan-branch read-only scanners
    # ------------------------------------------------------------------

    async def bitbucket_scan_pull_requests(
        self, cred: Any, workspace: str, repo: str
    ) -> list[dict[str, Any]]:
        """Return open/draft PRs projected into the PO-review shape.

        Each returned mapping carries the keys the PO Review API shim
        (`api/po_review._project_pull_requests`) expects: ``id``,
        ``source_branch``, ``is_draft``, ``author_account_id`` and
        ``title``. The Bitbucket Cloud ``/pullrequests`` endpoint is
        paginated; we follow ``next`` links up to a small cap so a
        busy repo cannot stall the request.
        """

        results: list[dict[str, Any]] = []
        # ``state=OPEN`` covers both ready and draft PRs; Bitbucket
        # exposes the draft flag on each PR object as ``draft``.
        path = (
            f"/repositories/{workspace}/{repo}/pullrequests"
            "?state=OPEN&pagelen=50"
        )
        for _ in range(10):  # hard page cap (50 * 10 = 500 PRs max)
            data = await self._bitbucket_get(cred, path)
            values = data.get("values") if isinstance(data, dict) else None
            for item in values or []:
                if not isinstance(item, dict):
                    continue
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                account_id = str(author.get("account_id") or author.get("uuid") or "")
                source = item.get("source") if isinstance(item.get("source"), dict) else {}
                branch = source.get("branch") if isinstance(source.get("branch"), dict) else {}
                source_branch = str(branch.get("name") or "")
                pr_id = item.get("id")
                if not isinstance(pr_id, int) or not source_branch or not account_id:
                    continue
                results.append(
                    {
                        "id": pr_id,
                        "source_branch": source_branch,
                        "is_draft": bool(item.get("draft", False)),
                        "author_account_id": account_id,
                        "title": str(item.get("title") or ""),
                    }
                )
            next_url = data.get("next") if isinstance(data, dict) else None
            if not isinstance(next_url, str) or not next_url:
                break
            # ``next`` is an absolute URL; strip the API origin so the
            # shared ``_bitbucket_get`` (which prepends the origin) works.
            path = next_url.split("https://api.bitbucket.org/2.0", 1)[-1]
        return results

    async def bitbucket_scan_branches(
        self, cred: Any, workspace: str, repo: str
    ) -> list[dict[str, Any]]:
        """Return branches projected into the orphan-branch scan shape.

        Each mapping carries ``name`` and ``last_commit_at`` (UTC
        ``datetime`` or ``None``) — the keys the PO Review API shim's
        `_project_branches` helper consumes.
        """

        from datetime import datetime as _dt

        raw = await self._bitbucket_branches(cred, workspace, repo)
        projected: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            raw_date = str(target.get("date") or "")
            last_commit_at = None
            if raw_date:
                try:
                    last_commit_at = _dt.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    last_commit_at = None
            projected.append({"name": name, "last_commit_at": last_commit_at})
        return projected


def _normalise_mcp_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    return base if base.endswith("/mcp") else f"{base}/mcp"


def _tool_text(result: Any) -> str:
    return "".join(str(getattr(item, "text", "")) for item in result.content)


def _loads(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _basic_auth(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _bitbucket_headers(cred: Any) -> dict[str, str]:
    return {
        "Authorization": _basic_auth(str(cred.username), str(cred.personal_token)),
        "Accept": "application/json",
    }


def _confluence_origin(url: str) -> str:
    parsed = urlparse(str(url))
    if not parsed.scheme or not parsed.netloc:
        return str(url).rstrip("/").removesuffix("/wiki")
    return f"{parsed.scheme}://{parsed.netloc}"


_ProtocolMatch: type[_AtlassianProbeClientProtocol] = AtlassianProbeClient  # type: ignore[assignment]
