"""Live mutating smoke tests for external Atlassian write flows."""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import httpx
from audit_logger import AuditEvent
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.dependencies import require_admin

router = APIRouter(prefix="/api/v1/live-smoke", tags=["live-smoke"], dependencies=[Depends(require_admin)])

def _platform_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    for parent in Path(__file__).resolve().parents:
        if (parent / "config").is_dir():
            return parent
    return Path("/app")

def _departments() -> list[dict[str, Any]]:
    path = _platform_root() / "config" / "departments.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("departments", [])
    return rows if isinstance(rows, list) else []

def _dept_or_404(dept_id: str) -> dict[str, Any]:
    dept = next((d for d in _departments() if d.get("id") == dept_id), None)
    if not isinstance(dept, dict):
        raise HTTPException(status_code=404, detail=f"department {dept_id!r} not found")
    return dept

async def _credential(request: Request, dept: Mapping[str, Any], service: str) -> dict[str, str]:
    bot = dept.get("bot") if isinstance(dept.get("bot"), Mapping) else {}
    section = bot.get(service) if isinstance(bot.get(service), Mapping) else {}
    ref = str(section.get("credential_ref") or "")
    if not ref:
        raise HTTPException(status_code=409, detail=f"{service} credential_ref is not configured")
    vault = getattr(request.app.state, "vault_client", None)
    if vault is None:
        raise HTTPException(status_code=503, detail="vault client is not wired")
    secret = await vault.read_kv2_secret(path=ref)
    if not secret:
        raise HTTPException(status_code=502, detail=f"{service} credential could not be read")
    return {str(k): str(v) for k, v in secret.items()}

