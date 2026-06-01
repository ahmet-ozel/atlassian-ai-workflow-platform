"""HTTP-backed :class:`MCPCallerProtocol` implementation for the
``automation-worker``.

The :mod:`output_actions` activity dispatches every action (Jira
comment, Jira attachment, Bitbucket PR, Confluence page, Jira
transition) through the protocol declared in
:mod:`automation_worker.activities.output_actions`. Tests inject an
in-memory fake; production wires :class:`HttpMCPCaller` here so each
outgoing MCP request carries:

* ``X-Client-Source: automation-worker`` (platform-gap-fill task 8.2 /
  Requirement 9.3) — set by :func:`http_shared.make_mcp_client` so the
  observability layer can break MCP traffic down by origin Component.
* ``X-Trace-Id`` — injected per-request by the trace-id event hook
  attached to every client returned by ``make_mcp_client`` (task 7.2 /
  R8.4); the value reflects the trace_id installed on the activity's
  context by the workflow input plumbing.
* The three Atlassian credential headers (``-Url``, ``-Username``,
  ``-Personal-Token``) for the requested *service* — injected for the
  duration of the JSON-RPC call by
  :func:`http_shared.with_atlassian_creds` and then restored.

The caller is intentionally thin: it is the integration glue between
the :mod:`output_actions` Protocol and the MCP server's JSON-RPC
endpoint.  All retry / timeout policy lives on the activity side
(``ACTION_TIMEOUT_SECONDS`` is enforced per call); the caller just
honours the timeout it is given.

Validates Requirements: 9.3, 9.4 (platform-gap-fill spec)
Design reference: design.md §"MCP Client Source Etiketi"
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable

import httpx

from http_shared import make_mcp_client, with_atlassian_creds

from automation_worker.activities.minio_jira_upload import (
    upload_minio_artifact_to_jira,
)

__all__ = (
    "HttpMCPCaller",
    "MCPHttpError",
    "build_default_mcp_caller",
    "infer_service_from_tool",
    "resolve_mcp_tool_name",
)

_LOG = logging.getLogger(__name__)

#: Default MCP base URL (matches every other worker / service).  The
#: ``MCP_BASE_URL`` env var overrides it in production deployments.
_DEFAULT_MCP_BASE_URL: str = "http://atlassian-mcp:8090"

#: MCP streamable-http endpoint.
_MCP_PATH: str = "/mcp"
_MCP_ACCEPT: str = "application/json, text/event-stream"

#: ``X-Client-Source`` value advertised by this worker. Pinned here so
#: future refactors of :mod:`output_actions` cannot accidentally drop
#: the identifier.
CLIENT_SOURCE: str = "automation-worker"

#: Map MCP tool names → Atlassian service for credential injection.
#: Tools not in this table fall through to ``"jira"`` because every
#: action the :mod:`output_actions` activity dispatches today is
#: routed through Jira credentials except for the explicit
#: Bitbucket / Confluence calls listed below.
_TOOL_SERVICE_MAP: dict[str, str] = {
    "jira_get_issue": "jira",
    "jira_add_comment": "jira",
    "jira_add_attachment": "jira",
    "jira_transition_issue": "jira",
    "bitbucket_create_pr": "bitbucket",
    "bitbucket_create_pull_request": "bitbucket",
    "bitbucket_put_file_content": "bitbucket",
    "confluence_create_page": "confluence",
    "confluence_update_page": "confluence",
}

_TOOL_NAME_ALIASES: dict[str, str] = {
    # The output-action vocabulary uses the shorter historical name; the
    # mounted FastMCP Bitbucket server exposes create_pull_request under
    # the bitbucket_ prefix.
    "bitbucket_create_pr": "bitbucket_create_pull_request",
}


class MCPHttpError(RuntimeError):
    """Raised when the MCP server returns a non-2xx response or a
    JSON-RPC ``error`` envelope."""

    def __init__(self, tool_name: str, status_code: int, detail: str) -> None:
        # O5 fix (GEREKSINIM_ANALIZI.md): the Confluence smoke test
        # surfaced ``"The calling user does not have permission to view
        # the content"`` as an opaque MCP failure (E2E vs VPS report
        # divergence). Detect permission-shaped errors here and tag
        # the exception with a structured ``is_permission_denied`` flag
        # + a human-readable Turkish hint so downstream Jira-comment
        # helpers can show "bot hesabının izni yok" instead of the raw
        # Atlassian error blob.
        self.tool_name = tool_name
        self.status_code = status_code
        self.detail = detail
        self.is_permission_denied = self._detect_permission_denied(
            status_code, detail
        )
        suffix = ""
        if self.is_permission_denied:
            suffix = (
                " | permission_denied: bot hesabının ilgili Confluence "
                "space/sayfasına yazma yetkisi yok — yönetici bu hesaba "
                "izin vermeli ya da farklı bir bot hesabı atanmalı"
            )
        super().__init__(
            f"MCP call '{tool_name}' failed (status={status_code}): "
            f"{detail}{suffix}"
        )

    @staticmethod
    def _detect_permission_denied(status_code: int, detail: str) -> bool:
        """Return ``True`` when the MCP error looks like an Atlassian
        permission denial. Covers Confluence's
        ``"calling user does not have permission"`` blob and HTTP 401/403
        statuses generically."""
        if status_code in (401, 403):
            return True
        if not detail:
            return False
        lowered = detail.lower()
        return (
            "does not have permission" in lowered
            or "not authorized" in lowered
            or "no permission" in lowered
            or "forbidden" in lowered
        )


def infer_service_from_tool(tool_name: str) -> str:
    """Return the Atlassian service whose credentials *tool_name* needs.

    Defaults to ``"jira"`` when the tool is unknown — every action the
    :mod:`output_actions` activity emits today is either explicitly in
    :data:`_TOOL_SERVICE_MAP` or routed through Jira (e.g. transitions
    and comments).
    """

    return _TOOL_SERVICE_MAP.get(tool_name, "jira")


def resolve_mcp_tool_name(tool_name: str) -> str:
    """Return the concrete FastMCP tool name for a logical action name."""

    return _TOOL_NAME_ALIASES.get(tool_name, tool_name)


def _build_jsonrpc_request(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON-RPC 2.0 ``tools/call`` request envelope."""

    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }


