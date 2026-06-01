"""
Test 15: Webhook delivery — Jira and Bitbucket real event delivery.

Validates that Jira and Bitbucket webhooks can deliver real events to the
automation-service via a tunnel (ngrok or cloudflared) exposing localhost.

Flow:
1. Provision a tunnel (ngrok or cloudflared) exposing localhost:80
2. Subscribe a Jira webhook → create issue → assert delivery within 30s
3. Subscribe a Bitbucket webhook → push commit → assert delivery within 30s
4. Verify audit_events contains webhook.jira.received and webhook.bitbucket.received
5. Emit e2e-evidence/15-webhooks.json

NOTE: This test is SKIPPED if no tunnel tool (ngrok/cloudflared) is available
on the system. The test gracefully handles this case.

Uses:
- httpx for API calls
- subprocess for tunnel provisioning
- psycopg2 for querying audit_events table
- credentials fixture for Jira/Bitbucket credentials
- evidence_collector fixture for emitting evidence

Requirements: R15.1, R15.2, R15.3, R15.4, R15.5, R15.6
"""

import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "15-webhooks.json"
REQUEST_TIMEOUT = 30.0
WEBHOOK_DELIVERY_TIMEOUT = 30  # seconds to wait for webhook delivery
TUNNEL_STARTUP_TIMEOUT = 15  # seconds to wait for tunnel to be ready
LOCAL_WEBHOOK_PORT = 80  # port the tunnel exposes

# Jira webhook endpoint path on automation-service
JIRA_WEBHOOK_PATH = "/api/v1/webhooks/jira"
# Bitbucket webhook endpoint path on automation-service
BITBUCKET_WEBHOOK_PATH = "/api/v1/webhooks/bitbucket"

# Jira project key for test issue creation
JIRA_PROJECT_KEY = "JOH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_tunnel_tool() -> Optional[str]:
    """Detect which tunnel tool is available on the system.

    Returns:
        'ngrok', 'cloudflared', or None if neither is found.
    """
    if shutil.which("ngrok"):
        return "ngrok"
    if shutil.which("cloudflared"):
        return "cloudflared"
    return None


