"""
Strict live smoke suite for the production-like platform path.

These tests are intentionally opt-in because they touch real Jira, Vault-backed
credential refs, live services, and the configured SSH runner. Enable with:

    RUN_STRICT_LIVE_E2E=1 python -m pytest platform/tests/e2e/test_41_live_platform_smoke.py

Required environment depends on the scenario:

* LIVE_DEPT_ID
* LIVE_JIRA_PROJECT_KEY
* LIVE_JIRA_CREDENTIAL_REF
* LIVE_JIRA_BOT_ACCOUNT_ID and LIVE_JIRA_WEBHOOK_SECRET for assignment trigger
* RUN_STRICT_LIVE_MUTATING_E2E=1 for Jira/Confluence/Bitbucket writes
* LIVE_CONFLUENCE_SPACE_KEY for Confluence publish verification
* LIVE_BITBUCKET_E2E_SOURCE_BRANCH and LIVE_BITBUCKET_E2E_PR_TARGET_BRANCH
  for Bitbucket commit/PR verification

Optional endpoint overrides:

* ADMIN_DASHBOARD_API_URL, AUTOMATION_SERVICE_URL, ASSISTANT_SERVICE_URL, MCP_BASE_URL
* LIVE_ADMIN_BEARER, LIVE_SSH_BASE_PATH
* LIVE_BITBUCKET_PROJECT_KEY, LIVE_BITBUCKET_REPO_SLUG, LIVE_KEEP_E2E_JIRA
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STRICT_LIVE_E2E") != "1",
    reason="strict live E2E is opt-in via RUN_STRICT_LIVE_E2E=1",
)


ADMIN_API_URL = os.getenv("ADMIN_DASHBOARD_API_URL", "http://localhost:8082")
AUTOMATION_URL = os.getenv("AUTOMATION_SERVICE_URL", "http://localhost:8084")
ASSISTANT_URL = os.getenv("ASSISTANT_SERVICE_URL", "http://localhost:8081")
MCP_URL = os.getenv("MCP_BASE_URL", "http://localhost:8090")
ADMIN_BEARER = os.getenv("LIVE_ADMIN_BEARER", "dev-admin-token")
REQUEST_TIMEOUT = 30.0


def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{name} is required for strict live E2E")
    return value


def _admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_BEARER}"}


def _require_ok(response: httpx.Response, label: str) -> dict[str, Any]:
    assert response.status_code < 400, (
        f"{label} failed with HTTP {response.status_code}: {response.text[:500]}"
    )
    try:
        data = response.json()
    except ValueError:
        data = {"text": response.text}
    assert not (isinstance(data, dict) and data.get("error")), data
    return data if isinstance(data, dict) else {"result": data}


def _jira_client(credentials: Any) -> httpx.Client:
    return httpx.Client(
        base_url=credentials.jira_url.rstrip("/"),
        auth=(credentials.jira_username, credentials.jira_api_token),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _resolve_jira_issue_type(credentials: Any, project_key: str) -> str:
    preferred = (
        os.getenv("LIVE_JIRA_ISSUE_TYPE", "").strip(),
        "Task",
        "Görev",
        "Story",
        "Hikaye",
        "Bug",
        "Hata",
    )
    with _jira_client(credentials) as client:
        response = client.get(
            "/rest/api/3/issue/createmeta",
            params={"projectKeys": project_key, "expand": "projects.issuetypes"},
        )
    data = _require_ok(response, "jira issue createmeta")
    projects = data.get("projects", [])
    issue_types = projects[0].get("issuetypes", []) if projects else []
    names = [str(item.get("name") or "") for item in issue_types]
    for candidate in preferred:
        if candidate and candidate in names:
            return candidate
    assert names, data
    return names[0]


def _create_jira_issue(
    credentials: Any,
    *,
    project_key: str,
    assignee: str,
    summary: str | None = None,
    description: str | None = None,
) -> str:
    marker = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if description is None:
        description = (
            "---\n"
            "ai-bot:\n"
            '  workflow_type: "research_summary_jira"\n'
            "---\n\n"
            f"Strict live assignment trigger smoke {marker}."
        )
    issue_type = _resolve_jira_issue_type(credentials, project_key)
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary or f"[Strict-Live-E2E] Assignment trigger {marker}",
            "issuetype": {"name": issue_type},
            "description": _adf(description),
            "assignee": {"accountId": assignee},
        }
    }
    with _jira_client(credentials) as client:
        response = client.post("/rest/api/3/issue", json=payload)
    data = _require_ok(response, "jira issue create")
    key = str(data.get("key") or "")
    assert key, data
    return key


def _delete_jira_issue(credentials: Any, issue_key: str) -> None:
    with _jira_client(credentials) as client:
        try:
            client.delete(f"/rest/api/3/issue/{issue_key}", timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError:
            return


def _signed_jira_webhook_body(
    *,
    issue_key: str,
    project_key: str,
    bot_account_id: str,
    secret: str,
) -> tuple[bytes, str]:
    body = {
        "webhookEvent": "jira:issue_assigned",
        "issue": {
            "key": issue_key,
            "fields": {
                "project": {"key": project_key},
                "assignee": {"accountId": bot_account_id},
            },
        },
        "user": {"accountId": "strict-live-e2e"},
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


def _wait_for_admin_workflow(
    workflow_id: str,
    *,
    dept_id: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    with httpx.Client(base_url=ADMIN_API_URL, timeout=REQUEST_TIMEOUT) as client:
        while time.monotonic() < deadline:
            params: dict[str, Any] = {"page_size": 50}
            if dept_id:
                params["dept_id"] = dept_id
            response = client.get(
                "/api/v1/workflows",
                params=params,
                headers=_admin_headers(),
            )
            last_payload = _require_ok(response, "admin workflow list")
            for item in last_payload.get("items", []):
                if item.get("workflow_id") == workflow_id:
                    return item
            time.sleep(3)
    raise AssertionError(
        f"workflow {workflow_id} was not visible in admin list: {last_payload}"
    )


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_extract_text(item) for item in value)
    if isinstance(value, dict):
        parts: list[str] = []
        if isinstance(value.get("text"), str):
            parts.append(value["text"])
        for key in ("content", "body", "value"):
            if key in value:
                parts.append(_extract_text(value[key]))
        return "".join(parts)
    return ""


def _jira_comment_texts(credentials: Any, issue_key: str) -> list[str]:
    with _jira_client(credentials) as client:
        response = client.get(
            f"/rest/api/3/issue/{issue_key}/comment",
            params={"maxResults": 100, "orderBy": "created"},
        )
    data = _require_ok(response, "jira comments")
    return [_extract_text(item.get("body")) for item in data.get("comments", [])]


def _jira_attachment_names(credentials: Any, issue_key: str) -> list[str]:
    with _jira_client(credentials) as client:
        response = client.get(
            f"/rest/api/3/issue/{issue_key}",
            params={"fields": "attachment"},
        )
    data = _require_ok(response, "jira issue attachments")
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
    return [
        str(item.get("filename") or "")
        for item in fields.get("attachment", [])
        if isinstance(item, dict)
    ]


def _confluence_page_id(credentials: Any, *, space_key: str, title: str) -> str | None:
    with httpx.Client(
        base_url=credentials.confluence_url.rstrip("/"),
        auth=(credentials.confluence_username, credentials.confluence_api_token),
        headers={"Accept": "application/json"},
        timeout=REQUEST_TIMEOUT,
    ) as client:
        response = client.get(
            "/rest/api/content/search",
            params={
                "cql": f'type=page and space="{space_key}" and title="{title}"',
                "limit": 1,
            },
        )
        if response.status_code == 404 and "/wiki" not in credentials.confluence_url:
            response = client.get(
                "/wiki/rest/api/content/search",
                params={
                    "cql": f'type=page and space="{space_key}" and title="{title}"',
                    "limit": 1,
                },
            )
    data = _require_ok(response, "confluence page search")
    results = data.get("results", [])
    if results and isinstance(results[0], dict):
        return str(results[0].get("id") or "") or None
    return None


def _bitbucket_file_contains(
    credentials: Any,
    *,
    workspace: str,
    repo_slug: str,
    branch: str,
    file_path: str,
    marker: str,
) -> bool:
    with httpx.Client(
        base_url="https://api.bitbucket.org/2.0",
        auth=(credentials.bitbucket_username, credentials.bitbucket_token_basic),
        timeout=REQUEST_TIMEOUT,
    ) as client:
        encoded_branch = urllib.parse.quote(branch, safe="")
        branch_response = client.get(
            f"/repositories/{workspace}/{repo_slug}/commits/{encoded_branch}",
            params={"pagelen": 1},
        )
        if branch_response.status_code == 404:
            return False
        branch_data = _require_ok(branch_response, "bitbucket branch head read")
        values = branch_data.get("values") or []
        commit_hash = (
            str(values[0].get("hash") or "")
            if values and isinstance(values[0], dict)
            else ""
        )
        if not commit_hash:
            return False

        response = client.get(
            f"/repositories/{workspace}/{repo_slug}/src/{commit_hash}/{file_path}",
        )
    if response.status_code == 404:
        return False
    _require_ok(response, "bitbucket file read")
    return marker in response.text


def _bitbucket_pr_exists(
    credentials: Any,
    *,
    workspace: str,
    repo_slug: str,
    source_branch: str,
) -> bool:
    query = f'source.branch.name="{source_branch}"'
    with httpx.Client(
        base_url="https://api.bitbucket.org/2.0",
        auth=(credentials.bitbucket_username, credentials.bitbucket_token_basic),
        timeout=REQUEST_TIMEOUT,
    ) as client:
        response = client.get(
            f"/repositories/{workspace}/{repo_slug}/pullrequests",
            params={"state": "OPEN", "q": query},
        )
    if response.status_code == 404:
        return False
    data = _require_ok(response, "bitbucket pullrequest search")
    return bool(data.get("values"))


def _ssh_exec(client: Any, command: str, *, timeout: int = 180) -> tuple[int, str, str]:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    return exit_code, out, err


def test_services_are_live() -> None:
    endpoints = {
        "admin-dashboard-api": f"{ADMIN_API_URL}/healthz",
        "automation-service": f"{AUTOMATION_URL}/healthz",
        "assistant-service": f"{ASSISTANT_URL}/healthz",
        "atlassian-mcp": f"{MCP_URL}/healthz",
    }
    for label, url in endpoints.items():
        response = httpx.get(url, timeout=10.0)
        assert response.status_code == 200, (
            f"{label} not healthy at {url}: {response.status_code} {response.text[:300]}"
        )


def test_admin_auth_monitoring_and_control_surfaces_are_live() -> None:
    unauth = httpx.get(
        f"{ADMIN_API_URL}/admin/healthcheck/aggregate",
        timeout=REQUEST_TIMEOUT,
    )
    assert unauth.status_code in {401, 403}, unauth.text[:300]

    with httpx.Client(
        base_url=ADMIN_API_URL,
        headers=_admin_headers(),
        timeout=REQUEST_TIMEOUT,
    ) as client:
        health = _require_ok(
            client.get("/admin/healthcheck/aggregate"),
            "admin aggregate healthcheck",
        )
        assert "services" in health, health

        services = _require_ok(client.get("/admin/services"), "admin services")
        assert isinstance(services.get("result", services), (list, dict)), services

        control = client.post(
            f"/api/v1/workflows/strict-live-missing-{uuid.uuid4().hex}/cancel"
        )
        assert control.status_code in {404, 502, 503}, control.text[:300]


def test_assistant_mcp_proxy_uses_vault_credential_ref() -> None:
    project_key = _env("LIVE_JIRA_PROJECT_KEY")
    credential_ref = _env("LIVE_JIRA_CREDENTIAL_REF")
    headers = {
        "X-Credential-Ref-Jira": credential_ref,
        "X-Client-Source": "strict-live-e2e",
    }
    with httpx.Client(base_url=ASSISTANT_URL, timeout=REQUEST_TIMEOUT) as client:
        tools = _require_ok(client.get("/api/mcp/tools", headers=headers), "mcp tools")
        assert tools.get("tools"), "assistant MCP proxy returned no tools"
        result = _require_ok(
            client.post(
                "/api/mcp/tools/call",
                json={
                    "tool_name": "jira_search_issues",
                    "arguments": {
                        "jql": f"project = {project_key} ORDER BY created DESC",
                        "max_results": 1,
                    },
                },
                headers=headers,
            ),
            "assistant MCP proxy Jira search",
        )
    assert result, "Jira search returned an empty response"


def test_admin_capability_probes_for_ssh_and_docker_are_healthy() -> None:
    dept_id = _env("LIVE_DEPT_ID")
    with httpx.Client(base_url=ADMIN_API_URL, timeout=60.0) as client:
        for service in ("ssh", "docker"):
            data = _require_ok(
                client.post(
                    f"/api/v1/departments/{dept_id}/probe/{service}",
                    headers=_admin_headers(),
                ),
                f"{service} capability probe",
            )
            assert data.get("status") == "healthy", data


def test_real_ssh_runner_can_build_run_and_cleanup_docker(credentials: Any) -> None:
    paramiko = pytest.importorskip("paramiko")
    base_path = os.getenv("LIVE_SSH_BASE_PATH", "/tmp/ai-platform-live-smoke")
    run_id = f"strict-live-{uuid.uuid4().hex[:10]}"
    workdir = f"{base_path.rstrip('/')}/{run_id}"
    image = f"ai-platform-live-smoke:{run_id}"

    key_path = os.path.expanduser(credentials.ssh_key_path)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        credentials.ssh_host,
        username=credentials.ssh_user,
        key_filename=key_path,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
    )
    try:
        quoted_dir = shlex.quote(workdir)
        quoted_image = shlex.quote(image)
        script = f"""
