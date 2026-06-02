"""Streamlit boot script — wires session-state collaborators (`platform-mimari-ops` task 9.10).

Streamlit runs each page module independently, but every page in
``pages/`` reads its collaborators from ``st.session_state``:

* ``_assistant_client`` — POSTs to ``assistant-service /api/chat/stream``.
* ``_admin_api_client`` — generic GET/POST against admin-dashboard-api.
* ``_mcp_read_client`` — read-only MCP client for the Explorer page.
* ``_costs_api`` — wraps the ``GET /api/costs/me`` widget endpoint.
* ``_credential_api`` — POSTs per-user session credentials to
  ``assistant-service /api/session-credentials/...``.
* ``_quick_actions`` — per-page shortcut buttons (Task Creator / chat).
* ``_cookie_reader`` / ``_cookie_writer`` — signed multi-dept cookie
  helpers (R3.11).
* ``_probe_runner`` — invokes the dept connectivity probe through
  admin-dashboard-api.

This module is the single entry point Streamlit invokes via
``streamlit run app.py``; it injects every collaborator into
``st.session_state`` once per session, then routes the user to the
configured landing page. Production deployments override the API
base URLs through the ``.env`` file (see ``.env.example``).

Each collaborator is wired behind a soft import so the dev-mode
streamlit-app stays usable when one of the upstream services is
down — the page simply renders the failure as a banner instead of
crashing the whole UI.
"""

from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st

from config import Settings


def _settings() -> Settings:
    """Return the cached :class:`Settings` instance."""
    if "_settings" not in st.session_state:
        st.session_state["_settings"] = Settings()
    return st.session_state["_settings"]


# ---------------------------------------------------------------------------
# HTTP client helpers
# ---------------------------------------------------------------------------


