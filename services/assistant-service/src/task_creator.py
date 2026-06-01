"""Task Creator API for Streamlit-to-Jira issue creation."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import Settings
from .session_credentials import SessionCredentialDeps

router = APIRouter(prefix="/api/tasks", tags=["task-creator"])

_LOG = logging.getLogger(__name__)
_VALID_PROJECT_KEY_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_VALID_OUTPUT_TYPES = {
    "jira_comment",
    "jira_attachment",
    "bitbucket_commit",
    "bitbucket_create_pr",
    "confluence_create_page",
    "confluence_update_page",
    "jira_transition",
}
_VALID_CLEANUP_POLICIES = {"on_success", "always", "never"}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_project_key(value: str) -> str:
    project_key = value.strip().upper()
    if not project_key:
        raise HTTPException(status_code=400, detail="project_key is required")
    if len(project_key) > 32 or any(ch not in _VALID_PROJECT_KEY_CHARS for ch in project_key):
        raise HTTPException(status_code=400, detail="project_key is invalid")
    return project_key


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    return default


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 60 <= parsed <= 7200 else None


def _coerce_output_actions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return [{"type": "jira_comment", "params": {}}]

    actions: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, str):
            kind = raw.strip()
            params: dict[str, Any] = {}
        elif isinstance(raw, Mapping):
            kind = _text(raw.get("type") or raw.get("kind"))
            raw_params = raw.get("params", raw.get("payload", {}))
            params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        else:
            continue
        if kind not in _VALID_OUTPUT_TYPES:
            continue
        actions.append({"type": kind, "params": params})

    if not any(action["type"] == "jira_comment" for action in actions):
        actions.insert(0, {"type": "jira_comment", "params": {}})
    return actions


def _session_deps(request: Request) -> SessionCredentialDeps:
    deps = getattr(request.app.state, "session_creds", None)
    if not isinstance(deps, SessionCredentialDeps):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "session_credentials_not_wired"},
        )
    return deps


def _read_credential(request: Request) -> Mapping[str, str]:
    credential_ref = request.headers.get("X-Credential-Ref", "").strip()
    if not credential_ref:
        raise HTTPException(
            status_code=400,
            detail="X-Credential-Ref header with a Jira credential is required",
        )
    try:
        from vault_client import VaultPath

        path = VaultPath.parse(credential_ref)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid credential ref") from exc

    try:
        return _session_deps(request).vault.read(path)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="credential ref not found") from exc
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("task_creator credential read failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="credential read failed") from exc


def _mcp_headers(
    request: Request,
    credential: Mapping[str, str],
) -> dict[str, str]:
    url = credential.get("url", "")
    username = credential.get("username") or credential.get("email") or ""
    personal_token = credential.get("personal_token") or credential.get("token") or ""
    api_token = credential.get("api_token") or ""
    if not url or not username or not (personal_token or api_token):
        raise HTTPException(status_code=400, detail="stored Jira credential is incomplete")

    headers = {
        "X-Client-Source": "assistant-service",
        "X-Atlassian-Jira-Url": url,
        "X-Atlassian-Jira-Username": username,
    }
    trace_id = request.headers.get("X-Trace-Id")
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    if api_token:
        headers["X-Atlassian-Jira-Api-Token"] = api_token
    elif personal_token and (".atlassian.net" in url.lower() or "@" in username):
        headers["X-Atlassian-Jira-Api-Token"] = personal_token
    else:
        headers["X-Atlassian-Jira-Personal-Token"] = personal_token
    return headers


def _build_description(body: Mapping[str, Any], summary: str) -> str:
    cleanup = _text(body.get("cleanup")) or _text(body.get("cleanup_policy"))
    if cleanup not in _VALID_CLEANUP_POLICIES:
        cleanup = ""
    timeout_seconds = _int_or_none(body.get("timeout_seconds"))
    metadata = {
        "workflow_type": _text(body.get("workflow_type")) or "research_basic",
        "repo": _text(body.get("repo")),
        "branch": _text(body.get("branch")),
        "needs_ssh": _bool(body.get("needs_ssh")),
        "needs_docker": _bool(body.get("needs_docker")),
        "test_command": _text(body.get("test_command"))
        or _text(body.get("execution_command"))
        or _text(body.get("command")),
        "cleanup": cleanup,
        "timeout_seconds": timeout_seconds,
        "web_search": _bool(body.get("web_search")),
        "smart_defaults": bool(body.get("smart_defaults", True)),
        "output": _coerce_output_actions(
            body.get("output", body.get("output_actions"))
        ),
    }
    redirect_context = body.get("redirect_context")
    if isinstance(redirect_context, Mapping):
        metadata["redirect_context"] = dict(redirect_context)

    lines = ["---", "ai-bot:"]
    for key, value in metadata.items():
        if value in ("", None, [], {}):
            continue
        lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", summary])
    return "\n".join(lines)


def _extract_issue(result: Mapping[str, Any]) -> tuple[str | None, str | None]:
    payload = result.get("result")
    if isinstance(payload, Mapping):
        content = payload.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, Mapping):
                text = first.get("text")
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = {}
                    if isinstance(parsed, Mapping):
                        issue = parsed.get("issue")
                        if isinstance(issue, Mapping):
                            key = _text(issue.get("key"))
                            url = _text(issue.get("url")) or _text(issue.get("self"))
                            return (key or None, url or None)
        structured = payload.get("structuredContent")
        if isinstance(structured, Mapping):
            issue = structured.get("issue")
            if isinstance(issue, Mapping):
                return (_text(issue.get("key")) or None, _text(issue.get("url")) or None)
    return (None, None)


@router.post("/create")
async def create_task(request: Request) -> JSONResponse:
    """Create a Jira issue through the stateless Atlassian MCP server."""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, Mapping):
        raise HTTPException(status_code=400, detail="JSON object body required")

    project_key = _validate_project_key(_text(body.get("project_key")))
    summary = _text(body.get("summary")) or _text(body.get("title"))
    if not summary:
        raise HTTPException(status_code=400, detail="summary or title is required")
    description = _build_description(body, summary)
    issue_type = _text(body.get("issue_type")) or "Task"
    assignee = _text(body.get("assignee")) or _text(body.get("assignee_account_id"))
    if not bool(body.get("auto_assign", True)):
        assignee = ""

    credential = _read_credential(request)
    mcp_headers = _mcp_headers(request, credential)
    settings = Settings()
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": "task-create",
        "method": "tools/call",
        "params": {
            "name": "jira_create_issue",
            "arguments": {
                "project_key": project_key,
                "summary": summary,
                "issue_type": issue_type,
                "description": description,
            },
        },
    }
    if assignee:
        payload["params"]["arguments"]["assignee"] = assignee

    try:
        async with httpx.AsyncClient(base_url=settings.mcp_base_url, timeout=30.0) as client:
            response = await client.post("/mcp", json=payload, headers=mcp_headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"MCP request failed: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={"mcp_status": response.status_code, "body": response.text[:500]},
        )

    try:
        result = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="MCP returned invalid JSON") from exc
    if isinstance(result, Mapping) and result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])

    issue_key, issue_url = _extract_issue(result if isinstance(result, Mapping) else {})
    return JSONResponse(
        status_code=201,
        content={
            "workflow_id": f"jira:{issue_key}" if issue_key else "jira:created",
            "issue_key": issue_key,
            "jira_issue_url": issue_url,
            "mcp_result": result,
        },
    )
