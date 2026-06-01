"""Main FastMCP server setup for Atlassian integration."""

import base64
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from cachetools import TTLCache
from fastmcp import FastMCP
from fastmcp import settings as fastmcp_settings
from fastmcp.server.event_store import EventStore
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.tools import Tool as FastMCPTool
from mcp.types import Tool as MCPTool
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    generate_latest,
)
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_atlassian.confluence import ConfluenceFetcher
from mcp_atlassian.confluence.config import ConfluenceConfig
from mcp_atlassian.jira import JiraFetcher
from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.utils.env import is_env_truthy
from mcp_atlassian.utils.environment import get_available_services
from mcp_atlassian.utils.io import is_read_only_mode
from mcp_atlassian.utils.logging import mask_sensitive
from mcp_atlassian.utils.oauth import (
    CLOUD_AUTHORIZE_URL,
    CLOUD_TOKEN_URL,
    DC_AUTHORIZE_PATH,
    DC_TOKEN_PATH,
)
from mcp_atlassian.utils.token_verifier import AtlassianOpaqueTokenVerifier
from mcp_atlassian.utils.tools import get_enabled_tools, should_include_tool
from mcp_atlassian.utils.toolsets import (
    get_enabled_toolsets,
    should_include_tool_by_toolset,
)
from mcp_atlassian.utils.urls import is_atlassian_cloud_url, validate_url_for_ssrf

from .client_storage import build_oauth_client_storage_from_env
from .confluence import confluence_mcp
from .context import MainAppContext
from .jira import jira_mcp
from .bitbucket import bitbucket_mcp
from .oauth_proxy import HardenedOAuthProxy, parse_env_list

logger = logging.getLogger("mcp-atlassian.server.main")

DEFAULT_HOST = "0.0.0.0"  # noqa: S104
DEFAULT_ALLOWED_REDIRECT_URIS = [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chat.openai.com/connector_platform_oauth_redirect",
]
DEFAULT_ALLOWED_GRANT_TYPES = ["authorization_code", "refresh_token"]
OAUTH_PROXY_ENABLE_ENV = "ATLASSIAN_OAUTH_PROXY_ENABLE"

# ---------------------------------------------------------------------------
# X-Client-Source observability header — structured logging (Requirement 7.8)
# ---------------------------------------------------------------------------

_CLIENT_SOURCE_HEADER = "x-client-source"
_CLIENT_SOURCE_UNKNOWN = "unknown"

#: Structured logger dedicated to client-source observability. Emits one
#: record per inbound request so the admin-dashboard /audit panel can
#: filter by ``client_source``.
_client_source_logger = logging.getLogger("mcp-atlassian.client_source")


# ---------------------------------------------------------------------------
# Prometheus metrics — mcp_requests_total{client_source, tool, status}
# ---------------------------------------------------------------------------
#
# Requirement 9.6 (platform-gap-fill spec):
#
#     THE MCP_Server SHALL `client_source` değerini Prometheus metric
#     label'ı olarak expose etmelidir
#     (`mcp_requests_total{client_source="...", tool="...", status="..."}`).
#
# We use a dedicated CollectorRegistry instead of the prometheus_client
# global default registry so:
#
# * The MCP server can expose a ``/metrics`` endpoint without leaking
#   collectors registered by other libraries that may also be loaded
#   in-process (the Python default ``process_collector`` etc.).
# * Test isolation: tests that import ``main`` repeatedly do not raise
#   ``Duplicated timeseries`` errors when the module is re-imported under
#   different sys.path layouts.

_METRICS_REGISTRY: CollectorRegistry = CollectorRegistry()

#: One counter per (client_source, tool, status) triple. ``status`` is
#: ``"success"`` for any HTTP 2xx response and ``"error"`` otherwise so
#: the admin dashboard can compute success/error ratios per client.
mcp_requests_total: Counter = Counter(
    "mcp_requests_total",
    "Total MCP HTTP requests served, labelled by calling client, "
    "MCP tool name (or HTTP path when no tool is invoked) and outcome.",
    labelnames=("client_source", "tool", "status"),
    registry=_METRICS_REGISTRY,
)


def _extract_tool_from_jsonrpc_body(body: bytes) -> str | None:
    """Best-effort extraction of the MCP tool name from a JSON-RPC body.

    MCP tool calls follow JSON-RPC 2.0 with ``method == "tools/call"``
    and ``params.name`` carrying the tool name. Other JSON-RPC methods
    (``initialize``, ``tools/list``, ``ping``, ...) carry no tool, so
    we return ``None`` and the caller falls back to the JSON-RPC
    ``method`` (or HTTP path) as the metric ``tool`` label.

    The body is parsed defensively: malformed JSON, unexpected types
    or missing fields all return ``None`` rather than raising — the
    middleware must never break a request because metrics couldn't be
    extracted.
    """
    if not body:
        return None
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if method == "tools/call":
        params = payload.get("params")
        if isinstance(params, dict):
            name = params.get("name")
            if isinstance(name, str) and name:
                return name
        return None
    # Non-tool JSON-RPC methods (initialize, tools/list, ...). Use the
    # method name itself so the operator can still see traffic shape.
    if isinstance(method, str) and method:
        return method
    return None