def _build_http_client(base_url: str) -> Any:
    """Return a thin object exposing ``get`` / ``post`` against ``base_url``.

    Uses ``httpx.Client`` when installed (production); falls back to
    a stub that raises a clear error when ``httpx`` is missing. The
    stub keeps the page-side ``apiFetch`` calls from crashing at
    import time on a freshly cloned repo without dev deps.
    """

    try:
        import httpx  # type: ignore[import-not-found]
    except ImportError:

        class _Stub:
            def get(self, path: str, **kwargs: Any) -> Any:
                raise RuntimeError(
                    "httpx not installed; install streamlit-app deps"
                )

            def post(self, path: str, **kwargs: Any) -> Any:
                raise RuntimeError(
                    "httpx not installed; install streamlit-app deps"
                )

        return _Stub()

    class _Client:
        def __init__(self, _base: str) -> None:
            self._base = _base.rstrip("/")
            self._client = httpx.Client(timeout=10.0)

        def get(self, path: str, **kwargs: Any) -> httpx.Response:
            return self._client.get(self._base + path, **kwargs)

        def post(self, path: str, **kwargs: Any) -> httpx.Response:
            return self._client.post(self._base + path, **kwargs)

        def _json(self, response: httpx.Response) -> Any:
            response.raise_for_status()
            return response.json()

        def list_workflows(self, *, dept_id: str, status: str = "all") -> list[dict[str, Any]]:
            params = {"dept_id": dept_id}
            if status and status != "all":
                params["status"] = status
            data = self._json(self.get("/api/v1/workflows", params=params))
            return data.get("items", data if isinstance(data, list) else [])

        def cancel_workflow(self, *, workflow_id: str) -> dict[str, Any]:
            return self._json(self.post(f"/api/v1/workflows/{workflow_id}/cancel"))

        def retry_workflow(self, *, workflow_id: str) -> dict[str, Any]:
            return self._json(self.post(f"/api/v1/workflows/{workflow_id}/retry"))

        def reply_to_workflow(self, *, workflow_id: str, text: str) -> dict[str, Any]:
            return self._json(
                self.post(
                    f"/api/v1/workflows/{workflow_id}/signal",
                    json={"signal_name": "info_received", "payload": {"text": text}},
                )
            )

        def rerun_workflow_with_env(self, *, workflow_id: str, env_overrides: dict[str, str]) -> dict[str, Any]:
            return self._json(
                self.post(
                    f"/api/v1/workflows/{workflow_id}/signal",
                    json={
                        "signal_name": "rerun_with_env",
                        "payload": {"env_overrides": env_overrides},
                    },
                )
            )

        def list_orphan_branches(self, *, dept_id: str) -> list[dict[str, Any]]:
            data = self._json(self.get("/api/orphan-branches", params={"dept_id": dept_id}))
            return data.get("branches", data if isinstance(data, list) else [])

        def trigger_orphan_branch_deletion(self, *, repo: str, branch: str) -> dict[str, Any]:
            return self._json(
                self.post(
                    "/api/v1/workflows/bot-branch-retention/signal",
                    json={
                        "signal_name": "delete_orphan_branch",
                        "payload": {"repo": repo, "branch": branch},
                    },
                )
            )

        def list_runner_workspaces(self) -> dict[str, Any]:
            return self._json(self.get("/admin/runner/workspaces"))

        def purge_runner_workspace(self, *, issue_key: str) -> dict[str, Any]:
            response = self._client.delete(self._base + f"/admin/runner/workspaces/{issue_key}")
            return self._json(response)

        def list_po_review_requests(self, *, dept_id: str) -> list[dict[str, Any]]:
            data = self._json(self.get("/api/po-review-inbox", params={"dept_id": dept_id}))
            return data.get("items", data.get("requests", data if isinstance(data, list) else []))

        def po_decision(self, *, workflow_id: str, decision: str, comment: str) -> dict[str, Any]:
            return self._json(
                self.post(
                    f"/api/v1/workflows/{workflow_id}/signal",
                    json={
                        "signal_name": "po_decision",
                        "payload": {"decision": decision, "comment": comment},
                    },
                )
            )

        def list_mcp_tools(self, *, dept_id: str) -> list[dict[str, Any]]:
            del dept_id
            client = st.session_state.get("_mcp_read_client")
            return client.list_tools() if client is not None else []

        def invoke_mcp_tool(self, *, dept_id: str, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
            del dept_id
            client = st.session_state.get("_mcp_read_client")
            if client is None:
                raise RuntimeError("MCP client is not configured")
            result = client._call_tool(tool_name, params)
            return result if isinstance(result, dict) else {"result": result}

        def emit_audit_event(self, *, action: str, resource: str, payload: dict[str, Any]) -> None:
            try:
                self.post(
                    "/admin/audit-events",
                    json={"action": action, "resource": resource, "payload": payload},
                )
            except Exception:
                return None

    return _Client(base_url)


# ---------------------------------------------------------------------------
# Cookie helpers (R3.11, R10.1–R10.5)
# ---------------------------------------------------------------------------


def _init_cookie_controller() -> Any:
    """Initialize the streamlit-cookies-controller instance.

    Returns the CookieController object or None if the package is
    unavailable (graceful degradation for dev environments without
    the dependency installed).
    """
    try:
        from streamlit_cookies_controller import CookieController  # type: ignore[import-not-found]
    except ImportError:
        return None

    # CookieController must be instantiated once per session; it
    # renders a hidden iframe that bridges JS cookies to Python.
    if "_cookie_controller" not in st.session_state:
        st.session_state["_cookie_controller"] = CookieController()
    return st.session_state["_cookie_controller"]


class _CookieReader:
    """Real cookie reader backed by streamlit-cookies-controller.

    Reads raw cookie values from the browser. The cookie_manager
    module handles HMAC signature verification on top of this.

    Falls back to returning None when the controller is unavailable
    so pages degrade gracefully to dept-switcher state.
    """

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def get(self, name: str) -> str | None:
        if self._controller is None:
            return None
        try:
            value = self._controller.get(name)
            return value if value else None
        except Exception:  # noqa: BLE001
            return None

    def __call__(self, name: str) -> str | None:
        """Allow callable interface for cookie_manager compatibility."""
        return self.get(name)

    def delete(self, name: str) -> None:
        """Delete a cookie from the browser."""
        if self._controller is None:
            return
        try:
            self._controller.remove(name)
        except Exception:  # noqa: BLE001
            pass


class _CookieWriter:
    """Real cookie writer backed by streamlit-cookies-controller.

    Writes cookie values to the browser with configurable TTL.
    The cookie_manager module handles HMAC signing before calling
    this writer.
    """

    def __init__(self, controller: Any) -> None:
        self._controller = controller

    def set(self, name: str, value: str, *, max_age: int = 30 * 86400) -> None:
        if self._controller is None:
            return
        try:
            self._controller.set(name, value, max_age=max_age)
        except Exception:  # noqa: BLE001
            pass

    def __call__(self, name: str, value: str, *, ttl_days: int = 30) -> None:
        """Allow callable interface for cookie_manager compatibility."""
        self.set(name, value, max_age=ttl_days * 86400)


# ---------------------------------------------------------------------------
# Quick-actions registry
# ---------------------------------------------------------------------------


class _QuickActions:
    """Cross-page shortcut buttons.

    Each page renders its own quick-action UI; this registry only
    exposes the metadata so the buttons land on the right page
    consistently.
    """

    @staticmethod
    def to_task_creator_query(query: str) -> dict:
        """Return URL query params that pre-populate the Task Creator."""
        return {"q": query}

    @staticmethod
    def to_chat_query(message: str) -> dict:
        return {"prefill": message}


# ---------------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------------


class _ProbeRunner:
    """Invokes the dept connectivity probe through admin-dashboard-api."""

    def __init__(self, admin_client: Any) -> None:
        self._client = admin_client

    def run(self, dept_id: str) -> dict[str, Any]:
        try:
            resp = self._client.post(
                "/admin/probe", json={"dept_id": dept_id}
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}
        if hasattr(resp, "status_code") and resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"ok": True}


