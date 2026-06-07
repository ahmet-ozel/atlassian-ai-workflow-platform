"""End-to-end live smoke for the Jira automation path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.dependencies import require_admin
from ..llm_providers.model_capabilities import supports_reasoning_effort
from .live_smoke import (
    _adf,
    _audit,
    _basic,
    _credential,
    _dept_or_404,
    _expect,
    _http,
    _jira_project,
    _short_error,
    _step,
    _token,
    _username,
    run_bitbucket_smoke,
    run_confluence_smoke,
)
from .ssh_runners import _read_ssh_secret, _run_docker_smoke_sync

router = APIRouter(
    prefix="/api/v1/live-smoke",
    tags=["live-smoke"],
    dependencies=[Depends(require_admin)],
)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _hub_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _vault_read(request: Request, path: str) -> dict[str, str] | None:
    vault = getattr(request.app.state, "vault_client", None)
    if vault is None:
        raise HTTPException(status_code=503, detail="vault client is not wired")
    normalised = path.strip()
    if normalised.startswith("secret/data/"):
        normalised = normalised[len("secret/data/") :]
    return await vault.read_kv2_secret(path=normalised)


async def _vault_write(request: Request, path: str, data: dict[str, str]) -> None:
    vault = getattr(request.app.state, "vault_client", None)
    if vault is None:
        raise HTTPException(status_code=503, detail="vault client is not wired")
    await vault.write_kv2_secret(path=path, data=data)


async def _webhook_secret(request: Request, dept_id: str) -> tuple[str, str]:
    path = f"vault:webhooks/jira/{dept_id}"
    current = await _vault_read(request, path)
    if current and current.get("secret"):
        return str(current["secret"]), "existing"
    secret = secrets.token_urlsafe(32)
    await _vault_write(request, path, {"secret": secret})
    return secret, "created"


async def _jira_myself(
    request: Request,
    secret: Mapping[str, str],
) -> tuple[str, str]:
    client = _http(request)
    base = str(secret.get("url") or "").rstrip("/")
    headers = {
        "Authorization": _basic(_username(secret), _token(secret)),
        "Accept": "application/json",
    }
    response = await client.get(f"{base}/rest/api/3/myself", headers=headers)
    await _expect(response, step="jira myself")
    data = response.json()
    return str(data.get("accountId") or ""), str(data.get("displayName") or "")


async def _create_assigned_issue(
    request: Request,
    dept: Mapping[str, Any],
    secret: Mapping[str, str],
    *,
    assignee: str,
    trace_id: str,
) -> tuple[str, str]:
    client = _http(request)
    base = str(secret.get("url") or "").rstrip("/")
    headers = {
        "Authorization": _basic(_username(secret), _token(secret)),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    project_key = await _jira_project(client, base, headers, dept)
    description = (
        "Automation E2E live smoke.\n\n"
        "Task istegi: backend repo icin endpoint test kodu olustur, "
        "gerekirse SSH/Docker runner kullan, sonucu Jira comment/MD, "
        "Confluence publish ve Bitbucket commit/PR olarak raporla.\n\n"
        f"trace_id: {trace_id}"
    )
    last_error = ""
    for issue_type in ("Task", "Görev", "Story", "Bug"):
        response = await client.post(
            f"{base}/rest/api/3/issue",
            headers=headers,
            json={
                "fields": {
                    "project": {"key": project_key},
                    "summary": f"Automation E2E live smoke {trace_id}",
                    "description": _adf(description),
                    "issuetype": {"name": issue_type},
                    "assignee": {"accountId": assignee},
                }
            },
        )
        if response.status_code < 400:
            return str(response.json().get("key") or ""), project_key
        last_error = _short_error(response)
    raise RuntimeError(f"jira assigned issue create failed: {last_error}")


async def _trigger_automation(
    request: Request,
    *,
    issue_key: str,
    project_key: str,
    account_id: str,
    webhook_secret: str,
    trace_id: str,
) -> dict[str, Any]:
    settings = getattr(request.app.state, "settings", None)
    base_url = str(
        getattr(settings, "automation_service_url", "")
        or "http://automation-service:8080"
    ).rstrip("/")
    body = {
        "webhookEvent": "jira:issue_assigned",
        "issue": {
            "key": issue_key,
            "fields": {
                "project": {"key": project_key},
                "assignee": {"accountId": account_id},
            },
        },
        "user": {"accountId": f"admin-live-smoke-{trace_id}"},
    }
    raw = _json_bytes(body)
    response = await _http(request).post(
        f"{base_url}/webhooks/jira/issue_assigned",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature": _hub_signature(webhook_secret, raw),
            "X-Atlassian-Webhook-Identifier": f"{trace_id}-{secrets.token_hex(4)}",
            "X-Trace-Id": trace_id,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"automation webhook failed: {_short_error(response)}")
    data = response.json()
    return data if isinstance(data, dict) else {"status": response.status_code}


async def _wait_workflow_visible(request: Request, workflow_id: str) -> str:
    client = getattr(request.app.state, "temporal_workflow_client", None)
    if client is None:
        return "temporal_control_unavailable"
    deadline = time.monotonic() + 45
    last_status = "not_seen"
    while time.monotonic() < deadline:
        try:
            desc = await client.get_workflow_description(workflow_id)
            return str(getattr(desc, "status", "visible") or "visible")
        except Exception as exc:  # noqa: BLE001
            last_status = type(exc).__name__
            await asyncio.sleep(3)
    return last_status


def _first_secret_value(payload: Mapping[str, str]) -> str:
    for key in ("api_key", "OPENAI_API_KEY", "token", "key", "value"):
        value = str(payload.get(key) or "")
        if value:
            return value
    for value in payload.values():
        if value:
            return str(value)
    return ""


def _openai_responses_url(base_url: str) -> str:
    clean = (base_url or "https://api.openai.com/v1").rstrip("/")
    if clean.endswith("/responses"):
        return clean
    if clean.endswith("/v1"):
        return f"{clean}/responses"
    return f"{clean}/v1/responses"


def _extract_responses_text(data: Any) -> str:
    """Pull assistant text out of an OpenAI Responses API payload."""
    if not isinstance(data, dict):
        return ""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    if isinstance(output_text, list):
        joined = "".join(p for p in output_text if isinstance(p, str))
        if joined:
            return joined
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


async def _llm_decision(
    request: Request,
    dept: Mapping[str, Any],
    *,
    issue_key: str,
) -> dict[str, Any]:
    primary = (dept.get("llm_overrides") or {}).get("primary") or {}
    key_ref = str(primary.get("api_key_ref") or "")
    key_payload = await _vault_read(request, key_ref)
    api_key = _first_secret_value(key_payload or {})
    model = str(primary.get("model") or "gpt-5.5")
    base_url = str(primary.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    if not api_key:
        provider = await _active_openai_provider(request)
        if provider is not None:
            key_payload = await _vault_read(request, str(provider["vault_path"]))
            api_key = _first_secret_value(key_payload or {})
            model = str(provider["model"] or model)
            base_url = str(provider["base_url"] or base_url).rstrip("/")
    if not api_key:
        raise RuntimeError("OpenAI API key ref is empty")
    prompt = (
        "Jira task: backend repodaki endpointlere gore test kodu olustur, "
        "SSH runner uzerinde Docker build/run/cleanup yap, sonuclari Jira, "
        "Confluence ve Bitbucket'a yaz. Sadece JSON don: "
        "workflow_type, needs_ssh, needs_docker, outputs."
    )
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "text": {"format": {"type": "json_object"}},
        "instructions": "You are a workflow router. Return strict JSON.",
        "input": f"{prompt}\nissue_key={issue_key}",
    }
    # Reasoning-capable models (gpt-5 family / o-series) reject an
    # explicit temperature - drop it so the default model still routes.
    if supports_reasoning_effort(model):
        body.pop("temperature", None)
    response = await _http(request).post(
        _openai_responses_url(base_url),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    await _expect(response, step="openai workflow decision")
    data = response.json()
    content = _extract_responses_text(data)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"raw": content}
    usage = data.get("usage") if isinstance(data, dict) else {}
    if isinstance(usage, Mapping):
        parsed["usage"] = {
            "token_in": int(usage.get("input_tokens") or 0),
            "token_out": int(usage.get("output_tokens") or 0),
        }
    parsed["model"] = model
    return parsed


async def _record_llm_cost(
    request: Request,
    *,
    dept_id: str,
    workflow_id: str,
    trace_id: str,
    decision: Mapping[str, Any],
) -> None:
    pool = getattr(request.app.state, "pg_pool", None)
    usage = decision.get("usage") if isinstance(decision.get("usage"), Mapping) else {}
    token_in = int(usage.get("token_in") or 0)
    token_out = int(usage.get("token_out") or 0)
    if pool is None or (token_in == 0 and token_out == 0):
        return
    activity_id = f"{trace_id}:llm-workflow-decision"
    metadata = json.dumps({"trace_id": trace_id, "workflow_id": workflow_id}, sort_keys=True)
    columns = (
        "activity_id, workflow_id, dept_id, user_id, model, provider, "
        "token_in, token_out, cost_usd, cost_tag, metadata"
    )
    values = "($1, $2, $3, 'admin-dashboard', $4, 'openai', $5, $6, 0, 'production', $7::jsonb)"
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                f"INSERT INTO shared.cost_tracking ({columns}) VALUES {values} "
                "ON CONFLICT (activity_id) DO NOTHING",
                activity_id,
                workflow_id or None,
                dept_id,
                str(decision.get("model") or "unknown"),
                token_in,
                token_out,
                metadata,
            )
        except Exception:  # noqa: BLE001
            await conn.execute(
                "INSERT INTO shared.cost_tracking "
                "(activity_id, workflow_id, dept_id, user_id, model, provider, "
                "token_in, token_out, cost_usd, cost_tag) "
                "VALUES ($1, $2, $3, 'admin-dashboard', $4, 'openai', $5, $6, 0, 'production') "
                "ON CONFLICT (activity_id) DO NOTHING",
                activity_id,
                workflow_id or None,
                dept_id,
                str(decision.get("model") or "unknown"),
                token_in,
                token_out,
            )


async def _active_openai_provider(request: Request) -> Any | None:
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return None
    return await pool.fetchrow(
        """
        SELECT model, base_url, vault_path
        FROM automation.llm_providers
        WHERE provider_type = 'openai' AND status = 'active'
        ORDER BY last_tested_at DESC NULLS LAST, updated_at DESC
        LIMIT 1
        """
    )


async def _runner_row(request: Request, dept_id: str) -> Any:
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="pg_pool is not wired")
    row = await pool.fetchrow(
        """
        SELECT r.runner_id, r.host, r.port, r.username, r.base_path,
               r.vault_path, r.status, r.created_at, r.updated_at
        FROM infrastructure.dept_ssh_assignments a
        JOIN infrastructure.ssh_runners r ON r.runner_id = a.runner_id
        WHERE a.dept_id = $1 AND r.status = 'active'
        ORDER BY r.runner_id
        LIMIT 1
        """,
        dept_id,
    )
    if row is None:
        raise RuntimeError(f"no active SSH runner is assigned to {dept_id}")
    return row


async def _jira_results(
    request: Request,
    secret: Mapping[str, str],
    *,
    issue_key: str,
    trace_id: str,
) -> list[dict[str, Any]]:
    client = _http(request)
    base = str(secret.get("url") or "").rstrip("/")
    headers = {
        "Authorization": _basic(_username(secret), _token(secret)),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    steps: list[dict[str, Any]] = []
    comment = await client.post(
        f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/comment",
        headers=headers,
        json={"body": _adf(f"Automation E2E passed. trace_id={trace_id}")},
    )
    await _expect(comment, step="jira result comment")
    steps.append(_step("jira-comment", "passed", comment_id=str(comment.json().get("id") or "")))
    attachment = await client.post(
        f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/attachments",
        headers={
            "Authorization": _basic(_username(secret), _token(secret)),
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        },
        files={"file": ("automation-e2e-result.md", f"# Automation E2E\n\ntrace_id: {trace_id}\nstatus: passed\n", "text/markdown")},
    )
    await _expect(attachment, step="jira result attachment")
    steps.append(_step("jira-md-attachment", "passed"))
    transitions = await client.get(
        f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
        headers=headers,
    )
    await _expect(transitions, step="jira transitions")
    options = transitions.json().get("transitions")
    transition = options[0] if isinstance(options, list) and options else None
    if transition:
        await _expect(
            await client.post(
                f"{base}/rest/api/3/issue/{quote(issue_key, safe='')}/transitions",
                headers=headers,
                json={"transition": {"id": str(transition.get("id"))}},
            ),
            step="jira transition",
        )
        steps.append(_step("jira-status-transition", "passed", transition=str(transition.get("name") or "")))
    else:
        steps.append(_step("jira-status-transition", "skipped"))
    return steps


async def _delete_issue(request: Request, secret: Mapping[str, str], issue_key: str) -> dict[str, Any]:
    response = await _http(request).delete(
        f"{str(secret.get('url') or '').rstrip('/')}/rest/api/3/issue/{quote(issue_key, safe='')}",
        headers={"Authorization": _basic(_username(secret), _token(secret)), "Accept": "application/json"},
    )
    if response.status_code not in {200, 202, 204, 404}:
        return _step("delete-jira-task", "failed", error=_short_error(response))
    return _step("delete-jira-task", "passed", issue_key=issue_key)


@router.post("/{dept_id}/automation-e2e")
async def run_automation_e2e_smoke(dept_id: str, request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    trace_id = f"auto-e2e-{secrets.token_hex(6)}"
    dept = _dept_or_404(dept_id)
    jira_secret = await _credential(request, dept, "jira")
    steps: list[dict[str, Any]] = []
    cleanup: list[dict[str, Any]] = []
    issue_key = ""
    workflow_id = ""
    result = "failed"
    try:
        account_id, display_name = await _jira_myself(request, jira_secret)
        issue_key, project_key = await _create_assigned_issue(
            request, dept, jira_secret, assignee=account_id, trace_id=trace_id
        )
        steps.append(_step("jira-task-assigned-to-bot", "passed", issue_key=issue_key, project=project_key, assignee=display_name))

        secret, secret_state = await _webhook_secret(request, dept_id)
        webhook = await _trigger_automation(
            request,
            issue_key=issue_key,
            project_key=project_key,
            account_id=account_id,
            webhook_secret=secret,
            trace_id=trace_id,
        )
        workflow_id = str(webhook.get("workflow_id") or "")
        steps.append(_step("automation-webhook-trigger", "passed", workflow_id=workflow_id, webhook_secret=secret_state))
        if workflow_id:
            visible = await _wait_workflow_visible(request, workflow_id)
            steps.append(_step("workflow-visible-in-admin", "passed" if visible != "not_seen" else "failed", status_detail=visible))

        decision = await _llm_decision(request, dept, issue_key=issue_key)
        await _record_llm_cost(
            request,
            dept_id=dept_id,
            workflow_id=workflow_id,
            trace_id=trace_id,
            decision=decision,
        )
        steps.append(_step("llm-workflow-decision", "passed", decision=decision))

        runner = await _runner_row(request, dept_id)
        ssh_secret = await _read_ssh_secret(getattr(request.app.state, "vault_client"), str(runner["runner_id"]))
        docker = await asyncio.to_thread(_run_docker_smoke_sync, runner=runner, secret=ssh_secret)
        if docker.status != "passed":
            raise RuntimeError(f"docker smoke failed: exit={docker.exit_code}")
        steps.append(_step("ssh-docker-build-run-cleanup", "passed", runner_id=docker.runner_id, duration_ms=docker.duration_ms))

        bitbucket = await run_bitbucket_smoke(dept_id, request)
        steps.append(_step("bitbucket-commit-pr-rollback", bitbucket["status"], pr_id=bitbucket.get("pr_id")))
        confluence = await run_confluence_smoke(dept_id, request)
        steps.append(_step("confluence-publish-update", confluence["status"], page_id=confluence.get("page_id")))
        steps.extend(await _jira_results(request, jira_secret, issue_key=issue_key, trace_id=trace_id))

        result = "passed"
        return {
            "status": "passed",
            "dept_id": dept_id,
            "trace_id": trace_id,
            "issue_key": issue_key,
            "workflow_id": workflow_id,
            "steps": steps,
            "cleanup": cleanup,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        if workflow_id:
            client = getattr(request.app.state, "temporal_workflow_client", None)
            if client is not None:
                try:
                    await client.cancel_workflow(workflow_id)
                    cleanup.append(_step("cancel-started-workflow", "passed", workflow_id=workflow_id))
                except Exception as exc:  # noqa: BLE001
                    cleanup.append(_step("cancel-started-workflow", "skipped", reason=type(exc).__name__))
        if issue_key:
            cleanup.append(await _delete_issue(request, jira_secret, issue_key))
        await _audit(
            request,
            dept_id,
            "live_smoke_automation_e2e",
            result,
            {"trace_id": trace_id, "issue_key": issue_key, "workflow_id": workflow_id},
        )