# ---------------------------------------------------------------------------
# X-Trace-Id propagation — platform-gap-fill task 7.2 (Requirement 8.2 / 8.5)
# ---------------------------------------------------------------------------
#
# The MCP server sits at the edge of the platform: every Atlassian
# tool call from automation-worker, agent-runner-worker,
# execution-runner-worker, assistant-service, the Streamlit UI and the
# admin-dashboard-api flows through it.  To make end-to-end log
# correlation possible (Requirement 8.6 — Admin Dashboard surfaces
# all log lines for a workflow under a single ``trace_id``) every
# inbound MCP request must carry the originating ``X-Trace-Id``
# header through to:
#
#   * the request log (so Loki / structured logs are filterable
#     by trace_id);
#   * the outgoing Atlassian API calls (best-effort — the Atlassian
#     APIs themselves do not consume the header but the MCP-side
#     log lines emitted while building the response do);
#   * the response (echoed back so the caller can verify the value
#     made it through and reconcile its own per-request log).
#
# Implementation notes:
#
#   * The middleware is implemented as a bare ASGI 3.0 callable
#     (``__init__(self, app)`` + ``async __call__(self, scope, receive,
#     send)``).  We deliberately avoid taking a dependency on the
#     workspace ``observability`` library here because the MCP server
#     ships with its own ``uv``-managed virtualenv and pulling in
#     workspace path dependencies would require a Dockerfile change.
#     The contract — extract ``X-Trace-Id`` from the request,
#     generate a UUIDv7 fallback when absent, store on
#     ``scope.state``, echo on the response — is identical to the
#     workspace lib's :class:`observability.TraceMiddleware` so logs
#     correlate across every service.
#   * UUIDv7 generation follows :rfc:`9562` §5.7 (48-bit Unix-ms
#     timestamp + 4-bit version + 12 random bits + 2-bit variant +
#     62 random bits).  The format is byte-for-byte compatible with
#     the workspace ``generate_trace_id`` helper.
#   * The middleware is mounted *outermost* in
#     :meth:`AtlassianMCP.http_app` so :class:`UserTokenMiddleware`
#     and :class:`ClientSourceLoggingMiddleware` both see the
#     resolved trace_id on ``scope.state``.

_TRACE_HEADER = "x-trace-id"
_TRACE_HEADER_BYTES = _TRACE_HEADER.encode("latin-1")
_TRACE_HEADER_RESPONSE = "X-Trace-Id".encode("latin-1")

#: Structured logger for trace_id propagation events — emits one record
#: per inbound request so the operator can confirm the middleware is
#: live and trace_ids are flowing.
_trace_logger = logging.getLogger("mcp-atlassian.trace")


def _generate_trace_id() -> str:
    """Generate a UUIDv7 trace_id (RFC 9562 §5.7).

    Identical layout to :func:`observability.generate_trace_id`:

    * 48 bits — Unix milliseconds timestamp (big-endian).
    * 4 bits  — version field set to ``0b0111``.
    * 12 bits — random.
    * 2 bits  — RFC 4122 variant ``0b10``.
    * 62 bits — random.
    """

    import secrets

    unix_ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF
    rand_bytes = secrets.token_bytes(10)
    rand_a = ((rand_bytes[0] << 8) | rand_bytes[1]) & 0x0FFF
    rand_b = int.from_bytes(rand_bytes[2:], "big") & ((1 << 62) - 1)

    uuid_int = (
        (unix_ts_ms & 0xFFFFFFFFFFFF) << 80
        | (0x7 << 76)
        | (rand_a << 64)
        | (0x2 << 62)
        | rand_b
    )
    hex_str = f"{uuid_int:032x}"
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _is_valid_trace_id(value: str) -> bool:
    """Return ``True`` iff *value* is a syntactically valid UUID."""

    if not value or len(value) != 36:
        return False
    if (
        value[8] != "-"
        or value[13] != "-"
        or value[18] != "-"
        or value[23] != "-"
    ):
        return False
    hex_only = value.replace("-", "")
    if len(hex_only) != 32:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in hex_only)


