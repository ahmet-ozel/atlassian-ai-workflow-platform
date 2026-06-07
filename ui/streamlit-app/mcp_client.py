"""MCP / assistant-service HTTP client wrapper for the Streamlit UI.

Provides two client factories:

1. :func:`make_mcp_client` - Direct MCP client for the Explorer page
   (read-only Atlassian queries). Carries ``X-Client-Source: streamlit-app``
   and per-request credential headers.

2. :func:`make_assistant_client` - Proxied client for Chat and Task Creator
   pages that routes through ``assistant-service`` (PII filter, audit, cost
   tracking, rate-limit, prompt versioning - all in one place).

Both factories delegate to ``libs/http-shared``'s :func:`make_mcp_client`
for the ``X-Client-Source`` header injection. Going through the shared
factory means the Streamlit UI cannot drift out of the
:data:`http_shared.KNOWN_CLIENT_SOURCES` invariant and inherits the
``X-Trace-Id`` injection hook for free.

Stateless Design
----------------
The Atlassian MCP is stateless: every request MUST carry the credential
in headers. The client injects credentials from the Streamlit session
state (populated by ``components/credential_form.py``) into each request.

Usage (Explorer page - direct MCP)::

    from mcp_client import MCPClient

    client = MCPClient.from_session()
    issues = await client.call_tool("jira_search", {"jql": "project=PAY"})

Usage (Chat page - via assistant-service)::

    from mcp_client import AssistantClient

    client = AssistantClient.from_session()
    response = await client.chat("Açık task'lar neler?")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import streamlit as st

from config import Settings

# The Streamlit UI must stamp every outgoing MCP / assistant-service request with
# ``X-Client-Source: streamlit-app``. Use the shared factory so the
# header (and the trace-id hook it carries) is wired identically with
# the workers.
try:  # pragma: no cover - exercised when http_shared is installed.
    from http_shared import make_mcp_client as _make_http_shared_client
except Exception:  # pragma: no cover - defensive fallback for envs
    # without the shared lib on the path.
    _make_http_shared_client = None  # type: ignore[assignment]

__all__ = [
    "MCPClient",
    "AssistantClient",
    "MCPError",
    "CredentialMissingError",
]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MCPError(Exception):
    """Raised when an MCP tool call returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"MCP error {status_code}: {detail}")


class CredentialMissingError(Exception):
    """Raised when no credential is available for the requested service."""

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(
            f"Credential for '{service}' not found in session. "
            "Please enter your credentials on the Credentials page."
        )


# ---------------------------------------------------------------------------
# Header builder
# ---------------------------------------------------------------------------

_CLIENT_SOURCE = "streamlit-app"
_CLIENT_SOURCE_HEADER = "X-Client-Source"
_CREDENTIAL_HEADER = "X-Credential-Ref"
_DEPARTMENT_HEADER = "X-Department-Id"