def _http(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "http_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="http client is not wired")
    return client

def _username(secret: Mapping[str, str]) -> str:
    return str(secret.get("username") or secret.get("email") or "")

def _token(secret: Mapping[str, str]) -> str:
    return str(
        secret.get("app_password")
        or secret.get("password")
        or secret.get("api_token")
        or secret.get("personal_token")
        or secret.get("token")
        or ""
    )

def _basic(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")

def _confluence_origin(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    if not parsed.scheme or not parsed.netloc:
        return raw_url.rstrip("/").removesuffix("/wiki")
    return f"{parsed.scheme}://{parsed.netloc}"

def _step(name: str, status_text: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "status": status_text, **extra}

def _short_error(response: httpx.Response) -> str:
    text = response.text[:300]
    return f"HTTP {response.status_code}: {text}"

async def _expect(response: httpx.Response, *, step: str) -> httpx.Response:
    if response.status_code >= 400:
        raise RuntimeError(f"{step} failed: {_short_error(response)}")
    return response

async def _audit(request: Request, dept_id: str, action: str, result: str, payload: dict[str, Any]) -> None:
    sink = getattr(request.app.state, "workflow_control_audit_sink", None)
    if sink is None:
        sink = getattr(request.app.state, "dept_audit_sink", None)
    if sink is None:
        sink = getattr(request.app.state, "audit_sink", None)
    if sink is None:
        sink = getattr(request.app.state, "audit_logger", None)
    if sink is None:
        sink = getattr(request.app.state, "prompts_audit_sink", None)
    if sink is None:
        return
    audit_result = "ok" if result == "passed" else "error"
    event = AuditEvent(
        actor_id="admin-dashboard",
        actor_role="admin",
        dept_id=dept_id,
        action=action,
        resource=f"department:{dept_id}",
        result=audit_result,
        payload=payload,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await sink.write(event)
    except Exception:
        return

def _bitbucket_target(dept: Mapping[str, Any], secret: Mapping[str, str]) -> tuple[str, str, str]:
    mappings = dept.get("repo_mappings")
    if isinstance(mappings, list):
        for row in mappings:
            if not isinstance(row, Mapping):
                continue
            workspace = row.get("bitbucket_workspace") or dept.get("bitbucket_workspace")
            repo = row.get("bitbucket_repo") or row.get("repo_slug")
            branch = row.get("default_branch") or row.get("branch_pattern") or "main"
            if workspace and repo:
                return str(workspace), str(repo), str(branch)
    parsed = urlparse(str(secret.get("url") or ""))
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2:
        return parts[0], parts[1], "main"
    workspace = str(dept.get("bitbucket_workspace") or "")
    if workspace:
        return workspace, "smoke-test", "main"
    raise RuntimeError("bitbucket workspace/repo target is not configured")


def _bb_headers(secret: Mapping[str, str]) -> dict[str, str]:
    username = _username(secret)
    token = _token(secret)
    if not token:
        raise RuntimeError("bitbucket credential token is missing")
    if token.startswith("ATCTT") and not username:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    return {"Authorization": _basic(username, token), "Accept": "application/json"}


async def _bb_get(client: httpx.AsyncClient, secret: Mapping[str, str], path: str) -> dict[str, Any]:
    response = await client.get(f"https://api.bitbucket.org/2.0{path}", headers=_bb_headers(secret))
    await _expect(response, step=path)
    data = response.json()
    return data if isinstance(data, dict) else {}


async def _bb_post(
    client: httpx.AsyncClient,
    secret: Mapping[str, str],
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        f"https://api.bitbucket.org/2.0{path}",
        headers={**_bb_headers(secret), "Content-Type": "application/json"},
        json=payload,
    )
    await _expect(response, step=path)
    data = response.json()
    return data if isinstance(data, dict) else {}


@router.post("/{dept_id}/bitbucket")
async def run_bitbucket_smoke(dept_id: str, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    dept = _dept_or_404(dept_id)
    secret = await _credential(request, dept, "bitbucket")
    client = _http(request)
    workspace, repo, default_branch = _bitbucket_target(dept, secret)
    nonce = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(3)
    branch = f"ai/live-smoke-{nonce}"
    file_path = f"live-smoke/{nonce}.md"
    title = f"AI live smoke {nonce}"
    steps: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    pr_id: int | None = None
    commit_hash = ""
    result = "failed"
    try:
        repo_payload = await _bb_get(client, secret, f"/repositories/{workspace}/{repo}")
        default_branch = str(((repo_payload.get("mainbranch") or {}).get("name")) or default_branch)
        main_ref = await _bb_get(
            client,
            secret,
            f"/repositories/{workspace}/{repo}/refs/branches/{quote(default_branch, safe='')}",
        )
        start_hash = str(((main_ref.get("target") or {}).get("hash")) or "")
        if not start_hash:
            raise RuntimeError("default branch hash could not be resolved")
        steps.append(_step("resolve-default-branch", "passed", branch=default_branch, hash=start_hash[:12]))

        await _bb_post(
            client,
            secret,
            f"/repositories/{workspace}/{repo}/refs/branches",
            {"name": branch, "target": {"hash": start_hash}},
        )
        steps.append(_step("create-branch", "passed", branch=branch))

        content = (
            f"# AI live smoke\n\n"
            f"department: {dept_id}\n"
            f"created_at: {datetime.now(timezone.utc).isoformat()}\n"
        )
        response = await client.post(
            f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/src",
            headers=_bb_headers(secret),
            files={
                "branch": (None, branch),
                "message": (None, title),
                file_path: (None, content),
            },
        )
        await _expect(response, step="bitbucket commit")
        commit_payload = response.json() if response.text else {}
        commit_hash = str(
            ((commit_payload.get("commit") or {}).get("hash"))
            or commit_payload.get("hash")
            or ""
        )
        steps.append(_step("commit-file", "passed", path=file_path, commit=commit_hash[:12]))

        pr_payload = await _bb_post(
            client,
            secret,
            f"/repositories/{workspace}/{repo}/pullrequests",
            {
                "title": title,
                "description": "Admin dashboard live smoke PR. It will be declined by cleanup.",
                "source": {"branch": {"name": branch}},
                "destination": {"branch": {"name": default_branch}},
                "close_source_branch": True,
            },
        )
        pr_id = int(pr_payload.get("id") or 0)
        pr_url = str(((pr_payload.get("links") or {}).get("html") or {}).get("href") or "")
        steps.append(_step("create-pr", "passed", pr_id=pr_id, url=pr_url))

        if pr_id:
            declined = await client.post(
                f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests/{pr_id}/decline",
                headers=_bb_headers(secret),
            )
            await _expect(declined, step="decline pull request")
            cleanup.append(_step("decline-pr", "passed", pr_id=pr_id))
        deleted = await client.delete(
            f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/refs/branches/{quote(branch, safe='')}",
            headers=_bb_headers(secret),
        )
        if deleted.status_code not in {200, 202, 204, 404}:
            raise RuntimeError(f"delete branch failed: {_short_error(deleted)}")
        cleanup.append(_step("delete-branch", "passed", branch=branch))
        result = "passed"
        return {
            "status": "passed",
            "dept_id": dept_id,
            "workspace": workspace,
            "repo": repo,
            "branch": branch,
            "commit": commit_hash,
            "pr_id": pr_id,
            "steps": steps,
            "cleanup": cleanup,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await _audit(
            request,
            dept_id,
            "live_smoke_bitbucket",
            result,
            {"workspace": workspace, "repo": repo, "branch": branch, "pr_id": pr_id},
        )


async def _confluence_space(client: httpx.AsyncClient, secret: Mapping[str, str], dept: Mapping[str, Any]) -> str:
    configured = dept.get("confluence_space_keys")
    if isinstance(configured, list) and configured:
        return str(configured[0])
    origin = _confluence_origin(str(secret.get("url") or ""))
    response = await client.get(
        f"{origin}/wiki/rest/api/space?limit=1",
        headers={"Authorization": _basic(_username(secret), _token(secret)), "Accept": "application/json"},
    )
    await _expect(response, step="confluence space lookup")
    results = response.json().get("results")
    if not isinstance(results, list) or not results:
        raise RuntimeError("no Confluence space is visible to this credential")
    return str(results[0]["key"])


@router.post("/{dept_id}/confluence")
async def run_confluence_smoke(dept_id: str, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    dept = _dept_or_404(dept_id)
    secret = await _credential(request, dept, "confluence")
    client = _http(request)
    origin = _confluence_origin(str(secret.get("url") or ""))
    headers = {
        "Authorization": _basic(_username(secret), _token(secret)),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    page_id = ""
    result = "failed"
    steps: list[dict[str, Any]] = []
    try:
        space_key = await _confluence_space(client, secret, dept)
        title = f"_AI_LIVE_SMOKE_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"
        create = await client.post(
            f"{origin}/wiki/rest/api/content",
            headers=headers,
            json={
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "body": {"storage": {"value": f"<p>{title} created</p>", "representation": "storage"}},
            },
        )
        await _expect(create, step="confluence create page")
        created = create.json()
        page_id = str(created.get("id") or "")
        steps.append(_step("create-page", "passed", page_id=page_id, space=space_key))

        update = await client.put(
            f"{origin}/wiki/rest/api/content/{quote(page_id, safe='')}",
            headers=headers,
            json={
                "id": page_id,
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "version": {"number": 2},
                "body": {"storage": {"value": f"<p>{title} updated</p>", "representation": "storage"}},
            },
        )
        await _expect(update, step="confluence update page")
        steps.append(_step("update-page", "passed", page_id=page_id, version=2))

        delete = await client.delete(f"{origin}/wiki/rest/api/content/{quote(page_id, safe='')}", headers=headers)
        if delete.status_code not in {200, 202, 204, 404}:
            raise RuntimeError(f"confluence cleanup failed: {_short_error(delete)}")
        result = "passed"
        return {
            "status": "passed",
            "dept_id": dept_id,
            "space_key": space_key,
            "page_id": page_id,
            "steps": steps,
            "cleanup": [_step("delete-page", "passed", page_id=page_id)],
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await _audit(request, dept_id, "live_smoke_confluence", result, {"page_id": page_id})


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


async def _jira_project(client: httpx.AsyncClient, base: str, headers: Mapping[str, str], dept: Mapping[str, Any]) -> str:
    configured = dept.get("jira_project_keys")
    if isinstance(configured, list) and configured:
        return str(configured[0])
    response = await client.get(f"{base}/rest/api/3/project/search?maxResults=50", headers=headers)
    await _expect(response, step="jira project lookup")
    values = response.json().get("values")
    if not isinstance(values, list) or not values:
        raise RuntimeError("no Jira project is visible to this credential")
    return str(values[0]["key"])


@router.post("/{dept_id}/jira")
async def run_jira_smoke(dept_id: str, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    dept = _dept_or_404(dept_id)
    secret = await _credential(request, dept, "jira")
    client = _http(request)
    base = str(secret.get("url") or "").rstrip("/")
    headers = {
        "Authorization": _basic(_username(secret), _token(secret)),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    issue_key = ""
    result = "failed"
    steps: list[dict[str, Any]] = []
    try:
        project_key = await _jira_project(client, base, headers, dept)
        summary = f"AI live smoke {datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        issue = None
        last_error = ""
        for issue_type in ("Task", "Görev", "Story", "Bug"):
            response = await client.post(
                f"{base}/rest/api/3/issue",
                headers=headers,
                json={
                    "fields": {
                        "project": {"key": project_key},
                        "summary": summary,
                        "description": _adf("Temporary admin dashboard live smoke issue."),
                        "issuetype": {"name": issue_type},
                    }
                },
            )
            if response.status_code < 400:
                issue = response.json()
                break
            last_error = _short_error(response)
        if issue is None:
            raise RuntimeError(f"jira create issue failed: {last_error}")
        issue_key = str(issue.get("key") or "")
        steps.append(_step("create-issue", "passed", issue_key=issue_key, project=project_key))

        comment = await client.post(
            f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/comment",
            headers=headers,
            json={"body": _adf("AI live smoke comment from admin dashboard.")},
        )
        await _expect(comment, step="jira comment")
        steps.append(_step("add-comment", "passed", comment_id=str(comment.json().get("id") or "")))

        attachment = await client.post(
            f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/attachments",
            headers={
                "Authorization": _basic(_username(secret), _token(secret)),
                "Accept": "application/json",
                "X-Atlassian-Token": "no-check",
            },
            files={"file": ("live-smoke-result.md", f"# {summary}\n\nstatus: passed\n", "text/markdown")},
        )
        await _expect(attachment, step="jira attachment")
        attached = attachment.json()
        steps.append(_step("upload-attachment", "passed", count=len(attached) if isinstance(attached, list) else 1))

        transitions = await client.get(
            f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
            headers=headers,
        )
        await _expect(transitions, step="jira transitions")
        options = transitions.json().get("transitions")
        transition = options[0] if isinstance(options, list) and options else None
        if transition:
            transition_id = str(transition.get("id"))
            transition_name = str(transition.get("name") or transition_id)
            response = await client.post(
                f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
                headers=headers,
                json={"transition": {"id": transition_id}},
            )
            await _expect(response, step="jira transition")
            steps.append(_step("transition-status", "passed", transition=transition_name))
        else:
            steps.append(_step("transition-status", "skipped", reason="no transition available"))

        delete = await client.delete(f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}", headers=headers)
        if delete.status_code not in {200, 202, 204, 404}:
            raise RuntimeError(f"jira cleanup failed: {_short_error(delete)}")
        result = "passed"
        return {
            "status": "passed",
            "dept_id": dept_id,
            "project_key": project_key,
            "issue_key": issue_key,
            "steps": steps,
            "cleanup": [_step("delete-issue", "passed", issue_key=issue_key)],
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await _audit(request, dept_id, "live_smoke_jira", result, {"issue_key": issue_key})