class TraceMiddleware:
    """ASGI middleware that propagates ``X-Trace-Id`` end-to-end.

    Mirrors :class:`observability.TraceMiddleware` from the workspace
    lib (kept inline here so the MCP server's vendored virtualenv
    doesn't need a workspace path dependency).  For every inbound HTTP
    request the middleware:

    1. Extracts the ``X-Trace-Id`` header (case-insensitive).
    2. If the header is present and well-formed, preserves it so
       Atlassian webhook retries / cross-service relays keep the same
       trace (Requirement 8.7).
    3. Otherwise generates a fresh UUIDv7 (Requirement 8.1).
    4. Stores the resolved trace_id on ``scope.state.trace_id`` so
       :class:`ClientSourceLoggingMiddleware` and downstream MCP
       handlers can include it in structured log lines.
    5. Echoes the resolved trace_id on the response under the same
       header so the caller can reconcile its own per-request log
       record with the MCP-side log lines.

    Validates: Requirements 8.1, 8.2, 8.5, 8.7 (platform-gap-fill).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Resolve trace_id ------------------------------------------------
        inbound: str | None = None
        for raw_name, raw_value in scope.get("headers", []) or ():
            if raw_name.lower() == _TRACE_HEADER_BYTES:
                try:
                    inbound = raw_value.decode("latin-1").strip()
                except (UnicodeDecodeError, AttributeError):
                    inbound = None
                break

        if inbound and _is_valid_trace_id(inbound):
            trace_id = inbound
        else:
            if inbound:
                _trace_logger.debug(
                    "trace_middleware_invalid_inbound value=%r", inbound
                )
            trace_id = _generate_trace_id()

        # Stash on scope.state so downstream middlewares / handlers can
        # read it without re-parsing headers.
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["trace_id"] = trace_id

        trace_bytes = trace_id.encode("latin-1")

        async def _wrapped_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                filtered: list[tuple[bytes, bytes]] = [
                    (k, v)
                    for (k, v) in raw_headers
                    if k.lower() != _TRACE_HEADER_BYTES
                ]
                filtered.append((_TRACE_HEADER_RESPONSE, trace_bytes))
                message = dict(message)
                message["headers"] = filtered
            await send(message)

        await self.app(scope, receive, _wrapped_send)


class ClientSourceLoggingMiddleware:
    """ASGI middleware that extracts ``X-Client-Source`` and logs it.

    Every inbound HTTP request is annotated with the caller's
    ``client_source`` identity (injected by
    ``http_shared.make_mcp_client``). The middleware:

    1. Reads the ``X-Client-Source`` header (case-insensitive).
    2. Emits a structured log record containing ``client_source``,
       ``method``, ``path``, and ``timestamp``.
    3. Stores ``client_source`` in ``scope["state"]`` so downstream
       handlers (tool execution, audit) can reference it without
       re-parsing headers.
    4. Increments ``mcp_requests_total{client_source, tool, status}``
       once per request so the admin dashboard can graph traffic by
       client (Requirement 9.6).

    Missing or empty client-source values are still logged as
    ``"unknown"`` for observability, but MCP JSON-RPC requests are
    rejected with HTTP 400 so stateless clients cannot accidentally
    hide their origin from the admin dashboard.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract X-Client-Source from raw ASGI headers (bytes tuples).
        headers = dict(scope.get("headers", []))
        raw_client_source = headers.get(b"x-client-source")
        client_source = (
            raw_client_source.decode("latin-1").strip()
            if raw_client_source
            else _CLIENT_SOURCE_UNKNOWN
        )
        if not client_source:
            client_source = _CLIENT_SOURCE_UNKNOWN

        # Inject into scope state for downstream access.
        if "state" not in scope:
            scope["state"] = {}
        scope["state"]["client_source"] = client_source

        # The TraceMiddleware (mounted outermost) sets ``trace_id`` on
        # ``scope.state`` for us; surface it on the structured log
        # record so Loki / Grafana can correlate MCP requests with the
        # originating workflow (platform-gap-fill task 7.2,
        # Requirement 8.5).
        trace_id = scope["state"].get("trace_id", "") if isinstance(scope.get("state"), dict) else ""

        # Structured log for observability pipeline.
        method = scope.get("method", "?")
        path = scope.get("path", "?")
        _client_source_logger.info(
            "mcp_request client_source=%s method=%s path=%s trace_id=%s",
            client_source,
            method,
            path,
            trace_id,
            extra={
                "client_source": client_source,
                "method": method,
                "path": path,
                "trace_id": trace_id,
                "timestamp": time.time(),
            },
        )

        # ----------------------------------------------------------------
        # Buffer request body so we can both inspect it for the JSON-RPC
        # tool name AND replay it to the downstream ASGI app. The MCP
        # streamable-HTTP transport posts a single JSON message per
        # request, so a one-shot buffer is sufficient.
        # ----------------------------------------------------------------
        body_chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.request":
                body_chunks.append(message.get("body", b"") or b"")
                more_body = message.get("more_body", False)
            elif message["type"] == "http.disconnect":
                # Client disconnected before sending the full body. Pass
                # the message through so the app can clean up; metric
                # records this as an error after the response phase.
                more_body = False
                body_chunks.append(b"")
                break
            else:
                # Unknown message type — forward unchanged by aborting
                # the buffering loop. The app will re-receive via the
                # wrapped receive below.
                break

        buffered_body = b"".join(body_chunks)
        tool_label = (
            _extract_tool_from_jsonrpc_body(buffered_body) or scope.get("path") or "?"
        )

        if client_source == _CLIENT_SOURCE_UNKNOWN and str(method).upper() == "POST":
            try:
                mcp_requests_total.labels(
                    client_source=_CLIENT_SOURCE_UNKNOWN,
                    tool=str(tool_label),
                    status="error",
                ).inc()
            except Exception:  # pragma: no cover - metric accounting must never break a request
                logger.exception(
                    "Failed to record mcp_requests_total counter "
                    "(client_source=%s, tool=%s, status=error)",
                    _CLIENT_SOURCE_UNKNOWN,
                    tool_label,
                )
            payload = json.dumps(
                {
                    "error": "missing_client_source",
                    "detail": "X-Client-Source header is required.",
                }
            ).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})
            return

        # Replay the buffered body to the wrapped app exactly once.
        replayed = False

        async def _wrapped_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {
                    "type": "http.request",
                    "body": buffered_body,
                    "more_body": False,
                }
            # After replay, propagate further messages (typically
            # ``http.disconnect``) from the real transport.
            return await receive()

        # ----------------------------------------------------------------
        # Wrap ``send`` so we can observe the response status code and
        # record the Prometheus metric exactly once.
        # ----------------------------------------------------------------
        status_code: int | None = None

        async def _wrapped_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 0)) or None
            await send(message)

        try:
            await self.app(scope, _wrapped_receive, _wrapped_send)
        finally:
            status_label = (
                "success"
                if status_code is not None and 200 <= status_code < 300
                else "error"
            )
            try:
                mcp_requests_total.labels(
                    client_source=client_source,
                    tool=str(tool_label),
                    status=status_label,
                ).inc()
            except Exception:  # pragma: no cover - metric accounting must never break a request
                logger.exception(
                    "Failed to record mcp_requests_total counter "
                    "(client_source=%s, tool=%s, status=%s)",
                    client_source,
                    tool_label,
                    status_label,
                )


def _render_metrics() -> tuple[bytes, str]:
    """Serialise the MCP-server metric registry in Prometheus exposition.

    Wired by the ``/metrics`` Starlette route registered at the bottom of
    this module.
    """
    return generate_latest(_METRICS_REGISTRY), CONTENT_TYPE_LATEST


def _sanitize_schema_for_compatibility(tool: MCPTool) -> MCPTool:
    """Sanitize tool inputSchema for AI platform compatibility.

    Collapses simple nullable ``anyOf`` unions that Pydantic v2 generates
    for ``T | None`` into a plain ``{"type": T}`` property.  This fixes
    Vertex AI / Google ADK rejecting ``anyOf`` alongside ``default`` or
    ``description`` fields (issues #640, #733).

    The transform is intentionally conservative — it only flattens unions
    of exactly ``[{"type": <primitive>}, {"type": "null"}]`` so that
    complex / nested schemas are left untouched.

    Note: Only top-level ``properties`` are processed.  Nested schemas
    (e.g. ``items`` of arrays or ``properties`` of sub-objects) are not
    walked.  This is sufficient for current tool definitions; extend if
    nested ``anyOf`` patterns appear in the future.

    Args:
        tool: The MCP tool whose inputSchema will be sanitized in-place.

    Returns:
        The same MCPTool instance (mutated) for chaining convenience.
    """
    schema = tool.inputSchema
    if not schema or not isinstance(schema, dict):
        return tool

    properties = schema.get("properties")
    if not properties or not isinstance(properties, dict):
        return tool

    for _prop_name, prop_def in properties.items():
        if not isinstance(prop_def, dict):
            continue

        any_of = prop_def.get("anyOf")
        if not any_of or not isinstance(any_of, list):
            continue

        # Only flatten simple nullable unions: [{"type": T}, {"type": "null"}]
        non_null = [v for v in any_of if v != {"type": "null"}]
        null_present = any(v == {"type": "null"} for v in any_of)

        if null_present and len(non_null) == 1 and "type" in non_null[0]:
            # Collapse: pull the real type up, drop anyOf
            resolved_type = non_null[0]["type"]
            prop_def.pop("anyOf")
            prop_def["type"] = resolved_type

    return tool