def _build_headers(
    dept_id: str | None = None,
    credential_ref: str | None = None,
    service: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build request headers with client source, department, and credential."""
    headers: dict[str, str] = {
        _CLIENT_SOURCE_HEADER: _CLIENT_SOURCE,
    }
    if dept_id:
        headers[_DEPARTMENT_HEADER] = dept_id
    if credential_ref:
        headers[_CREDENTIAL_HEADER] = credential_ref
        if service:
            headers[f"X-Credential-Ref-{service.capitalize()}"] = credential_ref
    if extra:
        headers.update(extra)
    return headers


def _build_all_credential_ref_headers() -> dict[str, str]:
    headers: dict[str, str] = {}
    for service in ("jira", "bitbucket", "confluence"):
        ref = _get_session_credential(service)
        if ref:
            headers[f"X-Credential-Ref-{service.capitalize()}"] = ref
    return headers


def _get_session_credential(service: str) -> str | None:
    """Read credential vault_path from Streamlit session state.

    The credential_form.py stores results as:
        st.session_state[f"credential_{service}"] = CredentialFormResult
    """
    key = f"credential_{service}"
    result = st.session_state.get(key)
    if result is None:
        return None
    # CredentialFormResult has a vault_path attribute
    vault_path = getattr(result, "vault_path", None)
    if vault_path:
        return vault_path
    # Fallback: might be stored as a plain string
    if isinstance(result, str):
        return result
    return None


def _get_active_dept_id() -> str | None:
    """Read active department from session state."""
    return st.session_state.get("active_department_id")


# ---------------------------------------------------------------------------
# MCPClient - Direct MCP access (Explorer, MCP Inspector)
# ---------------------------------------------------------------------------


@dataclass
class MCPClient:
    """Stateless HTTP client for direct Atlassian MCP tool calls.

    Used by Explorer and MCP Inspector pages for read-only queries.
    Each request carries the user's credential from session state.
    """

    base_url: str
    timeout: float = 30.0
    _client: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Go through the shared factory so ``X-Client-Source: streamlit-app`` is injected
        # consistently with every other Component. Falls back to a
        # plain ``httpx.AsyncClient`` only in environments where the
        # shared lib is not on the import path; the explicit header
        # below preserves behaviour for those cases.
        if _make_http_shared_client is not None:
            self._client = _make_http_shared_client(
                _CLIENT_SOURCE,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        else:  # pragma: no cover - defensive only
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={_CLIENT_SOURCE_HEADER: _CLIENT_SOURCE},
            )

    @classmethod
    def from_session(cls) -> "MCPClient":
        """Create client from Streamlit session settings."""
        settings = Settings()
        return cls(base_url=settings.mcp_base_url)

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        service: str = "jira",
    ) -> dict[str, Any]:
        """Call an MCP tool with credential injection.

        Args:
            tool_name: MCP tool name (e.g. "jira_search", "confluence_get_page").
            arguments: Tool arguments dict.
            service: Which Atlassian service credential to use
                     ("jira", "bitbucket", "confluence").

        Returns:
            Parsed JSON response from MCP.

        Raises:
            CredentialMissingError: No credential in session for the service.
            MCPError: MCP returned non-2xx.
        """
        credential_ref = _get_session_credential(service)
        if not credential_ref:
            raise CredentialMissingError(service)

        dept_id = _get_active_dept_id()
        headers = _build_headers(
            dept_id=dept_id,
            credential_ref=credential_ref,
            service=service,
        )

        payload = {
            "jsonrpc": "2.0",
            "id": tool_name,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        }

        try:
            response = await self._client.post(
                "/mcp",
                json=payload,
                headers=headers,
            )
        except httpx.RequestError as exc:
            _LOG.error("MCP request failed: %s", exc)
            raise MCPError(0, f"Connection error: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500]
            _LOG.warning(
                "MCP tool call failed",
                extra={
                    "tool": tool_name,
                    "status": response.status_code,
                    "detail": detail,
                },
            )
            raise MCPError(response.status_code, detail)

        return response.json()

    async def list_tools(self) -> list[dict[str, Any]]:
        """List available MCP tools (no credential needed)."""
        try:
            response = await self._client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
                headers=_build_headers(),
            )
            if response.status_code == 200:
                payload = response.json()
                result = payload.get("result", {}) if isinstance(payload, dict) else {}
                return result.get("tools", []) if isinstance(result, dict) else []
        except httpx.RequestError as exc:
            _LOG.error("MCP list_tools failed: %s", exc)
        return []

    async def health_check(self) -> bool:
        """Check if MCP is reachable."""
        try:
            response = await self._client.get("/healthz")
            return response.status_code == 200
        except httpx.RequestError:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


# ---------------------------------------------------------------------------
# AssistantClient - Proxied through assistant-service (Chat, Task Creator)
# ---------------------------------------------------------------------------


@dataclass
class AssistantClient:
    """HTTP client for assistant-service (chat proxy).

    Used by Chat and Task Creator pages. All LLM interactions go through
    assistant-service which handles PII filtering, audit logging, cost
    tracking, rate limiting, and prompt versioning.
    """

    base_url: str
    timeout: float = 60.0
    _client: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # See ``MCPClient.__post_init__`` - same shared-factory contract
        # so the assistant proxy traffic also carries
        # ``X-Client-Source: streamlit-app``.
        if _make_http_shared_client is not None:
            self._client = _make_http_shared_client(
                _CLIENT_SOURCE,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        else:  # pragma: no cover - defensive only
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={_CLIENT_SOURCE_HEADER: _CLIENT_SOURCE},
            )

    @classmethod
    def from_session(cls) -> "AssistantClient":
        """Create client from Streamlit session settings."""
        settings = Settings()
        return cls(base_url=settings.assistant_service_url)

    async def chat(
        self,
        message: str,
        *,
        history: list[dict[str, str]] | None = None,
        dept_id: str | None = None,
        system_prompt_override: str | None = None,
    ) -> dict[str, Any]:
        """Send a chat message through assistant-service.

        Args:
            message: User message text.
            history: Previous conversation messages.
            dept_id: Department context (defaults to session active dept).
            system_prompt_override: Optional system prompt override
                (used by Task Creator for the task-creation-assistant prompt).

        Returns:
            Response dict with "reply", "tokens_used", "cost_usd" keys.
        """
        dept_id = dept_id or _get_active_dept_id()
        headers = _build_headers(
            dept_id=dept_id,
            credential_ref=_get_session_credential("jira"),
            service="jira",
            extra=_build_all_credential_ref_headers(),
        )

        payload: dict[str, Any] = {
            "message": message,
            "history": history or [],
        }
        if dept_id:
            payload["department_id"] = dept_id
        if system_prompt_override:
            payload["system_prompt_override"] = system_prompt_override

        try:
            response = await self._client.post(
                "/api/chat",
                json=payload,
                headers=headers,
            )
        except httpx.RequestError as exc:
            _LOG.error("Assistant-service request failed: %s", exc)
            return {
                "reply": f"⚠️ Bağlantı hatası: {exc}",
                "tokens_used": 0,
                "cost_usd": 0.0,
                "error": True,
            }

        if response.status_code >= 400:
            detail = response.text[:500]
            _LOG.warning(
                "Assistant-service chat failed",
                extra={"status": response.status_code, "detail": detail},
            )
            return {
                "reply": f"⚠️ Servis hatası ({response.status_code}): {detail}",
                "tokens_used": 0,
                "cost_usd": 0.0,
                "error": True,
            }

        return response.json()

    async def create_task(
        self,
        *,
        dept_id: str,
        summary: str,
        description: str,
        assignee_account_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a Jira task through assistant-service.

        The assistant-service validates the request, applies PII filtering,
        and delegates to MCP for the actual Jira API call using the
        department's bot credential.

        Args:
            dept_id: Target department.
            summary: Jira issue summary (title).
            description: Full task description (generated by Task Creator).
            assignee_account_id: Bot account_id to auto-assign.

        Returns:
            Response dict with "issue_key", "issue_url" on success.
        """
        credential_ref = _get_session_credential("jira")
        headers = _build_headers(
            dept_id=dept_id,
            credential_ref=credential_ref,
            service="jira",
            extra=_build_all_credential_ref_headers(),
        )

        payload: dict[str, Any] = {
            "department_id": dept_id,
            "summary": summary,
            "description": description,
        }
        if assignee_account_id:
            payload["assignee_account_id"] = assignee_account_id

        try:
            response = await self._client.post(
                "/api/tasks/create",
                json=payload,
                headers=headers,
            )
        except httpx.RequestError as exc:
            _LOG.error("Task creation failed: %s", exc)
            return {"error": True, "detail": f"Connection error: {exc}"}

        if response.status_code >= 400:
            return {
                "error": True,
                "detail": response.text[:500],
                "status_code": response.status_code,
            }

        return response.json()

    async def health_check(self) -> bool:
        """Check if assistant-service is reachable."""
        try:
            response = await self._client.get("/healthz")
            return response.status_code == 200
        except httpx.RequestError:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