def _start_ngrok_tunnel(port: int) -> tuple[subprocess.Popen, Optional[str]]:
    """Start ngrok tunnel and return (process, public_url).

    Starts ngrok in background and queries its local API for the public URL.
    """
    proc = subprocess.Popen(
        ["ngrok", "http", str(port), "--log=stdout", "--log-format=json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for ngrok to start and query its API for the tunnel URL
    public_url = None
    deadline = time.time() + TUNNEL_STARTUP_TIMEOUT

    while time.time() < deadline:
        time.sleep(1)
        try:
            # ngrok exposes a local API at http://127.0.0.1:4040
            resp = httpx.get("http://127.0.0.1:4040/api/tunnels", timeout=5.0)
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    tunnel_url = tunnel.get("public_url", "")
                    if tunnel_url.startswith("https://"):
                        public_url = tunnel_url
                        break
                if public_url:
                    break
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
            continue

    return proc, public_url


def _start_cloudflared_tunnel(port: int) -> tuple[subprocess.Popen, Optional[str]]:
    """Start cloudflared quick tunnel and return (process, public_url).

    Uses `cloudflared tunnel --url` for a quick ephemeral tunnel.
    Parses stdout/stderr for the assigned URL.
    """
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    public_url = None
    deadline = time.time() + TUNNEL_STARTUP_TIMEOUT

    # cloudflared prints the URL to stderr
    url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")

    while time.time() < deadline:
        time.sleep(1)
        if proc.stderr:
            # Read available output without blocking
            try:
                import select
                # On Windows, use a non-blocking approach
                import msvcrt
                import os
                if hasattr(proc.stderr, 'fileno'):
                    # Try reading what's available
                    output = b""
                    try:
                        while True:
                            chunk = proc.stderr.read1(4096) if hasattr(proc.stderr, 'read1') else b""
                            if not chunk:
                                break
                            output += chunk
                    except (BlockingIOError, OSError):
                        pass

                    text = output.decode("utf-8", errors="replace")
                    match = url_pattern.search(text)
                    if match:
                        public_url = match.group(0)
                        break
            except (ImportError, AttributeError, OSError):
                pass

        # Alternative: check if process wrote anything
        if proc.poll() is not None:
            # Process ended, read all output
            _, stderr_output = proc.communicate(timeout=5)
            text = stderr_output.decode("utf-8", errors="replace")
            match = url_pattern.search(text)
            if match:
                public_url = match.group(0)
            break

    return proc, public_url


def _stop_tunnel(proc: subprocess.Popen) -> None:
    """Gracefully stop a tunnel process."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)


def _build_jira_client(credentials) -> httpx.Client:
    """Build an httpx client configured for Jira REST API with Basic Auth.

    NEVER logs raw API tokens.
    """
    auth = (credentials.jira_username, credentials.jira_api_token)
    base_url = credentials.jira_url.rstrip("/")

    return httpx.Client(
        base_url=base_url,
        auth=auth,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )


def _bb_headers(token: str) -> dict[str, str]:
    """Build authorization headers for Bitbucket API using Bearer token.

    NEVER logs the raw token value.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _query_audit_events(event_type: str) -> list[dict[str, Any]]:
    """Query automation.audit_events for rows matching event_type.

    Uses docker compose exec to run psql against the postgres container.

    Returns:
        List of matching rows as dicts, or empty list on failure.
    """
    query = (
        f"SELECT id, event_type, payload, created_at "
        f"FROM automation.audit_events "
        f"WHERE event_type = '{event_type}' "
        f"ORDER BY created_at DESC LIMIT 5;"
    )

    try:
        result = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "postgres", "-d", "automation",
                "--csv", "-c", query,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=str(_get_platform_dir()),
        )

        if result.returncode != 0:
            return []

        lines = result.stdout.strip().split("\n")
        if len(lines) < 2:
            return []

        headers = lines[0].split(",")
        rows = []
        for line in lines[1:]:
            if line.strip():
                values = line.split(",", len(headers) - 1)
                row = dict(zip(headers, values))
                rows.append(row)
        return rows

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return []


def _get_platform_dir():
    """Get the platform directory path."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebhookDelivery:
    """Webhook delivery tests — Jira and Bitbucket real event delivery.

    Tests provision a tunnel, subscribe webhooks, trigger events, and verify
    delivery. Skipped if no tunnel tool is available.

    Tests execute in order:
    1. Provision tunnel
    2. Subscribe Jira webhook → create issue → assert delivery
    3. Subscribe Bitbucket webhook → push commit → assert delivery
    4. Verify audit_events
    5. Emit evidence
    """

    # Shared state across test methods
    _tunnel_tool: Optional[str] = None
    _tunnel_proc: Optional[subprocess.Popen] = None
    _tunnel_url: Optional[str] = None
    _jira_webhook_id: Optional[str] = None
    _bitbucket_webhook_uuid: Optional[str] = None
    _jira_issue_key: Optional[str] = None
    _jira_delivery_received: bool = False
    _bitbucket_delivery_received: bool = False
    _scenario_results: list[dict[str, Any]] = []

    @pytest.fixture(autouse=True)
    def _check_tunnel_available(self):
        """Skip all tests in this class if no tunnel tool is available."""
        tool = _find_tunnel_tool()
        if tool is None:
            pytest.skip(
                "No tunnel tool available (ngrok or cloudflared). "
                "Install ngrok or cloudflared to run webhook delivery tests."
            )
        TestWebhookDelivery._tunnel_tool = tool

    def test_provision_tunnel(self, credentials, evidence_collector):
        """R15.1: Provision tunnel exposing localhost:80 as public HTTPS URL.

        WHEN the webhook test starts, THE Test_Framework SHALL provision a
        tunnel (ngrok or cloudflared) exposing localhost:80 as a public HTTPS
        URL and SHALL record the tunnel URL.
        """
        tool = TestWebhookDelivery._tunnel_tool
        assert tool is not None, "Tunnel tool should be detected by fixture"

        start = time.perf_counter()

        if tool == "ngrok":
            proc, public_url = _start_ngrok_tunnel(LOCAL_WEBHOOK_PORT)
        else:
            proc, public_url = _start_cloudflared_tunnel(LOCAL_WEBHOOK_PORT)

        latency_ms = (time.perf_counter() - start) * 1000

        TestWebhookDelivery._tunnel_proc = proc
        TestWebhookDelivery._tunnel_url = public_url

        result = {
            "scenario": "TUNNEL-PROVISION",
            "tunnel_tool": tool,
            "tunnel_url": public_url,
            "latency_ms": round(latency_ms, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if public_url is None:
            result["verdict"] = "fail"
            result["error"] = (
                f"Failed to provision {tool} tunnel within {TUNNEL_STARTUP_TIMEOUT}s. "
                f"Ensure {tool} is properly configured and authenticated."
            )
            TestWebhookDelivery._scenario_results.append(result)
            # Clean up the process
            if proc:
                _stop_tunnel(proc)
            pytest.fail(
                f"Tunnel provisioning failed: could not obtain public URL from {tool} "
                f"within {TUNNEL_STARTUP_TIMEOUT} seconds."
            )
        else:
            result["verdict"] = "pass"
            TestWebhookDelivery._scenario_results.append(result)

    def test_jira_webhook_delivery(self, credentials, evidence_collector):
        """R15.2: Subscribe Jira webhook, create issue, assert delivery within 30s.

        WHEN a Jira webhook is subscribed pointing to {tunnel_url}/api/v1/webhooks/jira,
        THE Test_Framework SHALL create a Jira issue and SHALL assert that
        automation-service receives the webhook within 30 seconds.
        """
        tunnel_url = TestWebhookDelivery._tunnel_url
        if tunnel_url is None:
            pytest.skip("Tunnel not provisioned — cannot test Jira webhook delivery")

        webhook_target_url = f"{tunnel_url}{JIRA_WEBHOOK_PATH}"
        start = time.perf_counter()

        # Step 1: Register Jira webhook
        client = _build_jira_client(credentials)
        webhook_payload = {
            "name": f"E2E-Test-Webhook-{int(time.time())}",
            "url": webhook_target_url,
            "events": ["jira:issue_created", "jira:issue_updated"],
            "filters": {
                "issue-related-events-section": f"project = {JIRA_PROJECT_KEY}"
            },
            "enabled": True,
        }

        try:
            register_resp = client.post(
                "/rest/webhooks/1.0/webhook",
                json=webhook_payload,
            )

            webhook_registered = register_resp.status_code in (200, 201)
            webhook_id = None
            if webhook_registered:
                webhook_data = register_resp.json()
                webhook_id = str(webhook_data.get("self", webhook_data.get("id", "")))
                TestWebhookDelivery._jira_webhook_id = webhook_id

            # Step 2: Create a Jira issue to trigger the webhook
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            issue_payload = {
                "fields": {
                    "project": {"key": JIRA_PROJECT_KEY},
                    "summary": f"[Webhook-E2E] Trigger issue {timestamp}",
                    "issuetype": {"name": "Task"},
                }
            }

            create_resp = client.post("/rest/api/3/issue", json=issue_payload)
            issue_created = create_resp.status_code in (200, 201)
            issue_key = None
            if issue_created:
                issue_key = create_resp.json().get("key")
                TestWebhookDelivery._jira_issue_key = issue_key

            # Step 3: Wait for webhook delivery (poll audit_events)
            delivery_received = False
            if webhook_registered and issue_created:
                poll_deadline = time.time() + WEBHOOK_DELIVERY_TIMEOUT
                while time.time() < poll_deadline:
                    rows = _query_audit_events("webhook.jira.received")
                    if rows:
                        delivery_received = True
                        break
                    time.sleep(2)

            TestWebhookDelivery._jira_delivery_received = delivery_received
            latency_ms = (time.perf_counter() - start) * 1000

        finally:
            client.close()

        result = {
            "scenario": "JIRA-WEBHOOK",
            "webhook_target_url": webhook_target_url,
            "webhook_registered": webhook_registered,
            "webhook_id": webhook_id,
            "issue_key": issue_key,
            "delivery_received": delivery_received,
            "latency_ms": round(latency_ms, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not webhook_registered:
            result["verdict"] = "fail"
            result["error"] = (
                f"Jira webhook registration failed: HTTP {register_resp.status_code}. "
                f"Response: {register_resp.text[:300]}"
            )
        elif not issue_created:
            result["verdict"] = "fail"
            result["error"] = (
                f"Jira issue creation failed: HTTP {create_resp.status_code}. "
                f"Response: {create_resp.text[:300]}"
            )
        elif not delivery_received:
            result["verdict"] = "fail"
            result["error"] = (
                f"Webhook delivery not received within {WEBHOOK_DELIVERY_TIMEOUT}s. "
                f"No 'webhook.jira.received' event found in audit_events."
            )
        else:
            result["verdict"] = "pass"

        TestWebhookDelivery._scenario_results.append(result)

        # Assert — allow soft failure with evidence
        if not delivery_received:
            pytest.fail(
                f"Jira webhook delivery not confirmed within {WEBHOOK_DELIVERY_TIMEOUT}s. "
                f"Webhook registered: {webhook_registered}, Issue created: {issue_created} ({issue_key})"
            )

    def test_bitbucket_webhook_delivery(self, credentials, evidence_collector):
        """R15.3: Subscribe Bitbucket webhook, push commit, assert delivery within 30s.

        WHEN a Bitbucket webhook is subscribed pointing to
        {tunnel_url}/api/v1/webhooks/bitbucket, THE Test_Framework SHALL push
        a commit and SHALL assert that automation-service receives the webhook
        within 30 seconds.
        """
        tunnel_url = TestWebhookDelivery._tunnel_url
        if tunnel_url is None:
            pytest.skip("Tunnel not provisioned — cannot test Bitbucket webhook delivery")

        webhook_target_url = f"{tunnel_url}{BITBUCKET_WEBHOOK_PATH}"
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer

        start = time.perf_counter()

        # Step 1: Register Bitbucket webhook
        webhook_url = (
            f"https://api.bitbucket.org/2.0/repositories/"
            f"{workspace}/{repo_slug}/hooks"
        )
        webhook_payload = {
            "description": f"E2E-Test-Webhook-{int(time.time())}",
            "url": webhook_target_url,
            "active": True,
            "events": ["repo:push"],
        }

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            register_resp = client.post(
                webhook_url,
                headers=_bb_headers(token),
                json=webhook_payload,
            )

        webhook_registered = register_resp.status_code in (200, 201)
        webhook_uuid = None
        if webhook_registered:
            webhook_data = register_resp.json()
            webhook_uuid = webhook_data.get("uuid", "")
            TestWebhookDelivery._bitbucket_webhook_uuid = webhook_uuid

        # Step 2: Push a commit to trigger the webhook
        epoch = int(time.time())
        commit_url = (
            f"https://api.bitbucket.org/2.0/repositories/"
            f"{workspace}/{repo_slug}/src"
        )
        file_content = (
            f"# Webhook E2E Test\n"
            f"Triggered at epoch: {epoch}\n"
            f"Purpose: Validate Bitbucket webhook delivery\n"
        )

        commit_headers = {"Authorization": f"Bearer {token}"}

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            commit_resp = client.post(
                commit_url,
                headers=commit_headers,
                data={
                    "message": f"e2e: webhook trigger commit at {epoch}",
                    "branch": "main",
                },
                files={
                    "e2e-webhook-trigger.md": (
                        "e2e-webhook-trigger.md",
                        file_content,
                        "text/plain",
                    ),
                },
            )

        commit_pushed = commit_resp.status_code in (200, 201)

        # Step 3: Wait for webhook delivery (poll audit_events)
        delivery_received = False
        if webhook_registered and commit_pushed:
            poll_deadline = time.time() + WEBHOOK_DELIVERY_TIMEOUT
            while time.time() < poll_deadline:
                rows = _query_audit_events("webhook.bitbucket.received")
                if rows:
                    delivery_received = True
                    break
                time.sleep(2)

        TestWebhookDelivery._bitbucket_delivery_received = delivery_received
        latency_ms = (time.perf_counter() - start) * 1000

        result = {
            "scenario": "BITBUCKET-WEBHOOK",
            "webhook_target_url": webhook_target_url,
            "webhook_registered": webhook_registered,
            "webhook_uuid": webhook_uuid,
            "commit_pushed": commit_pushed,
            "delivery_received": delivery_received,
            "latency_ms": round(latency_ms, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        if not webhook_registered:
            result["verdict"] = "fail"
            result["error"] = (
                f"Bitbucket webhook registration failed: HTTP {register_resp.status_code}. "
                f"Response: {register_resp.text[:300]}"
            )
        elif not commit_pushed:
            result["verdict"] = "fail"
            result["error"] = (
                f"Bitbucket commit push failed: HTTP {commit_resp.status_code}. "
                f"Response: {commit_resp.text[:300]}"
            )
        elif not delivery_received:
            result["verdict"] = "fail"
            result["error"] = (
                f"Webhook delivery not received within {WEBHOOK_DELIVERY_TIMEOUT}s. "
                f"No 'webhook.bitbucket.received' event found in audit_events."
            )
        else:
            result["verdict"] = "pass"

        TestWebhookDelivery._scenario_results.append(result)

        # Assert — allow soft failure with evidence
        if not delivery_received:
            pytest.fail(
                f"Bitbucket webhook delivery not confirmed within {WEBHOOK_DELIVERY_TIMEOUT}s. "
                f"Webhook registered: {webhook_registered}, Commit pushed: {commit_pushed}"
            )

    def test_verify_audit_events(self, credentials, evidence_collector):
        """R15.4: Verify audit_events contains webhook event rows.

        WHEN webhooks are received, THE Test_Framework SHALL verify
        automation.audit_events contains rows with event_type='webhook.jira.received'
        and event_type='webhook.bitbucket.received'.
        """
        tunnel_url = TestWebhookDelivery._tunnel_url
        if tunnel_url is None:
            pytest.skip("Tunnel not provisioned — cannot verify audit events")

        jira_rows = _query_audit_events("webhook.jira.received")
        bitbucket_rows = _query_audit_events("webhook.bitbucket.received")

        jira_found = len(jira_rows) > 0
        bitbucket_found = len(bitbucket_rows) > 0

        result = {
            "scenario": "AUDIT-EVENTS-VERIFY",
            "jira_events_found": jira_found,
            "jira_event_count": len(jira_rows),
            "bitbucket_events_found": bitbucket_found,
            "bitbucket_event_count": len(bitbucket_rows),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Determine verdict based on what was attempted
        if TestWebhookDelivery._jira_delivery_received and not jira_found:
            result["verdict"] = "fail"
            result["error"] = "Jira delivery was received but no audit_events row found"
        elif TestWebhookDelivery._bitbucket_delivery_received and not bitbucket_found:
            result["verdict"] = "fail"
            result["error"] = "Bitbucket delivery was received but no audit_events row found"
        elif jira_found or bitbucket_found:
            result["verdict"] = "pass"
        else:
            result["verdict"] = "skip"
            result["note"] = (
                "No webhook deliveries were received, so no audit events expected"
            )

        TestWebhookDelivery._scenario_results.append(result)

        # Soft assertion — don't fail if webhooks weren't delivered
        if TestWebhookDelivery._jira_delivery_received:
            assert jira_found, (
                "Expected 'webhook.jira.received' in audit_events but none found"
            )
        if TestWebhookDelivery._bitbucket_delivery_received:
            assert bitbucket_found, (
                "Expected 'webhook.bitbucket.received' in audit_events but none found"
            )

    def test_emit_webhook_evidence(self, credentials, evidence_collector):
        """R15.5, R15.6: Emit e2e-evidence/15-webhooks.json with full evidence.

        THE Evidence_Collector SHALL emit e2e-evidence/15-webhooks.json with
        tunnel URL, subscription IDs, delivery timestamps and audit row evidence.
        """
        # Cleanup: stop tunnel and unregister webhooks
        self._cleanup_resources(credentials)

        # Build overall verdict
        all_pass = all(
            r.get("verdict") == "pass"
            for r in TestWebhookDelivery._scenario_results
        )
        any_fail = any(
            r.get("verdict") == "fail"
            for r in TestWebhookDelivery._scenario_results
        )

        if all_pass:
            overall_verdict = "pass"
        elif any_fail:
            overall_verdict = "fail"
        else:
            overall_verdict = "partial"

        evidence_data = {
            "test": "test_15_webhooks",
            "overall_verdict": overall_verdict,
            "tunnel_tool": TestWebhookDelivery._tunnel_tool,
            "tunnel_url": TestWebhookDelivery._tunnel_url,
            "jira_webhook_id": TestWebhookDelivery._jira_webhook_id,
            "bitbucket_webhook_uuid": TestWebhookDelivery._bitbucket_webhook_uuid,
            "jira_issue_key": TestWebhookDelivery._jira_issue_key,
            "jira_delivery_received": TestWebhookDelivery._jira_delivery_received,
            "bitbucket_delivery_received": TestWebhookDelivery._bitbucket_delivery_received,
            "scenarios": TestWebhookDelivery._scenario_results,
        }

        evidence_path = evidence_collector.emit_json(
            "R15", EVIDENCE_FILENAME, evidence_data
        )

        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )

    # ─── Cleanup helpers ───────────────────────────────────────────────

    def _cleanup_resources(self, credentials) -> None:
        """Clean up tunnel process and webhook subscriptions."""
        # Stop tunnel
        if TestWebhookDelivery._tunnel_proc:
            _stop_tunnel(TestWebhookDelivery._tunnel_proc)
            TestWebhookDelivery._tunnel_proc = None

        # Unregister Jira webhook (best-effort)
        if TestWebhookDelivery._jira_webhook_id:
            try:
                client = _build_jira_client(credentials)
                webhook_id = TestWebhookDelivery._jira_webhook_id
                # Try to delete by self URL or by ID
                if webhook_id.startswith("http"):
                    client.delete(webhook_id)
                else:
                    client.delete(f"/rest/webhooks/1.0/webhook/{webhook_id}")
                client.close()
            except Exception:
                pass  # Best-effort cleanup

        # Unregister Bitbucket webhook (best-effort)
        if TestWebhookDelivery._bitbucket_webhook_uuid:
            try:
                workspace = credentials.bitbucket_workspace
                repo_slug = credentials.bitbucket_repo
                token = credentials.bitbucket_token_bearer
                uuid = TestWebhookDelivery._bitbucket_webhook_uuid

                url = (
                    f"https://api.bitbucket.org/2.0/repositories/"
                    f"{workspace}/{repo_slug}/hooks/{uuid}"
                )
                with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                    client.delete(url, headers=_bb_headers(token))
            except Exception:
                pass  # Best-effort cleanup

        # Clean up Jira test issue (best-effort)
        if TestWebhookDelivery._jira_issue_key:
            try:
                client = _build_jira_client(credentials)
                client.delete(
                    f"/rest/api/3/issue/{TestWebhookDelivery._jira_issue_key}"
                )
                client.close()
            except Exception:
                pass  # Best-effort cleanup