async def health_check(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@asynccontextmanager
async def main_lifespan(app: FastMCP[MainAppContext]) -> AsyncIterator[dict[str, Any]]:
    logger.info("Main Atlassian MCP server lifespan starting...")
    services = get_available_services()
    read_only = is_read_only_mode()
    enabled_tools = get_enabled_tools()
    enabled_toolsets = get_enabled_toolsets()

    loaded_jira_config: JiraConfig | None = None
    loaded_confluence_config: ConfluenceConfig | None = None
    loaded_bitbucket_config: BitbucketConfig | None = None

    if services.get("jira"):
        try:
            jira_config = JiraConfig.from_env()
            if jira_config.is_auth_configured():
                loaded_jira_config = jira_config
                logger.info(
                    "Jira configuration loaded and authentication is configured."
                )
            else:
                logger.warning(
                    "Jira URL found, but authentication is not fully configured. Jira tools will be unavailable."
                )
        except Exception as e:
            logger.error(f"Failed to load Jira configuration: {e}", exc_info=True)

    if services.get("confluence"):
        try:
            confluence_config = ConfluenceConfig.from_env()
            if confluence_config.is_auth_configured():
                loaded_confluence_config = confluence_config
                logger.info(
                    "Confluence configuration loaded and authentication is configured."
                )
            else:
                logger.warning(
                    "Confluence URL found, but authentication is not fully configured. Confluence tools will be unavailable."
                )
        except Exception as e:
            logger.error(f"Failed to load Confluence configuration: {e}", exc_info=True)

    if services.get("bitbucket"):
        try:
            bitbucket_config = BitbucketConfig.from_env()
            if bitbucket_config.is_auth_configured():
                loaded_bitbucket_config = bitbucket_config
                logger.info(
                    "Bitbucket configuration loaded and authentication is configured."
                )
            else:
                logger.warning(
                    "Bitbucket URL found, but authentication is not fully configured. Bitbucket tools will be unavailable."
                )
        except Exception as e:
            logger.error(f"Failed to load Bitbucket configuration: {e}", exc_info=True)

    app_context = MainAppContext(
        full_jira_config=loaded_jira_config,
        full_confluence_config=loaded_confluence_config,
        full_bitbucket_config=loaded_bitbucket_config,
        read_only=read_only,
        enabled_tools=enabled_tools,
        enabled_toolsets=enabled_toolsets,
    )
    logger.info(f"Read-only mode: {'ENABLED' if read_only else 'DISABLED'}")
    logger.info(f"Enabled tools filter: {enabled_tools or 'All tools enabled'}")
    logger.info(f"Enabled toolsets filter: {sorted(enabled_toolsets)}")

    try:
        yield {"app_lifespan_context": app_context}
    except Exception as e:
        logger.error(f"Error during lifespan: {e}", exc_info=True)
        raise
    finally:
        logger.info("Main Atlassian MCP server lifespan shutting down...")
        # Perform any necessary cleanup here
        try:
            # Close any open connections if needed
            if loaded_jira_config:
                logger.debug("Cleaning up Jira resources...")
            if loaded_confluence_config:
                logger.debug("Cleaning up Confluence resources...")
            if loaded_bitbucket_config:
                logger.debug("Cleaning up Bitbucket resources...")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
        logger.info("Main Atlassian MCP server lifespan shutdown complete.")


class AtlassianMCP(FastMCP[MainAppContext]):
    """Custom FastMCP server class for Atlassian integration with tool filtering."""

    _active_streamable_http_path: str | None = None

    @staticmethod
    def _normalize_http_path(path: str) -> str:
        normalized_path = path.strip()
        if not normalized_path:
            return "/"
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/")
        return normalized_path or "/"

    def get_streamable_http_path(self) -> str:
        if self._active_streamable_http_path:
            return self._active_streamable_http_path
        return self._normalize_http_path(fastmcp_settings.streamable_http_path)

    async def _list_tools_mcp(self) -> list[MCPTool]:
        # Filter tools based on enabled_tools, read_only mode, and service configuration from the lifespan context.
        req_context = self._mcp_server.request_context
        if req_context is None or req_context.lifespan_context is None:
            logger.warning(
                "Lifespan context not available during _list_tools_mcp call."
            )
            return []

        lifespan_ctx_dict = req_context.lifespan_context
        app_lifespan_state: MainAppContext | None = (
            lifespan_ctx_dict.get("app_lifespan_context")
            if isinstance(lifespan_ctx_dict, dict)
            else None
        )
        read_only = (
            getattr(app_lifespan_state, "read_only", False)
            if app_lifespan_state
            else False
        )
        enabled_tools_filter = (
            getattr(app_lifespan_state, "enabled_tools", None)
            if app_lifespan_state
            else None
        )
        enabled_toolsets_filter: set[str] | None = (
            getattr(app_lifespan_state, "enabled_toolsets", None)
            if app_lifespan_state
            else None
        )

        header_based_services = {"jira": False, "confluence": False, "bitbucket": False}
        request = getattr(req_context, "request", None)
        if request is not None:
            service_headers = getattr(request.state, "atlassian_service_headers", {})
            if service_headers:
                header_based_services = get_available_services(service_headers)
                logger.debug(
                    f"Header-based service availability: {header_based_services}"
                )

        logger.debug(
            f"_list_tools_mcp: read_only={read_only}, enabled_tools_filter={enabled_tools_filter}, header_services={header_based_services}"
        )

        all_tools: dict[str, FastMCPTool] = await self.get_tools()
        logger.debug(
            f"Aggregated {len(all_tools)} tools before filtering: {list(all_tools.keys())}"
        )

        filtered_tools: list[MCPTool] = []
        for registered_name, tool_obj in all_tools.items():
            tool_tags = tool_obj.tags

            if not should_include_tool_by_toolset(tool_tags, enabled_toolsets_filter):
                logger.debug(
                    f"Excluding tool '{registered_name}' (toolset not enabled)"
                )
                continue

            if not should_include_tool(registered_name, enabled_tools_filter):
                logger.debug(f"Excluding tool '{registered_name}' (not enabled)")
                continue

            if tool_obj and read_only and "write" in tool_tags:
                logger.debug(
                    f"Excluding tool '{registered_name}' due to read-only mode and 'write' tag"
                )
                continue

            # Exclude Jira/Confluence/Bitbucket tools if config is not fully authenticated
            is_jira_tool = "jira" in tool_tags
            is_confluence_tool = "confluence" in tool_tags
            is_bitbucket_tool = "bitbucket" in tool_tags
            service_configured_and_available = True
            if app_lifespan_state:
                jira_available = (
                    app_lifespan_state.full_jira_config is not None
                ) or header_based_services.get("jira", False)
                confluence_available = (
                    app_lifespan_state.full_confluence_config is not None
                ) or header_based_services.get("confluence", False)
                bitbucket_available = (
                    app_lifespan_state.full_bitbucket_config is not None
                ) or header_based_services.get("bitbucket", False)

                if is_jira_tool and not jira_available:
                    logger.debug(
                        f"Excluding Jira tool '{registered_name}' as Jira configuration/authentication is incomplete and no header-based auth available."
                    )
                    service_configured_and_available = False
                if is_confluence_tool and not confluence_available:
                    logger.debug(
                        f"Excluding Confluence tool '{registered_name}' as Confluence configuration/authentication is incomplete and no header-based auth available."
                    )
                    service_configured_and_available = False
                if is_bitbucket_tool and not bitbucket_available:
                    logger.debug(
                        f"Excluding Bitbucket tool '{registered_name}' as Bitbucket configuration/authentication is incomplete and no header-based auth available."
                    )
                    service_configured_and_available = False
            elif is_jira_tool or is_confluence_tool or is_bitbucket_tool:
                jira_available = header_based_services.get("jira", False)
                confluence_available = header_based_services.get("confluence", False)
                bitbucket_available = header_based_services.get("bitbucket", False)

                if is_jira_tool and not jira_available:
                    logger.debug(
                        f"Excluding Jira tool '{registered_name}' as no Jira authentication available."
                    )
                    service_configured_and_available = False
                if is_confluence_tool and not confluence_available:
                    logger.debug(
                        f"Excluding Confluence tool '{registered_name}' as no Confluence authentication available."
                    )
                    service_configured_and_available = False
                if is_bitbucket_tool and not bitbucket_available:
                    logger.debug(
                        f"Excluding Bitbucket tool '{registered_name}' as no Bitbucket authentication available."
                    )
                    service_configured_and_available = False

            if not service_configured_and_available:
                continue

            mcp_tool = tool_obj.to_mcp_tool(name=registered_name)
            _sanitize_schema_for_compatibility(mcp_tool)
            filtered_tools.append(mcp_tool)

        logger.debug(
            f"_list_tools_mcp: Total tools after filtering: {len(filtered_tools)}"
        )
        return filtered_tools

    def http_app(
        self,
        path: str | None = None,
        middleware: list[Middleware] | None = None,
        json_response: bool | None = None,
        stateless_http: bool | None = None,
        transport: Literal["http", "streamable-http", "sse"] = "streamable-http",
        event_store: EventStore | None = None,
        retry_interval: int | None = None,
    ) -> StarletteWithLifespan:
        final_path = path
        if transport == "streamable-http":
            configured_path = path or fastmcp_settings.streamable_http_path
            final_path = self._normalize_http_path(configured_path)
            self._active_streamable_http_path = final_path

        user_token_mw = Middleware(UserTokenMiddleware, mcp_server_ref=self)
        client_source_mw = Middleware(ClientSourceLoggingMiddleware)
        # platform-gap-fill task 7.2 — :class:`TraceMiddleware` extracts
        # / generates the ``X-Trace-Id`` header and binds it onto
        # ``scope.state`` so :class:`ClientSourceLoggingMiddleware`
        # (and downstream tool handlers) can include the trace_id in
        # their structured log records.  Mounted FIRST so it is the
        # outermost layer on the inbound side and the innermost on the
        # outbound side — i.e. it sees every request and stamps the
        # response header on every reply (Requirement 8.2 / 8.5 / 8.7).
        trace_mw = Middleware(TraceMiddleware)
        final_middleware_list = [trace_mw, client_source_mw, user_token_mw]
        if middleware:
            final_middleware_list.extend(middleware)
        app = super().http_app(
            path=final_path,
            middleware=final_middleware_list,
            json_response=json_response,
            stateless_http=stateless_http,
            transport=transport,
            event_store=event_store,
            retry_interval=retry_interval,
        )
        return app


token_validation_cache: TTLCache[
    int, tuple[bool, str | None, JiraFetcher | None, ConfluenceFetcher | None]
] = TTLCache(maxsize=100, ttl=300)


class UserTokenMiddleware:
    """ASGI-compliant middleware to extract Atlassian user tokens/credentials.

    Based on PR #700 by @isaacpalomero - fixes ASGI protocol violations that caused
    server crashes when MCP clients disconnect during HTTP requests.
    """

    def __init__(
        self, app: ASGIApp, mcp_server_ref: Optional["AtlassianMCP"] = None
    ) -> None:
        self.app = app
        self.mcp_server_ref = mcp_server_ref
        if not self.mcp_server_ref:
            logger.warning(
                "UserTokenMiddleware initialized without mcp_server_ref. "
                "Path matching for MCP endpoint might fail if settings are needed."
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Pass through non-HTTP requests directly per ASGI spec
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # According to ASGI spec, middleware should copy scope when modifying it
        scope_copy: Scope = dict(scope)

        # Ensure state exists in scope - this is where Starlette stores request state
        if "state" not in scope_copy:
            scope_copy["state"] = {}

        # Initialize default authentication state (only initialize fields that should always exist)
        # Note: user_atlassian_token and user_atlassian_auth_type are NOT initialized
        # They are only set when present, so hasattr() checks work correctly
        scope_copy["state"]["user_atlassian_email"] = None
        scope_copy["state"]["user_atlassian_cloud_id"] = None
        scope_copy["state"]["auth_validation_error"] = None

        logger.debug(
            f"UserTokenMiddleware: Processing {scope_copy.get('method', 'UNKNOWN')} "
            f"{scope_copy.get('path', 'UNKNOWN')}"
        )

        # Skip auth header processing if IGNORE_HEADER_AUTH is set
        # (useful for GCP Cloud Run / AWS ALB that inject Authorization headers)
        ignore_header_auth = is_env_truthy("IGNORE_HEADER_AUTH")

        # Only process authentication for our MCP endpoint
        if (
            not ignore_header_auth
            and self.mcp_server_ref
            and self._should_process_auth(scope_copy)
        ):
            self._process_authentication_headers(scope_copy)

        # Create wrapped send function to handle client disconnections gracefully
        async def safe_send(message: Message) -> None:
            try:
                await send(message)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                # Client disconnected - log but don't propagate to avoid ASGI violations
                logger.debug(
                    f"Client disconnected during response: {type(e).__name__}: {e}"
                )
                # Don't re-raise - this prevents the ASGI protocol violation
                return
            except Exception:
                # Re-raise unexpected errors
                raise

        # Check for auth errors and return 401 before calling app
        auth_error = scope_copy["state"].get("auth_validation_error")
        if auth_error:
            logger.warning(f"Authentication failed: {auth_error}")
            await self._send_json_error_response(safe_send, 401, auth_error)
            return  # Don't call self.app - request is rejected

        # Call the next application with modified scope and safe send wrapper
        await self.app(scope_copy, receive, safe_send)

    async def _send_json_error_response(
        self, send: Send, status_code: int, error_message: str
    ) -> None:
        """Send a JSON error response via ASGI protocol.

        Args:
            send: ASGI send callable (should be safe_send wrapper).
            status_code: HTTP status code (e.g., 401).
            error_message: Error message to include in JSON body.
        """
        body = json.dumps({"error": error_message}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _should_process_auth(self, scope: Scope) -> bool:
        """Check if this request should be processed for authentication."""
        if not self.mcp_server_ref or scope.get("method") != "POST":
            return False

        try:
            mcp_path = self.mcp_server_ref.get_streamable_http_path()
            request_path = AtlassianMCP._normalize_http_path(scope.get("path", ""))
            return request_path == mcp_path
        except (AttributeError, ValueError) as e:
            logger.warning(f"Error checking auth path: {e}")
            return False

    def _process_authentication_headers(self, scope: Scope) -> None:
        """Process authentication headers and store in scope state."""
        try:
            # Parse headers from scope (headers are byte tuples per ASGI spec)
            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization")
            cloud_id_header = headers.get(b"x-atlassian-cloud-id")

            # Convert bytes to strings (ASGI headers are always bytes)
            auth_header_str = auth_header.decode("latin-1") if auth_header else None
            cloud_id_str = (
                cloud_id_header.decode("latin-1") if cloud_id_header else None
            )

            # Extract additional Atlassian service headers for service availability detection.
            # The MCP server is stateless, so every request may carry a
            # different Jira/Confluence/Bitbucket credential bundle.
            def _header_text(name: bytes) -> str | None:
                raw = headers.get(name)
                if raw is None:
                    return None
                value = raw.decode("latin-1").strip()
                return value or None

            jira_url_str = _header_text(b"x-atlassian-jira-url")
            jira_username_str = _header_text(b"x-atlassian-jira-username")
            jira_token_str = _header_text(b"x-atlassian-jira-personal-token")
            jira_api_token_str = _header_text(b"x-atlassian-jira-api-token")

            confluence_url_str = _header_text(b"x-atlassian-confluence-url")
            confluence_username_str = _header_text(
                b"x-atlassian-confluence-username"
            )
            confluence_token_str = _header_text(
                b"x-atlassian-confluence-personal-token"
            )
            confluence_api_token_str = _header_text(
                b"x-atlassian-confluence-api-token"
            )

            bitbucket_url_str = _header_text(b"x-atlassian-bitbucket-url")
            bitbucket_username_str = _header_text(b"x-atlassian-bitbucket-username")
            bitbucket_token_str = _header_text(
                b"x-atlassian-bitbucket-personal-token"
            )
            bitbucket_cloud_token_str = _header_text(
                b"x-atlassian-bitbucket-cloud-access-token"
            )
            bitbucket_app_password_str = _header_text(
                b"x-atlassian-bitbucket-app-password"
            )

            # Validate URLs to prevent SSRF
            if jira_url_str:
                ssrf_error = validate_url_for_ssrf(jira_url_str)
                if ssrf_error:
                    scope["state"]["auth_validation_error"] = (
                        f"Forbidden: Invalid Jira URL - {ssrf_error}"
                    )
                    return

            if confluence_url_str:
                ssrf_error = validate_url_for_ssrf(confluence_url_str)
                if ssrf_error:
                    scope["state"]["auth_validation_error"] = (
                        f"Forbidden: Invalid Confluence URL - {ssrf_error}"
                    )
                    return

            if bitbucket_url_str:
                ssrf_error = validate_url_for_ssrf(bitbucket_url_str)
                if ssrf_error:
                    scope["state"]["auth_validation_error"] = (
                        f"Forbidden: Invalid Bitbucket URL - {ssrf_error}"
                    )
                    return

            service_headers: dict[str, str] = {}

            def _put_service_header(key: str, value: str | None) -> None:
                if value:
                    service_headers[key] = value

            _put_service_header("X-Atlassian-Jira-Url", jira_url_str)
            _put_service_header("X-Atlassian-Jira-Username", jira_username_str)
            _put_service_header("X-Atlassian-Jira-Personal-Token", jira_token_str)
            _put_service_header("X-Atlassian-Jira-Api-Token", jira_api_token_str)
            _put_service_header("X-Atlassian-Confluence-Url", confluence_url_str)
            _put_service_header(
                "X-Atlassian-Confluence-Username", confluence_username_str
            )
            _put_service_header(
                "X-Atlassian-Confluence-Personal-Token", confluence_token_str
            )
            _put_service_header(
                "X-Atlassian-Confluence-Api-Token", confluence_api_token_str
            )
            _put_service_header("X-Atlassian-Bitbucket-Url", bitbucket_url_str)
            _put_service_header(
                "X-Atlassian-Bitbucket-Username", bitbucket_username_str
            )
            _put_service_header(
                "X-Atlassian-Bitbucket-Personal-Token", bitbucket_token_str
            )
            _put_service_header(
                "X-Atlassian-Bitbucket-Cloud-Access-Token",
                bitbucket_cloud_token_str,
            )
            _put_service_header(
                "X-Atlassian-Bitbucket-App-Password",
                bitbucket_app_password_str,
            )

            scope["state"]["atlassian_service_headers"] = service_headers
            if service_headers:
                logger.debug(
                    f"UserTokenMiddleware: Extracted service headers: {list(service_headers.keys())}"
                )

            # Log mcp-session-id for debugging
            mcp_session_id = headers.get(b"mcp-session-id")
            if mcp_session_id:
                session_id_str = mcp_session_id.decode("latin-1")
                logger.debug(
                    f"UserTokenMiddleware: MCP-Session-ID header found: {session_id_str}"
                )

            logger.debug(
                f"UserTokenMiddleware: Processing auth for {scope.get('path')}, "
                f"AuthHeader present: {bool(auth_header_str)}, "
                f"CloudId present: {bool(cloud_id_str)}"
            )

            # Process Cloud ID
            if cloud_id_str and cloud_id_str.strip():
                scope["state"]["user_atlassian_cloud_id"] = cloud_id_str.strip()
                logger.debug(
                    f"UserTokenMiddleware: Extracted cloudId: {cloud_id_str.strip()}"
                )

            # Process Authorization header
            if auth_header_str:
                self._parse_auth_header(auth_header_str, scope)
            else:
                logger.debug("UserTokenMiddleware: No Authorization header provided")
                # If service headers are present without Authorization header, set PAT auth type
                complete_service_header_auth = (
                    (
                        jira_url_str
                        and (jira_token_str or (jira_username_str and jira_api_token_str))
                    )
                    or (
                        confluence_url_str
                        and (
                            confluence_token_str
                            or (confluence_username_str and confluence_api_token_str)
                        )
                    )
                    or (
                        bitbucket_url_str
                        and (
                            bitbucket_token_str
                            or bitbucket_cloud_token_str
                            or (bitbucket_username_str and bitbucket_app_password_str)
                        )
                    )
                )
                if service_headers and complete_service_header_auth:
                    scope["state"]["user_atlassian_auth_type"] = "pat"
                    scope["state"]["user_atlassian_email"] = None
                    logger.debug(
                        "UserTokenMiddleware: Header-based authentication detected. Setting PAT auth type."
                    )

        except Exception as e:
            logger.error(f"Error processing authentication headers: {e}", exc_info=True)
            scope["state"]["auth_validation_error"] = "Authentication processing error"

    def _parse_auth_header(self, auth_header: str, scope: Scope) -> None:
        """Parse the Authorization header and store credentials in scope state."""
        # Check prefix BEFORE stripping to preserve "Bearer " / "Token " matching
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()  # Remove "Bearer " prefix and strip token
            if not token:
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Empty Bearer token"
                )
            else:
                scope["state"]["user_atlassian_token"] = token
                scope["state"]["user_atlassian_auth_type"] = "oauth"
                logger.debug(
                    "UserTokenMiddleware: Bearer token extracted (masked): "
                    f"...{mask_sensitive(token, 8)}"
                )

        elif auth_header.startswith("Token "):
            token = auth_header[6:].strip()  # Remove "Token " prefix and strip token
            if not token:
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Empty Token (PAT)"
                )
            else:
                scope["state"]["user_atlassian_token"] = token
                scope["state"]["user_atlassian_auth_type"] = "pat"
                logger.debug(
                    "UserTokenMiddleware: PAT token extracted (masked): "
                    f"...{mask_sensitive(token, 8)}"
                )

        elif auth_header.startswith("Basic "):
            encoded = auth_header[6:].strip()
            if not encoded:
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Empty Basic auth credentials"
                )
                return
            try:
                decoded = base64.b64decode(encoded).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as e:
                logger.warning(f"Failed to decode Basic auth: {e}")
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Invalid Basic auth encoding"
                )
                return
            if ":" not in decoded:
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Invalid Basic auth format. "
                    "Expected 'email:api_token'"
                )
                return
            email, api_token = decoded.split(":", 1)
            if not email or not api_token:
                scope["state"]["auth_validation_error"] = (
                    "Unauthorized: Email or API token is empty"
                )
                return
            scope["state"]["user_atlassian_email"] = email
            scope["state"]["user_atlassian_api_token"] = api_token
            scope["state"]["user_atlassian_auth_type"] = "basic"
            scope["state"]["user_atlassian_token"] = None
            logger.debug(
                f"UserTokenMiddleware: Basic auth extracted for email: {email}"
            )

        elif auth_header.strip():
            # Non-empty but unsupported auth type
            auth_value = auth_header.strip()
            auth_type = auth_value.split(" ", 1)[0] if " " in auth_value else auth_value
            logger.warning(f"Unsupported Authorization type: {auth_type}")
            scope["state"]["auth_validation_error"] = (
                "Unauthorized: Only 'Bearer <OAuthToken>', "
                "'Token <PAT>', or 'Basic <base64(email:api_token)>' "
                "types are supported."
            )
        else:
            # Empty or whitespace-only
            scope["state"]["auth_validation_error"] = (
                "Unauthorized: Empty Authorization header"
            )