set -eu
mkdir -p {quoted_dir}
cat > {quoted_dir}/Dockerfile <<'DOCKERFILE'
FROM alpine:3.20
CMD ["sh", "-c", "echo live-smoke-ok"]
DOCKERFILE
docker build -q -t {quoted_image} {quoted_dir}
docker run --rm {quoted_image}
"""
        code, out, err = _ssh_exec(client, script, timeout=180)
        assert code == 0, f"remote docker smoke failed: stdout={out} stderr={err}"
        assert "live-smoke-ok" in out
    finally:
        cleanup = (
            f"docker rmi -f {shlex.quote(image)} >/dev/null 2>&1 || true; "
            f"rm -rf {shlex.quote(workdir)} >/dev/null 2>&1 || true"
        )
        try:
            _ssh_exec(client, cleanup, timeout=60)
        finally:
            client.close()


def test_jira_assignment_to_bot_triggers_automation(credentials: Any) -> None:
    dept_id = _env("LIVE_DEPT_ID")
    project_key = _env("LIVE_JIRA_PROJECT_KEY")
    bot_account_id = _env("LIVE_JIRA_BOT_ACCOUNT_ID")
    secret = _env("LIVE_JIRA_WEBHOOK_SECRET")
    issue_key = _create_jira_issue(
        credentials,
        project_key=project_key,
        assignee=bot_account_id,
    )
    try:
        raw, signature = _signed_jira_webhook_body(
            issue_key=issue_key,
            project_key=project_key,
            bot_account_id=bot_account_id,
            secret=secret,
        )
        response = httpx.post(
            f"{AUTOMATION_URL}/webhooks/jira/issue_assigned",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": signature,
                "X-Atlassian-Webhook-Identifier": f"strict-live-{uuid.uuid4().hex}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = _require_ok(response, "jira issue_assigned webhook")
        assert response.status_code == 202
        assert data.get("status") in {
            "accepted",
            "started",
            "workflow_started",
            "workflow_already_started",
        }, data
        workflow_id = str(data.get("workflow_id") or "")
        assert workflow_id == f"automation-jira-{issue_key}", data
        seen = _wait_for_admin_workflow(
            workflow_id,
            dept_id=dept_id,
            timeout_seconds=120,
        )
        assert seen.get("workflow_id") == workflow_id
    finally:
        _delete_jira_issue(credentials, issue_key)


@pytest.mark.skipif(
    os.getenv("RUN_STRICT_LIVE_MUTATING_E2E") != "1",
    reason=(
        "real Jira/Confluence/Bitbucket writes are opt-in via "
        "RUN_STRICT_LIVE_MUTATING_E2E=1"
    ),
)
def test_live_workflow_publishes_jira_confluence_bitbucket_results(
    credentials: Any,
) -> None:
    dept_id = _env("LIVE_DEPT_ID")
    project_key = _env("LIVE_JIRA_PROJECT_KEY")
    bot_account_id = _env("LIVE_JIRA_BOT_ACCOUNT_ID")
    secret = _env("LIVE_JIRA_WEBHOOK_SECRET")
    confluence_space = _env("LIVE_CONFLUENCE_SPACE_KEY")
    bb_source_branch = _env("LIVE_BITBUCKET_E2E_SOURCE_BRANCH")
    bb_target_branch = _env("LIVE_BITBUCKET_E2E_PR_TARGET_BRANCH")
    bb_project_key = os.getenv(
        "LIVE_BITBUCKET_PROJECT_KEY",
        credentials.bitbucket_workspace,
    ).strip()
    bb_repo_slug = os.getenv(
        "LIVE_BITBUCKET_REPO_SLUG",
        credentials.bitbucket_repo,
    ).strip()
    assert bb_project_key and bb_repo_slug

    marker = f"strict-live-{uuid.uuid4().hex[:10]}"
    stdout_marker = f"runner-output-{marker}"
    jira_comment_marker = f"jira-comment-{marker}"
    attachment_name = f"{marker}-stdout.txt"
    confluence_title = f"Strict Live E2E {marker}"
    bitbucket_marker = f"bitbucket-file-{marker}"
    bitbucket_branch = f"ai-strict-live/{marker}"
    bitbucket_file_path = f"strict-live-e2e/{marker}.md"
    pr_title = f"Strict Live E2E PR {marker}"
    command = f"printf '%s\\n' {shlex.quote(stdout_marker)}"

    def q(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    description = (
        "---\n"
        "ai-bot:\n"
        "  workflow_type: remote_ssh_test_only\n"
        "  needs_ssh: true\n"
        "  needs_docker: false\n"
        f"  test_command: {q(command)}\n"
        "  cleanup: always\n"
        "  timeout_seconds: 300\n"
        "  output:\n"
        "    - type: jira_comment\n"
        "      params:\n"
        f"        body: {q(jira_comment_marker)}\n"
        "    - type: jira_attachment\n"
        "      params:\n"
        f"        file_name: {q(attachment_name)}\n"
        "    - type: confluence_create_page\n"
        "      params:\n"
        f"        space_key: {q(confluence_space)}\n"
        f"        title: {q(confluence_title)}\n"
        f"        content: {q('Confluence publish ' + marker)}\n"
        "    - type: bitbucket_commit\n"
        "      params:\n"
        f"        project_key: {q(bb_project_key)}\n"
        f"        repo_slug: {q(bb_repo_slug)}\n"
        f"        file_path: {q(bitbucket_file_path)}\n"
        f"        content: {q(bitbucket_marker)}\n"
        f"        message: {q('Strict live E2E commit ' + marker)}\n"
        f"        branch: {q(bitbucket_branch)}\n"
        f"        source_branch: {q(bb_source_branch)}\n"
        "    - type: bitbucket_pr\n"
        "      params:\n"
        f"        project_key: {q(bb_project_key)}\n"
        f"        repo_slug: {q(bb_repo_slug)}\n"
        f"        title: {q(pr_title)}\n"
        f"        from_branch: {q(bitbucket_branch)}\n"
        f"        to_branch: {q(bb_target_branch)}\n"
        f"        description: {q('Strict live E2E PR ' + marker)}\n"
        "---\n\n"
        "Verify SSH execution result publishing to Jira, Confluence, and Bitbucket."
    )

    issue_key = _create_jira_issue(
        credentials,
        project_key=project_key,
        assignee=bot_account_id,
        summary=f"[Strict-Live-E2E] publish outputs {marker}",
        description=description,
    )
    try:
        raw, signature = _signed_jira_webhook_body(
            issue_key=issue_key,
            project_key=project_key,
            bot_account_id=bot_account_id,
            secret=secret,
        )
        response = httpx.post(
            f"{AUTOMATION_URL}/webhooks/jira/issue_assigned",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": signature,
                "X-Atlassian-Webhook-Identifier": f"strict-live-{uuid.uuid4().hex}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = _require_ok(response, "jira issue_assigned webhook")
        workflow_id = str(data.get("workflow_id") or "")
        assert workflow_id == f"automation-jira-{issue_key}", data
        _wait_for_admin_workflow(workflow_id, dept_id=dept_id, timeout_seconds=120)

        deadline = time.monotonic() + 300
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            comments = _jira_comment_texts(credentials, issue_key)
            attachments = _jira_attachment_names(credentials, issue_key)
            confluence_page = _confluence_page_id(
                credentials,
                space_key=confluence_space,
                title=confluence_title,
            )
            bitbucket_file = _bitbucket_file_contains(
                credentials,
                workspace=bb_project_key,
                repo_slug=bb_repo_slug,
                branch=bitbucket_branch,
                file_path=bitbucket_file_path,
                marker=bitbucket_marker,
            )
            bitbucket_pr = _bitbucket_pr_exists(
                credentials,
                workspace=bb_project_key,
                repo_slug=bb_repo_slug,
                source_branch=bitbucket_branch,
            )
            last_state = {
                "comments": comments,
                "attachments": attachments,
                "confluence_page": confluence_page,
                "bitbucket_file": bitbucket_file,
                "bitbucket_pr": bitbucket_pr,
            }
            if (
                any(jira_comment_marker in text for text in comments)
                and attachment_name in attachments
                and confluence_page
                and bitbucket_file
                and bitbucket_pr
            ):
                return
            time.sleep(10)

        raise AssertionError(
            "live output publishing did not complete before timeout: "
            f"{last_state}"
        )
    finally:
        if os.getenv("LIVE_KEEP_E2E_JIRA") != "1":
            _delete_jira_issue(credentials, issue_key)


@pytest.mark.skipif(
    os.getenv("RUN_STRICT_LIVE_ROLLBACK_E2E") != "1",
    reason="real workflow cancel is opt-in via RUN_STRICT_LIVE_ROLLBACK_E2E=1",
)
def test_live_workflow_can_be_cancelled_from_admin_control_plane(
    credentials: Any,
) -> None:
    dept_id = _env("LIVE_DEPT_ID")
    project_key = _env("LIVE_JIRA_PROJECT_KEY")
    bot_account_id = _env("LIVE_JIRA_BOT_ACCOUNT_ID")
    secret = _env("LIVE_JIRA_WEBHOOK_SECRET")
    marker = f"strict-rollback-{uuid.uuid4().hex[:10]}"
    description = (
        "---\n"
        "ai-bot:\n"
        "  workflow_type: remote_ssh_test_only\n"
        "  needs_ssh: true\n"
        "  needs_docker: false\n"
        "  test_command: \"sleep 300\"\n"
        "  cleanup: always\n"
        "  timeout_seconds: 600\n"
        "  output:\n"
        "    - type: jira_comment\n"
        "      params:\n"
        f"        body: {json.dumps(marker)}\n"
        "---\n\n"
        "Long-running strict live workflow for admin cancel verification."
    )
    issue_key = _create_jira_issue(
        credentials,
        project_key=project_key,
        assignee=bot_account_id,
        summary=f"[Strict-Live-E2E] rollback cancel {marker}",
        description=description,
    )
    try:
        raw, signature = _signed_jira_webhook_body(
            issue_key=issue_key,
            project_key=project_key,
            bot_account_id=bot_account_id,
            secret=secret,
        )
        response = httpx.post(
            f"{AUTOMATION_URL}/webhooks/jira/issue_assigned",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature": signature,
                "X-Atlassian-Webhook-Identifier": f"strict-live-{uuid.uuid4().hex}",
            },
            timeout=REQUEST_TIMEOUT,
        )
        data = _require_ok(response, "jira issue_assigned webhook")
        workflow_id = str(data.get("workflow_id") or "")
        assert workflow_id == f"automation-jira-{issue_key}", data
        _wait_for_admin_workflow(workflow_id, dept_id=dept_id, timeout_seconds=120)

        cancel_response = httpx.post(
            f"{ADMIN_API_URL}/api/v1/workflows/{workflow_id}/cancel",
            headers=_admin_headers(),
            timeout=REQUEST_TIMEOUT,
        )
        cancel_data = _require_ok(cancel_response, "admin workflow cancel")
        assert cancel_data.get("status") in {"cancelled", "canceled"}, cancel_data
    finally:
        if os.getenv("LIVE_KEEP_E2E_JIRA") != "1":
            _delete_jira_issue(credentials, issue_key)


def _parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = {"type": "token", "payload": {"text": raw}}
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def test_assistant_llm_can_select_live_jira_tool() -> None:
    dept_id = os.getenv("LIVE_DEPT_ID", "strict-live-e2e")
    project_key = _env("LIVE_JIRA_PROJECT_KEY")
    credential_ref = _env("LIVE_JIRA_CREDENTIAL_REF")
    payload = {
        "user_message": (
            f"Jira project {project_key} icin en son 1 taski ara. "
            "Guncel veri gerekiyorsa sadece jira_search_issues tool cagrisi "
            "JSON'u uret."
        ),
        "history": [],
        "dept_id": dept_id,
        "session_id": f"strict-live-{uuid.uuid4().hex}",
        "capabilities": ["jira"],
    }
    response = httpx.post(
        f"{ASSISTANT_URL}/api/chat/stream",
        json=payload,
        headers={
            "X-Credential-Ref-Jira": credential_ref,
            "X-Client-Source": "strict-live-e2e",
        },
        timeout=90.0,
    )
    assert response.status_code == 200, response.text[:500]
    events = _parse_sse_events(response.text)
    assert events, "assistant returned no SSE events"
    assert not any(event.get("type") == "error" for event in events), events
    assert any(
        event.get("type") in {"tool_call", "tool_result"} for event in events
    ), events