def _parse_jsonrpc_result(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Extract the ``result`` body from a JSON-RPC 2.0 envelope.

    Raises :class:`MCPHttpError` if the envelope carries an ``error``
    object or no ``result``.
    """

    if "error" in payload:
        error = payload["error"] or {}
        message = (
            error.get("message", "unknown error") if isinstance(error, dict) else str(error)
        )
        raise MCPHttpError(tool_name, status_code=-1, detail=str(message))

    result = payload.get("result")
    if result is None:
        raise MCPHttpError(tool_name, status_code=-1, detail="empty result envelope")

    if isinstance(result, dict):
        _raise_for_mcp_tool_error(result, tool_name)
        return result
    return {"result": result}


def _jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _result_text(result: dict[str, Any]) -> str:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = _jsonish(structured.get("result"))
        if isinstance(nested, dict):
            return json.dumps(nested, ensure_ascii=False)
        if isinstance(nested, str) and nested:
            return nested
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]
    return json.dumps(result, ensure_ascii=False)


def _raise_for_mcp_tool_error(result: dict[str, Any], tool_name: str) -> None:
    """Surface MCP tool-level failures inside successful JSON-RPC envelopes."""

    if result.get("isError") is True:
        raise MCPHttpError(tool_name, status_code=-1, detail=_result_text(result))

    candidates: list[Any] = [result.get("structuredContent")]
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        candidates.append(structured.get("result"))
    for item in result.get("content", []) or []:
        if isinstance(item, dict):
            candidates.append(item.get("text"))

    for candidate in candidates:
        parsed = _jsonish(candidate)
        if isinstance(parsed, dict) and parsed.get("success") is False:
            raise MCPHttpError(
                tool_name,
                status_code=-1,
                detail=json.dumps(parsed, ensure_ascii=False)[:500],
            )


def _decode_jsonrpc_payload(
    response: httpx.Response,
    tool_name: str,
) -> dict[str, Any]:
    """Decode JSON-RPC returned either as JSON or streamable-http SSE."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload

    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed

    snippet = response.text[:500]
    raise MCPHttpError(
        tool_name,
        status_code=response.status_code,
        detail=f"non-JSON/SSE response: {snippet}",
    )


class HttpMCPCaller:
    """Production :class:`MCPCallerProtocol` implementation.

    The caller owns a single :class:`httpx.AsyncClient` (with
    ``X-Client-Source`` and ``X-Trace-Id`` injection wired up by the
    factory) and uses :func:`with_atlassian_creds` to inject
    department-specific Atlassian credentials *only* for the duration
    of a single ``call_tool`` invocation.

    Parameters
    ----------
    credential_resolver:
        A duck-typed credential resolver with an async
        ``get(dept_id, service, scope=...)`` method (mirrors the shape
        used by ``agent-runner-worker``).
    base_url:
        MCP base URL (e.g. ``http://atlassian-mcp:8090``). When ``None``
        the value of the ``MCP_BASE_URL`` env var is used; falling back
        to :data:`_DEFAULT_MCP_BASE_URL` when that is also unset.
    client_factory:
        Optional override for the ``httpx.AsyncClient`` factory used to
        build the underlying client. Tests inject a custom factory to
        feed in an :class:`httpx.MockTransport`; production code leaves
        this as ``None`` so :func:`http_shared.make_mcp_client` is used
        and the ``X-Client-Source`` invariant cannot be sidestepped.
    """

    def __init__(
        self,
        *,
        credential_resolver: Any,
        base_url: str | None = None,
        client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._base_url = base_url or os.environ.get(
            "MCP_BASE_URL", _DEFAULT_MCP_BASE_URL
        )
        self._client_factory = client_factory or self._default_client_factory

    @staticmethod
    def _default_client_factory(
        *, base_url: str, timeout: float
    ) -> httpx.AsyncClient:
        """Build the client via :func:`http_shared.make_mcp_client`.

        Pinning ``client_source=CLIENT_SOURCE`` at this single call
        site is what guarantees every outgoing MCP request from this
        worker carries ``X-Client-Source: automation-worker`` (R9.3).
        """

        return make_mcp_client(
            client_source=CLIENT_SOURCE,
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": _MCP_ACCEPT},
        )

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        dept_id: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Invoke an MCP tool and return its parsed JSON-RPC result.

        Parameters
        ----------
        tool_name:
            The MCP tool to invoke (e.g. ``"jira_add_comment"``).
        params:
            Tool-specific arguments passed verbatim under
            ``params.arguments`` of the JSON-RPC envelope.
        dept_id:
            Department whose Atlassian credentials should be injected.
        timeout:
            Request timeout in seconds; honoured by the underlying
            ``httpx.AsyncClient``.

        Returns
        -------
        dict
            The ``result`` body of the JSON-RPC response.

        Raises
        ------
        MCPHttpError
            On HTTP non-2xx, JSON-RPC ``error`` envelope, or empty
            ``result`` envelope.
        """

        if tool_name == "upload_artifact_to_jira":
            return await upload_minio_artifact_to_jira(
                params,
                dept_id=dept_id,
                credential_resolver=self._credential_resolver,
                timeout=timeout,
            )

        service = infer_service_from_tool(tool_name)
        concrete_tool_name = resolve_mcp_tool_name(tool_name)
        request_body = _build_jsonrpc_request(concrete_tool_name, params)

        client = self._client_factory(base_url=self._base_url, timeout=timeout)

        async with client:
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,  # type: ignore[arg-type]
                credential_resolver=self._credential_resolver,
            ) as authed:
                try:
                    response = await authed.post(_MCP_PATH, json=request_body)
                except httpx.HTTPError as exc:
                    raise MCPHttpError(
                        concrete_tool_name,
                        status_code=-1,
                        detail=f"transport error: {exc}",
                    ) from exc

                if response.status_code >= 400:
                    raise MCPHttpError(
                        concrete_tool_name,
                        status_code=response.status_code,
                        detail=response.text[:500],
                    )

                payload = _decode_jsonrpc_payload(response, concrete_tool_name)

        return _parse_jsonrpc_result(payload, concrete_tool_name)


def build_default_mcp_caller(
    credential_resolver: Any,
    *,
    base_url: str | None = None,
) -> HttpMCPCaller:
    """Convenience factory used by ``main.py`` at worker boot.

    Keeps the boot script ignorant of the constructor signature so a
    future change (e.g. carrying retry policy) only touches this
    helper.
    """

    return HttpMCPCaller(
        credential_resolver=credential_resolver,
        base_url=base_url,
    )