def _get_allowed_redirect_uris() -> list[str] | None:
    raw = os.getenv("ATLASSIAN_OAUTH_ALLOWED_CLIENT_REDIRECT_URIS")
    parsed = parse_env_list(raw)
    if parsed is None:
        return DEFAULT_ALLOWED_REDIRECT_URIS
    return parsed


def _get_allowed_grant_types() -> list[str]:
    raw = os.getenv("ATLASSIAN_OAUTH_ALLOWED_GRANT_TYPES")
    parsed = parse_env_list(raw)
    if parsed is None:
        return DEFAULT_ALLOWED_GRANT_TYPES
    if not parsed:
        logger.warning(
            "ATLASSIAN_OAUTH_ALLOWED_GRANT_TYPES is empty; defaulting to %s",
            DEFAULT_ALLOWED_GRANT_TYPES,
        )
        return DEFAULT_ALLOWED_GRANT_TYPES
    return parsed


def _resolve_upstream_oauth_endpoints(instance_url: str) -> tuple[str, str]:
    parsed_host = (urlparse(instance_url).hostname or "").lower()
    is_cloud = (
        is_atlassian_cloud_url(instance_url) or parsed_host == "auth.atlassian.com"
    )

    if is_cloud:
        return CLOUD_AUTHORIZE_URL, CLOUD_TOKEN_URL

    base_url = instance_url.rstrip("/")
    return f"{base_url}{DC_AUTHORIZE_PATH}", f"{base_url}{DC_TOKEN_PATH}"