# ---------------------------------------------------------------------------
# Costs API wrapper
# ---------------------------------------------------------------------------


class _CostsApi:
    """Wraps GET /api/costs/me for the sidebar cost widget (R5.8)."""

    def __init__(self, admin_client: Any) -> None:
        self._client = admin_client

    def me(self) -> dict[str, Any]:
        try:
            resp = self._client.get("/api/costs/me")
        except Exception:  # noqa: BLE001
            return {"weekly_usd": 0.0, "monthly_usd": 0.0}
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {"weekly_usd": 0.0, "monthly_usd": 0.0}

    def get_me(self) -> dict[str, Any]:
        return self.me()


# ---------------------------------------------------------------------------
# Credential API wrapper
# ---------------------------------------------------------------------------


class _CredentialApi:
    """Wraps the per-user session credential POST (R8.4)."""

    def __init__(self, assistant_client: Any) -> None:
        self._client = assistant_client

    def set_for_session(
        self, *, dept_id: str, service: str, token: str
    ) -> dict[str, Any]:
        try:
            resp = self._client.post(
                "/session/credentials",
                json={
                    "session_id": dept_id,
                    "service": service,
                    "url": "",
                    "username": "",
                    "personal_token": token,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": str(exc)}
        if hasattr(resp, "status_code") and resp.status_code >= 400:
            return {"ok": False, "status": resp.status_code}
        return {"ok": True}

    def post(
        self,
        *,
        dept_id: str,
        session_id: str,
        service: str,
        url: str,
        email: str,
        api_token: str,
        persist_with_pin: str | None = None,
    ) -> dict[str, Any]:
        del dept_id, persist_with_pin
        resp = self._client.post(
            "/session/credentials",
            json={
                "session_id": session_id,
                "service": service,
                "url": url,
                "username": email,
                "personal_token": api_token,
            },
        )
        if hasattr(resp, "status_code") and resp.status_code >= 400:
            raise RuntimeError(getattr(resp, "text", f"HTTP {resp.status_code}"))
        return resp.json()


class _AssistantClient:
    """Synchronous assistant-service client used by Chat and Task Creator."""

    def __init__(self, base_url: str, *, client_source: str = "streamlit-app") -> None:
        self._http = _build_http_client(base_url)
        self._client_source = client_source

    def _credential_ref(self, service: str) -> str | None:
        result = st.session_state.get(f"credential_{service}")
        vault_path = getattr(result, "vault_path", None) if result is not None else None
        if vault_path:
            return str(vault_path)

        bound = st.session_state.get("bound_credentials") or set()
        if service not in bound:
            return None
        session_id = (st.session_state.get("user") or {}).get("session_id")
        if not session_id:
            return None
        return f"vault:atlassian/_user_session/{session_id}/{service}"

    def _headers(self, *, dept_id: str | None = None, service: str | None = None) -> dict[str, str]:
        headers = {"X-Client-Source": self._client_source}
        if dept_id:
            headers["X-Department-Id"] = dept_id
        for candidate in ("jira", "bitbucket", "confluence"):
            ref = self._credential_ref(candidate)
            if ref:
                headers[f"X-Credential-Ref-{candidate.capitalize()}"] = ref
        if service:
            credential_ref = self._credential_ref(service)
            if not credential_ref:
                session_id = (st.session_state.get("user") or {}).get("session_id")
                if session_id:
                    credential_ref = (
                        f"vault:atlassian/_user_session/{session_id}/{service}"
                    )
            if credential_ref:
                headers["X-Credential-Ref"] = credential_ref
                headers[f"X-Credential-Ref-{service.capitalize()}"] = credential_ref
        return headers

    def _capabilities(self) -> list[str]:
        capabilities: list[str] = []
        for service in ("jira", "bitbucket", "confluence"):
            if self._credential_ref(service):
                capabilities.append(service)
        return capabilities

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._http.get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        headers = kwargs.pop("headers", None) or {}
        merged_headers = {"X-Client-Source": self._client_source, **headers}
        return self._http.post(path, headers=merged_headers, **kwargs)

    def stream(
        self,
        *,
        dept_id: str,
        session_id: str,
        text: str,
        history: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        resp = self._http.post(
            "/api/chat/stream",
            json={
                "user_message": text,
                "history": history,
                "dept_id": dept_id,
                "session_id": session_id,
                "capabilities": self._capabilities(),
            },
            headers=self._headers(dept_id=dept_id, service="jira"),
            timeout=60.0,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        events: list[dict[str, Any]] = []
        for line in getattr(resp, "text", "").splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except ValueError:
                events.append({"type": "token", "payload": {"text": raw}})
        return events

    def create_task(self, **payload: Any) -> dict[str, Any]:
        dept_id = str(payload.get("dept_id") or payload.get("department_id") or "")
        resp = self._http.post(
            "/api/tasks/create",
            json=payload,
            headers=self._headers(dept_id=dept_id, service="jira"),
            timeout=30.0,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp.json()


class _BotInfoApi:
    """Tiny adapter consumed by the Task Creator bot assignee card."""

    def __init__(self, assistant_client: _AssistantClient) -> None:
        self._client = assistant_client

    def get_bot_info(self, dept_id: str) -> dict[str, Any] | None:
        try:
            resp = self._client.get(f"/api/dept/{dept_id}/bot-info", timeout=5.0)
            if hasattr(resp, "status_code") and resp.status_code >= 400:
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None


# ---------------------------------------------------------------------------
# MCP read-only client wrapper
# ---------------------------------------------------------------------------


class _McpReadClient:
    """Read-only Atlassian MCP wrapper used by the Explorer page (R3.3).

    The Explorer is the single page allowed to talk to MCP directly
    (every other page proxies through assistant-service for the
    PII filter + banned-tool list). The wrapper exposes only
    explicitly-allowed read methods so a page bug cannot reach a
    write tool.
    """

    def __init__(self, base_url: str, *, assistant_base_url: str | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = _build_http_client(base_url)
        self._assistant_http = (
            _build_http_client(assistant_base_url.rstrip("/"))
            if assistant_base_url
            else None
        )

    def _credential_headers(self) -> dict[str, str]:
        headers = {"X-Client-Source": "streamlit-app"}
        for service in ("jira", "bitbucket", "confluence"):
            result = st.session_state.get(f"credential_{service}")
            vault_path = getattr(result, "vault_path", None) if result else None
            if vault_path:
                headers[f"X-Credential-Ref-{service.capitalize()}"] = str(vault_path)
        return headers

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._assistant_http is not None:
            resp = self._assistant_http.post(
                "/api/mcp/tools/call",
                json={"tool_name": name, "arguments": arguments},
                headers=self._credential_headers(),
                timeout=30.0,
            )
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            return resp.json()
        resp = self._http.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=self._credential_headers(),
            timeout=30.0,
        )
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    def list_tools(self) -> list[dict[str, Any]]:
        try:
            if self._assistant_http is not None:
                resp = self._assistant_http.get(
                    "/api/mcp/tools",
                    headers=self._credential_headers(),
                    timeout=15.0,
                )
                data = resp.json()
                tools = data.get("tools") if isinstance(data, dict) else None
                return tools if isinstance(tools, list) else []
            resp = self._http.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
                headers=self._credential_headers(),
                timeout=15.0,
            )
            data = resp.json()
            result = data.get("result", {}) if isinstance(data, dict) else {}
            return result.get("tools", []) if isinstance(result, dict) else []
        except Exception:  # noqa: BLE001
            return []

    def jira_get_issue(self, issue_key: str) -> dict[str, Any]:
        try:
            result = self._call_tool("jira_get_issue", {"issue_key": issue_key})
            return result if isinstance(result, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def search_jira(self, *, jql: str) -> list[dict[str, Any]]:
        try:
            result = self._call_tool("jira_search_issues", {"jql": jql})
        except Exception:
            result = self._call_tool("jira_search", {"jql": jql})
        return _items_from_mcp_result(result, "issues")

    def list_bitbucket_prs(self, *, repo: str) -> list[dict[str, Any]]:
        workspace, _, repo_slug = repo.partition("/")
        args = {"repo_slug": repo_slug or repo}
        if workspace and repo_slug:
            args["project_key"] = workspace
        result = self._call_tool("bitbucket_list_pull_requests", args)
        return _items_from_mcp_result(result, "pullrequests")

    def search_confluence(self, *, cql: str) -> list[dict[str, Any]]:
        try:
            result = self._call_tool("confluence_search", {"cql": cql})
        except Exception:
            result = self._call_tool("confluence_search_content", {"cql": cql})
        return _items_from_mcp_result(result, "results")


def _items_from_mcp_result(result: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(result, dict):
        value = result.get(key) or result.get("values") or result.get("items")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        content = result.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text") if isinstance(content[0], dict) else None
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    return []
                return _items_from_mcp_result(parsed, key)
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def _ensure_user_context(state: Any) -> None:
    """Install a minimal session user when OIDC has not populated one.

    This branch only runs in the dev fallback — production OIDC
    middleware populates ``state["user"]`` (with real ``dept_ids``
    claims) before this is reached, so the early return keeps the
    production path untouched. In dev mode we seed ``dept_ids`` from
    admin-dashboard-api so dept-scoped pages (Task Creator, PO Review
    Inbox) are reachable without a live IdP. An explicit
    ``STREAMLIT_DEV_DEPT_IDS`` env (comma-separated) wins when set.
    """

    if state.get("user"):
        return

    env_dept_ids = [
        item.strip()
        for item in os.environ.get("STREAMLIT_DEV_DEPT_IDS", "").split(",")
        if item.strip()
    ]
    dept_ids = env_dept_ids or _load_department_ids_from_admin_api()
    default_dept = os.environ.get("STREAMLIT_DEV_DEFAULT_DEPT_ID", "").strip()
    if default_dept not in dept_ids:
        default_dept = dept_ids[0] if dept_ids else ""

    state["user"] = {
        "id": os.environ.get("STREAMLIT_DEV_USER_ID", "dev-user"),
        "session_id": os.environ.get("STREAMLIT_DEV_SESSION_ID", "dev-streamlit-session"),
        "display_name": os.environ.get("STREAMLIT_DEV_USER_NAME", "Dev User"),
        "dept_ids": dept_ids,
        "default_dept_id": default_dept,
    }
    state.setdefault("auth_token", os.environ.get("STREAMLIT_DEV_AUTH_TOKEN", "dev"))


def _load_department_ids_from_admin_api() -> list[str]:
    """Return department IDs from admin-dashboard-api for dev sessions."""

    admin_url = os.environ.get(
        "ADMIN_DASHBOARD_API_URL",
        os.environ.get("ADMIN_API_URL", "http://admin-dashboard-api:8082"),
    )
    try:
        client = _build_http_client(admin_url)
        response = client.get(
            "/admin/departments",
            headers={"Authorization": "Bearer dev-admin-token"},
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        data = response.json()
    except Exception:  # noqa: BLE001
        return []

    rows = data.get("departments", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    dept_ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dept_id = str(row.get("id") or "").strip()
        if dept_id and dept_id not in dept_ids:
            dept_ids.append(dept_id)
    return dept_ids


# ---------------------------------------------------------------------------
# Session-state injection
# ---------------------------------------------------------------------------


def _inject_session_state() -> None:
    """Idempotently populate ``st.session_state`` with collaborators."""

    settings = _settings()
    state = st.session_state
    _ensure_user_context(state)

    if "_assistant_client" not in state:
        state["_assistant_client"] = _AssistantClient(
            os.environ.get(
                "ASSISTANT_SERVICE_URL",
                getattr(settings, "assistant_service_url", "http://assistant-service:8081"),
            ),
            client_source=getattr(settings, "client_source", "streamlit-app"),
        )

    if "_admin_api_client" not in state:
        state["_admin_api_client"] = _build_http_client(
            os.environ.get(
                "ADMIN_DASHBOARD_API_URL",
                getattr(settings, "admin_api_url", "http://admin-dashboard-api:8082"),
            )
        )

    if "_mcp_read_client" not in state:
        state["_mcp_read_client"] = _McpReadClient(
            os.environ.get(
                "MCP_BASE_URL",
                getattr(settings, "mcp_base_url", "http://atlassian-mcp:8090"),
            ),
            assistant_base_url=None,
        )

    if "_costs_api" not in state:
        state["_costs_api"] = _CostsApi(state["_admin_api_client"])

    if "_credential_api" not in state:
        state["_credential_api"] = _CredentialApi(state["_assistant_client"])

    if "_quick_actions" not in state:
        state["_quick_actions"] = []

    if "_bot_info_api" not in state:
        state["_bot_info_api"] = _BotInfoApi(state["_assistant_client"])

    # --- Cookie controller initialization (Requirement 10.1) ---
    # Initialize the streamlit-cookies-controller once per session.
    # The controller must be created before any cookie read/write.
    if "_cookie_reader" not in state or "_cookie_writer" not in state:
        controller = _init_cookie_controller()
        state["_cookie_reader"] = _CookieReader(controller)
        state["_cookie_writer"] = _CookieWriter(controller)

    if "_probe_runner" not in state:
        state["_probe_runner"] = _ProbeRunner(state["_admin_api_client"])

    # --- Department cookie integration (Requirements 10.2, 10.3, 10.5) ---
    # On app load: read department cookie, verify signature, load into
    # session state. Pre-fill department selector with cookie value.
    # On invalid signature: delete cookie, redirect to selector.
    if "current_dept" not in state:
        dept_from_cookie = _read_verified_department_cookie(state)
        user = state.get("user") or {}
        allowed_depts = user.get("dept_ids") or []
        if dept_from_cookie and dept_from_cookie not in allowed_depts:
            dept_from_cookie = None
        fallback_dept = (
            user.get("default_dept_id")
            or (allowed_depts or ["default"])[0]
        )
        state["current_dept"] = dept_from_cookie or fallback_dept or "default"
        # Also populate active_dept_id so the dept_switcher component
        # can use the cookie value as its pre-fill default (Req 10.3).
        if "active_dept_id" not in state:
            state["active_dept_id"] = state["current_dept"]


_USER_NAV_PAGES: tuple[tuple[str, str, str], ...] = (
    ("🔑", "Credentials", "pages/0_credentials.py"),
    ("💬", "Chat", "pages/1_chat.py"),
    ("🆕", "Task Creator", "pages/2_task_creator.py"),
)


def render_user_navigation() -> None:
    """Render the end-user sidebar navigation.

    Streamlit's built-in page navigation is disabled in ``.streamlit/config.toml``
    so admin/debug-only pages such as Explorer and MCP Inspector do not leak
    into the normal user menu.
    """

    with st.sidebar:
        st.markdown("### Platform")
        page_link = getattr(st, "page_link", None)
        if callable(page_link):
            for icon, label, page in _USER_NAV_PAGES:
                st.page_link(page, label=label, icon=icon)
        else:  # pragma: no cover - legacy Streamlit fallback
            for icon, label, page in _USER_NAV_PAGES:
                st.markdown(f"{icon} `{page}`")


def _read_verified_department_cookie(state: dict) -> str | None:
    """Read and verify the department cookie on app load.

    Uses the cookie_manager module's signing/verification logic to
    ensure the cookie hasn't been tampered with.

    Checks both the primary ``dept_selection`` cookie (written by
    ``write_department_cookie``) and the ``active_dept_id`` cookie
    (written by the dept_switcher component) for maximum compatibility.

    On invalid signature: deletes the cookie so the user is redirected
    to the department selector (Requirement 10.5).

    Returns:
        The verified department string, or None if cookie is absent,
        expired, or has an invalid signature.
    """
    from components.cookie_manager import (
        COOKIE_NAME,
        verify_cookie,
        _get_secret,
    )

    reader = state.get("_cookie_reader")
    if reader is None:
        return None

    secret = _get_secret()

    # Try the primary dept_selection cookie first, then fall back to
    # the active_dept_id cookie written by the dept_switcher component.
    cookie_names = [COOKIE_NAME, "active_dept_id"]

    for cookie_name in cookie_names:
        try:
            raw_value = reader(cookie_name)
        except Exception:  # noqa: BLE001
            continue

        if not raw_value:
            continue

        # Verify the HMAC signature
        department = verify_cookie(raw_value, secret)

        if department is None:
            # Invalid signature — delete the tampered cookie (Req 10.5)
            try:
                reader.delete(cookie_name)
            except Exception:  # noqa: BLE001
                pass
            continue

        return department

    return None


def _has_bound_credentials() -> bool:
    """Return ``True`` when the user has bound at least one credential.

    Successful submits inside :func:`components.credential_form.render_credential_form`
    are recorded into ``st.session_state["bound_credentials"]`` (set of
    service names) by the credentials page. While that set is empty —
    or absent entirely on a freshly opened session — the landing page
    surfaces a "go to Credentials first" call-to-action (R4.5, MIMARI
    §16.17 Q6 + Y1 UX vurgusu).
    """

    bound = st.session_state.get("bound_credentials")
    if not bound:
        return False
    # Defensive: accept both ``set[str]`` (canonical) and any iterable
    # producing truthy entries — a downstream page persisting via cookie
    # restore could conceivably hand us a list/tuple.
    return any(bool(s) for s in bound)


def _render_empty_credentials_banner() -> None:
    """Render the landing-page info banner + page link to credentials.

    Streamlit ``st.page_link`` accepts a path relative to the entry
    script (``app.py``); ``pages/0_credentials.py`` is the canonical
    target enforced by R4.1 + R4.3 (the legacy
    ``pages/7_session_credentials.py`` was removed in task 9.2).
    """

    st.warning(
        "🔑 **Önce credential bağlamanız gerekiyor.**\n\n"
        "Token'lar yalnızca aktif Streamlit oturumunda Vault'ta saklanır. "
        "Chat ve task creator credential bağlanmadan çalışmaz.",
        icon="⚠️",
    )
    page_link = getattr(st, "page_link", None)
    if callable(page_link):
        page_link(
            "pages/0_credentials.py",
            label="Credentials sayfasına git →",
            icon="🔑",
        )
    else:  # pragma: no cover — legacy Streamlit fallback
        st.markdown(
            "➡️ Sol menüden **0_credentials** sayfasını açın."
        )


def main() -> None:
    """Streamlit entrypoint — render the landing page."""

    st.set_page_config(
        page_title="AI Bot Platform",
        page_icon=":robot_face:",
        layout="wide",
    )

    _inject_session_state()
    render_user_navigation()

    # --- Theme + hero ----------------------------------------------------
    from components.theme import apply_theme, page_hero, section_header

    apply_theme()

    page_hero(
        "AI Bot Platform",
        "Atlassian araçlarınızla konuşan, görev oluşturan ve "
        "iş akışlarını yöneten yapay zekâ asistanı.",
        icon="🤖",
    )

    # R4.5 — empty-credentials gate. Surfaced *before* the rest of the
    # landing page content so a freshly-onboarded user cannot miss the
    # required first step.
    if not _has_bound_credentials():
        _render_empty_credentials_banner()

    state = st.session_state
    active_dept = state.get("current_dept", "default")

    section_header("Aktif oturum", f"departman: {active_dept}")

    col1, col2, col3 = st.columns(3)
    col1.metric("Departman", active_dept)
    col2.metric(
        "Credential",
        "✅ Bağlı" if _has_bound_credentials() else "⏳ Bekliyor",
    )
    col3.metric(
        "Bağlı servis",
        str(len(state.get("bound_credentials", set()) or [])),
    )

    section_header("Sayfalar", "soldaki menüden seçin")

    pages_data = [
        ("🔑", "Credentials", "Atlassian token'larınızı bağlayın", "pages/0_credentials.py"),
        ("💬", "Chat", "AI ile konuşun, soru sorun", "pages/1_chat.py"),
        ("🆕", "Task Creator", "Jira task description taslağı hazırlayın", "pages/2_task_creator.py"),
    ]

    grid_cols = st.columns(2)
    for idx, (icon, label, desc, page) in enumerate(pages_data):
        with grid_cols[idx % 2]:
            with st.container(border=True):
                st.markdown(
                    f"<div style='display:flex; align-items:center; gap:0.7rem; margin-bottom:0.3rem'>"
                    f"<div style='width:36px; height:36px; border-radius:10px; "
                    f"background: linear-gradient(135deg, #6366f1, #7c3aed); "
                    f"display:grid; place-items:center; font-size:1.05rem'>"
                    f"{icon}</div>"
                    f"<strong style='font-size:1rem'>{label}</strong></div>"
                    f"<div style='color:#64748b; font-size:0.86rem; margin-bottom:0.5rem'>{desc}</div>",
                    unsafe_allow_html=True,
                )
                page_link = getattr(st, "page_link", None)
                if callable(page_link):
                    page_link(page, label="Aç →", icon=None)
                else:
                    st.markdown(f"`{page}`")


if __name__ == "__main__":
    main()
else:
    # When Streamlit imports the module to discover pages, run the
    # injection so every page's first call to ``st.session_state``
    # already sees the collaborators populated.
    _inject_session_state()