def _is_cloud_instance(instance_url: str) -> bool:
    parsed_host = (urlparse(instance_url).hostname or "").lower()
    return is_atlassian_cloud_url(instance_url) or parsed_host == "auth.atlassian.com"


def _build_auth_provider() -> HardenedOAuthProxy | None:
    """Create an opt-in OAuth proxy auth provider with DCR + discovery support."""
    if not is_env_truthy(OAUTH_PROXY_ENABLE_ENV, "false"):
        logger.info(
            "OAuth proxy auth provider disabled; set %s=true to enable DCR/proxy routes.",
            OAUTH_PROXY_ENABLE_ENV,
        )
        return None

    instance_url = (
        os.getenv("ATLASSIAN_OAUTH_INSTANCE_URL")
        or os.getenv("JIRA_URL")
        or os.getenv("CONFLUENCE_URL")
    )
    client_id = (
        os.getenv("ATLASSIAN_OAUTH_CLIENT_ID")
        or os.getenv("JIRA_OAUTH_CLIENT_ID")
        or os.getenv("CONFLUENCE_OAUTH_CLIENT_ID")
    )
    client_secret = (
        os.getenv("ATLASSIAN_OAUTH_CLIENT_SECRET")
        or os.getenv("JIRA_OAUTH_CLIENT_SECRET")
        or os.getenv("CONFLUENCE_OAUTH_CLIENT_SECRET")
    )
    redirect_uri = os.getenv("ATLASSIAN_OAUTH_REDIRECT_URI")
    scope_env = os.getenv("ATLASSIAN_OAUTH_SCOPE", "")

    if not all([instance_url, client_id, client_secret, redirect_uri]):
        logger.warning(
            "OAuth proxy requested but required vars are missing. "
            "Need instance URL + client credentials + redirect URI."
        )
        return None

    scopes = [s for part in scope_env.replace(",", " ").split() if (s := part)]
    is_cloud = _is_cloud_instance(instance_url)
    upstream_authorize, upstream_token = _resolve_upstream_oauth_endpoints(instance_url)

    parsed_redirect = urlparse(redirect_uri)
    raw_redirect_path = parsed_redirect.path or "/callback"

    base_url = os.getenv("PUBLIC_BASE_URL")
    if not base_url and parsed_redirect.scheme and parsed_redirect.netloc:
        redirect_dir = raw_redirect_path.rsplit("/", 1)[0]
        base_url = f"{parsed_redirect.scheme}://{parsed_redirect.netloc}{redirect_dir}"

    if not base_url:
        host = os.getenv("HOST", DEFAULT_HOST)
        port = os.getenv("PORT", "3000")
        host_for_url = "localhost" if host in (DEFAULT_HOST, "127.0.0.1") else host
        base_url = f"http://{host_for_url}:{port}"

    base_path = urlparse(base_url).path.rstrip("/")
    redirect_path = raw_redirect_path
    if base_path:
        if redirect_path == base_path:
            redirect_path = "/"
        elif redirect_path.startswith(base_path + "/"):
            redirect_path = redirect_path[len(base_path) :]

    if not redirect_path.startswith("/"):
        redirect_path = f"/{redirect_path}"

    allowed_client_redirect_uris = _get_allowed_redirect_uris()
    allowed_grant_types = _get_allowed_grant_types()
    require_consent = is_env_truthy("ATLASSIAN_OAUTH_REQUIRE_CONSENT", "true")
    verifier = AtlassianOpaqueTokenVerifier(required_scopes=scopes)

    return HardenedOAuthProxy(
        upstream_authorization_endpoint=upstream_authorize,
        upstream_token_endpoint=upstream_token,
        upstream_client_id=client_id,
        upstream_client_secret=client_secret,
        token_verifier=verifier,
        base_url=base_url,
        redirect_path=redirect_path,
        allowed_client_redirect_uris=allowed_client_redirect_uris,
        valid_scopes=scopes or None,
        allowed_grant_types=allowed_grant_types,
        forced_scopes=scopes or None,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
        extra_authorize_params=(
            {"audience": "api.atlassian.com", "prompt": "consent"} if is_cloud else None
        ),
        client_storage=build_oauth_client_storage_from_env(),
        require_authorization_consent=require_consent,
    )


main_mcp = AtlassianMCP(
    name="Atlassian MCP",
    lifespan=main_lifespan,
    auth=_build_auth_provider(),
)
main_mcp.mount(jira_mcp, "jira")
main_mcp.mount(confluence_mcp, "confluence")
main_mcp.mount(bitbucket_mcp, "bitbucket")


@main_mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def _health_check_route(request: Request) -> JSONResponse:
    return await health_check(request)


@main_mcp.custom_route("/metrics", methods=["GET"], include_in_schema=False)
async def _metrics_route(request: Request) -> Response:
    """Prometheus exposition endpoint (Requirement 9.6).

    Serves the ``mcp_requests_total{client_source, tool, status}``
    counter (and any other collectors registered on the MCP-server
    private registry) so the admin dashboard / Prometheus scrape job
    can graph traffic by ``client_source``.
    """
    body, content_type = _render_metrics()
    return Response(content=body, media_type=content_type)


logger.info("Added /healthz endpoint for Kubernetes probes")
