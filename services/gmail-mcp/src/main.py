"""Read-only Gmail MCP service.

This service talks to the Gmail REST API directly. In shared deployments it
expects a per-user Vault credential ref for mailbox access; service-local env
user tokens are a disabled local-dev fallback. Only read-only tools are
registered.
"""

from __future__ import annotations

import base64
from email.utils import parsedate_to_datetime
import html
import os
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


PROVIDER = os.environ.get("MAIL_MCP_PROVIDER", "gmail").strip().lower() or "gmail"
READ_ONLY = os.environ.get("MAIL_MCP_READ_ONLY", "true").strip().lower() != "false"
OAUTH_MODEL = os.environ.get("MAIL_MCP_OAUTH_MODEL", "per_user_vault").strip()
ALLOW_ENV_USER_TOKEN = (
    os.environ.get("MAIL_MCP_ALLOW_ENV_USER_TOKEN", "false").strip().lower()
    in {"1", "true", "yes"}
)
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URL = os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
GMAIL_API_BASE_URL = os.environ.get(
    "GMAIL_API_BASE_URL",
    "https://gmail.googleapis.com/gmail/v1",
).rstrip("/")
VAULT_ADDR = os.environ.get("VAULT_ADDR", "").rstrip("/")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "")
VAULT_KV_MOUNT = os.environ.get("VAULT_KV_MOUNT", "secret").strip("/") or "secret"
VAULT_BACKEND = os.environ.get("VAULT_BACKEND", "hashicorp").strip().lower() or "hashicorp"
MAX_LIMIT = 25
REQUEST_TIMEOUT = 20.0
BODY_CHAR_LIMIT = 4000
SNIPPET_CHAR_LIMIT = 500
HEADER_CHAR_LIMIT = 500

_TOKEN_CACHE: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_USER_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_VAULT_CLIENT: Any | None = None
_WRITE_TOOL_TOKENS = frozenset(
    {
        "archive",
        "compose",
        "create",
        "delete",
        "draft",
        "forward",
        "label",
        "mark",
        "modify",
        "move",
        "reply",
        "send",
        "trash",
        "update",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[^\s'\"&]{8,}"
    ),
    re.compile(r"\bya29\.[A-Za-z0-9._-]{12,}"),
    re.compile(r"\b1//[A-Za-z0-9._-]{12,}"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)


class GmailMcpError(RuntimeError):
    """Raised for auth/API errors that should be returned as JSON-RPC errors."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or lowered.startswith("your-") or lowered in {"changeme", "todo"}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": True,
        },
        "annotations": {"readOnlyHint": True},
    }


TOOLS = [
    _tool(
        "gmail_list_messages",
        "List recent Gmail messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "gmail_list_unread_messages",
        "List unread Gmail messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "gmail_search_messages",
        "Search Gmail messages by query, sender, or subject.",
        {
            "query": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
    ),
    _tool(
        "gmail_get_message",
        "Get one Gmail message by id.",
        {
            "message_id": {"type": "string"},
            "id": {"type": "string"},
            "include_body": {"type": "boolean"},
        },
    ),
    _tool(
        "gmail_get_latest_message",
        "Get the latest Gmail message with full details without requiring the caller to know its id.",
        {
            "offset": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            "inbox": {"type": "boolean"},
            "unread": {"type": "boolean"},
            "query": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "include_body": {"type": "boolean"},
        },
    ),
    _tool(
        "gmail_list_drafts",
        "List Gmail drafts without sending or modifying anything.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "gmail_get_latest_draft",
        "Get the latest Gmail draft with full details without sending or modifying anything.",
        {
            "offset": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            "include_body": {"type": "boolean"},
        },
    ),
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


app = FastAPI(title="gmail-mcp", version="0.2.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "provider": PROVIDER,
        "read_only": READ_ONLY,
        "per_user_vault_enabled": OAUTH_MODEL == "per_user_vault",
        "platform_oauth_client_configured": _gmail_platform_configured(),
        "env_user_token_enabled": ALLOW_ENV_USER_TOKEN,
        "oauth_model": OAUTH_MODEL,
        "token_owner": "gmail-mcp",
        "vault_backend": VAULT_BACKEND,
    }


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _jsonrpc_error(None, -32700, "Invalid JSON body")

    method = payload.get("method")
    request_id = payload.get("id")

    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})

    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        tool_name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if tool_name not in TOOL_NAMES:
            if _looks_like_write_tool(str(tool_name or "")):
                return _jsonrpc_error(
                    request_id,
                    -32003,
                    f"Mail MCP write tool blocked in read-only mode: {tool_name}",
                )
            return _jsonrpc_error(request_id, -32601, f"Unknown tool: {tool_name}")
        try:
            credential_ref = _credential_ref_from_request(request)
            result = _call_tool(str(tool_name), arguments, credential_ref=credential_ref)
        except GmailMcpError as exc:
            return _jsonrpc_error(request_id, -32001, str(exc))
        except httpx.HTTPError as exc:
            return _jsonrpc_error(request_id, -32002, f"Gmail API request failed: {exc.__class__.__name__}")
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    return _jsonrpc_error(request_id, -32601, f"Unknown method: {method}")


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=200,
    )


def _gmail_platform_configured() -> bool:
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")
    return all(value and not _is_placeholder(value) for value in (client_id, client_secret))


def _credential_ref_from_request(request: Request) -> str:
    return (
        request.headers.get("X-Credential-Ref-Gmail")
        or request.headers.get("X-Credential-Ref-Mail")
        or ""
    ).strip()


def _access_token(credential_ref: str = "") -> str:
    if credential_ref:
        return _access_token_from_vault(credential_ref)
    if not ALLOW_ENV_USER_TOKEN:
        raise GmailMcpError(
            "Gmail user credential ref is required. Connect Gmail for this user; "
            "GOOGLE_REFRESH_TOKEN/GOOGLE_ACCESS_TOKEN env fallback is disabled by default."
        )

    now = time.time()
    cached = str(_TOKEN_CACHE.get("access_token") or "")
    if cached and float(_TOKEN_CACHE.get("expires_at") or 0) > now + 60:
        return cached

    env_access_token = _env("GOOGLE_ACCESS_TOKEN")
    refresh_token = _env("GOOGLE_REFRESH_TOKEN")
    client_id = _env("GOOGLE_CLIENT_ID")
    client_secret = _env("GOOGLE_CLIENT_SECRET")

    can_refresh = all(
        value and not _is_placeholder(value)
        for value in (refresh_token, client_id, client_secret)
    )
    if can_refresh:
        return _refresh_access_token(refresh_token, client_id, client_secret)
    if env_access_token and not _is_placeholder(env_access_token):
        return env_access_token

    raise GmailMcpError(
        "Gmail env user token fallback is enabled but not configured. Set "
        "GOOGLE_REFRESH_TOKEN with GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET, or "
        "provide GOOGLE_ACCESS_TOKEN for local development only."
    )


def _access_token_from_vault(credential_ref: str) -> str:
    now = time.time()
    cached = _USER_TOKEN_CACHE.get(credential_ref) or {}
    access_token = str(cached.get("access_token") or "")
    if access_token and float(cached.get("expires_at") or 0) > now + 60:
        return access_token

    credential = _read_vault_credential(credential_ref)
    refresh_token = _first_credential_value(
        credential,
        "refresh_token",
        "google_refresh_token",
    )
    client_id = _first_credential_value(
        credential,
        "client_id",
        "google_client_id",
    )
    client_secret = _first_credential_value(
        credential,
        "client_secret",
        "google_client_secret",
    )
    direct_access_token = _first_credential_value(
        credential,
        "access_token",
        "google_access_token",
    )

    if (
        refresh_token
        and not _is_placeholder(refresh_token)
        and client_id
        and not _is_placeholder(client_id)
        and client_secret
        and not _is_placeholder(client_secret)
    ):
        token, expires_at = _request_refreshed_access_token(
            refresh_token,
            client_id,
            client_secret,
        )
        _USER_TOKEN_CACHE[credential_ref] = {
            "access_token": token,
            "expires_at": expires_at,
        }
        return token
    if direct_access_token and not _is_placeholder(direct_access_token):
        return direct_access_token
    raise GmailMcpError(
        "Gmail OAuth credential is incomplete. Store refresh_token plus "
        "client_id/client_secret in the user credential, or store access_token "
        "for a short-lived direct check."
    )


def _refresh_access_token(refresh_token: str, client_id: str, client_secret: str) -> str:
    access_token, expires_at = _request_refreshed_access_token(
        refresh_token,
        client_id,
        client_secret,
    )
    _TOKEN_CACHE["access_token"] = access_token
    _TOKEN_CACHE["expires_at"] = expires_at
    return access_token


def _request_refreshed_access_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> tuple[str, float]:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code >= 400:
        message = _error_message(response)
        raise GmailMcpError(f"Gmail OAuth token refresh failed: {message}")
    data = response.json()
    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise GmailMcpError("Gmail OAuth token refresh did not return access_token")
    expires_in = int(data.get("expires_in") or 3600)
    return access_token, time.time() + max(60, expires_in)


def _gmail_get(
    path: str,
    params: dict[str, Any] | list[tuple[str, Any]] | None = None,
    *,
    credential_ref: str = "",
) -> dict[str, Any]:
    token = _access_token(credential_ref)
    with httpx.Client(base_url=GMAIL_API_BASE_URL, timeout=REQUEST_TIMEOUT) as client:
        response = client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
    if response.status_code >= 400:
        raise GmailMcpError(f"Gmail API HTTP {response.status_code}: {_error_message(response)}")
    data = response.json()
    return data if isinstance(data, dict) else {}


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("status") or error)[:300]
        if error:
            return str(error)[:300]
    return response.text[:300]


def _call_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    credential_ref: str = "",
) -> dict[str, Any]:
    if not READ_ONLY:
        raise GmailMcpError("gmail-mcp is configured with MAIL_MCP_READ_ONLY=false")
    if tool_name not in {"gmail_list_drafts", "gmail_get_latest_draft"} and _looks_like_write_tool(tool_name):
        raise GmailMcpError(f"Mail MCP write tool blocked in read-only mode: {tool_name}")

    if tool_name == "gmail_list_messages":
        limit = _limit(arguments.get("limit"))
        items = _list_messages(limit=limit, credential_ref=credential_ref)
    elif tool_name == "gmail_list_unread_messages":
        limit = _limit(arguments.get("limit"))
        items = _list_messages(
            limit=limit,
            query="is:unread",
            label_ids=["UNREAD"],
            credential_ref=credential_ref,
        )
    elif tool_name == "gmail_search_messages":
        limit = _limit(arguments.get("limit"))
        query = _search_query(arguments)
        items = _list_messages(limit=limit, query=query, credential_ref=credential_ref)
    elif tool_name == "gmail_get_message":
        message_id = str(arguments.get("message_id") or arguments.get("id") or "").strip()
        include_body = bool(arguments.get("include_body", True))
        if not message_id:
            tool_name = "gmail_get_latest_message"
            items = [
                _get_latest_message(
                    arguments,
                    include_body=include_body,
                    credential_ref=credential_ref,
                )
            ]
        else:
            items = [
                _get_message(
                    message_id,
                    include_body=include_body,
                    credential_ref=credential_ref,
                )
            ]
    elif tool_name == "gmail_get_latest_message":
        include_body = bool(arguments.get("include_body", True))
        items = [
            _get_latest_message(
                arguments,
                include_body=include_body,
                credential_ref=credential_ref,
            )
        ]
    elif tool_name == "gmail_list_drafts":
        limit = _limit(arguments.get("limit"))
        items = _list_drafts(limit=limit, credential_ref=credential_ref)
    elif tool_name == "gmail_get_latest_draft":
        include_body = bool(arguments.get("include_body", True))
        items = [
            _get_latest_draft(
                arguments,
                include_body=include_body,
                credential_ref=credential_ref,
            )
        ]
    else:
        raise GmailMcpError(f"Unsupported Gmail tool: {tool_name}")

    return _mcp_result(tool_name, arguments, items)


def _limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 10
    return max(1, min(parsed, MAX_LIMIT))


def _search_query(arguments: dict[str, Any]) -> str:
    raw_query = str(arguments.get("query") or "").strip()
    parts = [raw_query] if raw_query else []
    sender = str(arguments.get("from") or "").strip()
    subject = str(arguments.get("subject") or "").strip()
    if sender and f"from:{sender}" not in raw_query:
        parts.append(f"from:{sender}")
    if subject and f"subject:{subject}" not in raw_query:
        parts.append(f"subject:{subject}")
    return " ".join(part for part in parts if part)


def _list_messages(
    *,
    limit: int,
    query: str = "",
    label_ids: list[str] | None = None,
    credential_ref: str = "",
) -> list[dict[str, Any]]:
    params: dict[str, Any] | list[tuple[str, Any]] = {"maxResults": limit}
    if query:
        params["q"] = query
    if label_ids:
        params = [(key, value) for key, value in params.items()]
        params.extend(("labelIds", label_id) for label_id in label_ids)
    listing = _gmail_get("/users/me/messages", params=params, credential_ref=credential_ref)
    message_refs = listing.get("messages") if isinstance(listing.get("messages"), list) else []
    items: list[dict[str, Any]] = []
    for ref in message_refs[:limit]:
        if not isinstance(ref, dict) or not ref.get("id"):
            continue
        items.append(
            _get_message(
                str(ref["id"]),
                include_body=False,
                credential_ref=credential_ref,
            )
        )
    return items


def _get_latest_message(
    arguments: dict[str, Any],
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    offset = _offset(arguments.get("offset"))
    query = _search_query(arguments)
    label_ids: list[str] | None = None
    if bool(arguments.get("inbox")):
        label_ids = ["INBOX"]
    if bool(arguments.get("unread")):
        query = " ".join(part for part in (query, "is:unread") if part)
        label_ids = [*(label_ids or []), "UNREAD"]
    candidates = _list_messages(
        limit=max(offset, min(MAX_LIMIT, 10)),
        query=query,
        label_ids=label_ids,
        credential_ref=credential_ref,
    )
    candidates = _sort_messages_newest(candidates)
    if len(candidates) < offset:
        raise GmailMcpError("No Gmail message matched the latest-message request")
    message_id = str(candidates[offset - 1].get("id") or "")
    if not message_id:
        raise GmailMcpError("Latest Gmail message did not include an id")
    return _get_message(
        message_id,
        include_body=include_body,
        credential_ref=credential_ref,
    )


def _list_drafts(
    *,
    limit: int,
    credential_ref: str = "",
) -> list[dict[str, Any]]:
    listing = _gmail_get(
        "/users/me/drafts",
        params={"maxResults": limit},
        credential_ref=credential_ref,
    )
    draft_refs = listing.get("drafts") if isinstance(listing.get("drafts"), list) else []
    items: list[dict[str, Any]] = []
    for ref in draft_refs[:limit]:
        if not isinstance(ref, dict):
            continue
        draft_id = str(ref.get("id") or "")
        if not draft_id:
            continue
        items.append(
            _get_draft(
                draft_id,
                include_body=False,
                credential_ref=credential_ref,
            )
        )
    return _sort_messages_newest(items)


def _get_latest_draft(
    arguments: dict[str, Any],
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    offset = _offset(arguments.get("offset"))
    candidates = _list_drafts(
        limit=max(offset, min(MAX_LIMIT, 10)),
        credential_ref=credential_ref,
    )
    candidates = _sort_messages_newest(candidates)
    if len(candidates) < offset:
        raise GmailMcpError("No Gmail draft matched the latest-draft request")
    draft_id = str(candidates[offset - 1].get("draft_id") or "")
    if not draft_id:
        raise GmailMcpError("Latest Gmail draft did not include a draft id")
    return _get_draft(
        draft_id,
        include_body=include_body,
        credential_ref=credential_ref,
    )


def _get_draft(
    draft_id: str,
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    draft = _gmail_get(
        f"/users/me/drafts/{draft_id}",
        params={"format": "full"} if include_body else [
            ("format", "metadata"),
            ("metadataHeaders", "Subject"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "To"),
            ("metadataHeaders", "Date"),
        ],
        credential_ref=credential_ref,
    )
    message = draft.get("message") if isinstance(draft.get("message"), dict) else {}
    normalised = _normalise_message(message, include_body=include_body)
    normalised["draft_id"] = str(draft.get("id") or draft_id)
    normalised["is_draft"] = True
    return normalised


def _offset(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 1
    return max(1, min(parsed, MAX_LIMIT))


def _get_message(
    message_id: str,
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    if include_body:
        params: dict[str, Any] | list[tuple[str, Any]] = {"format": "full"}
    else:
        params = [
            ("format", "metadata"),
            ("metadataHeaders", "Subject"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "To"),
            ("metadataHeaders", "Date"),
        ]
    message = _gmail_get(
        f"/users/me/messages/{message_id}",
        params=params,
        credential_ref=credential_ref,
    )
    return _normalise_message(message, include_body=include_body)


def _read_vault_credential(credential_ref: str) -> dict[str, str]:
    relative = _vault_relative_path(credential_ref)
    if VAULT_BACKEND == "local-dev":
        return _read_local_dev_credential(credential_ref)
    if not VAULT_ADDR or not VAULT_TOKEN:
        raise GmailMcpError(
            "Gmail credential ref was provided, but VAULT_ADDR/VAULT_TOKEN "
            "are not configured on gmail-mcp."
        )
    url = f"{VAULT_ADDR}/v1/{VAULT_KV_MOUNT}/data/{relative}"
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(url, headers={"X-Vault-Token": VAULT_TOKEN})
    if response.status_code == 404:
        raise GmailMcpError("Gmail OAuth credential was not found in Vault")
    if response.status_code >= 400:
        raise GmailMcpError(f"Vault read failed: HTTP {response.status_code}")
    payload = response.json()
    data = payload.get("data", {}).get("data", {})
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _read_local_dev_credential(credential_ref: str) -> dict[str, str]:
    global _VAULT_CLIENT
    if _VAULT_CLIENT is not None:
        class _CredentialPath:
            raw = credential_ref

        try:
            data = _VAULT_CLIENT.read(_CredentialPath())
        except KeyError as exc:
            raise GmailMcpError("Gmail OAuth credential was not found in Vault") from exc
        except Exception as exc:  # noqa: BLE001
            raise GmailMcpError(f"Vault read failed: {type(exc).__name__}") from exc
        return {str(key): str(value) for key, value in data.items()}

    try:
        from vault_client import VaultPath, make_client  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise GmailMcpError(
            "Gmail local-dev Vault backend is selected, but vault_client is "
            "not installed in the gmail-mcp image."
        ) from exc

    try:
        if _VAULT_CLIENT is None:
            _VAULT_CLIENT = make_client(os.environ)
        data = _VAULT_CLIENT.read(VaultPath.parse(credential_ref))
    except KeyError as exc:
        raise GmailMcpError("Gmail OAuth credential was not found in Vault") from exc
    except Exception as exc:  # noqa: BLE001
        raise GmailMcpError(f"Vault read failed: {type(exc).__name__}") from exc
    return {str(key): str(value) for key, value in data.items()}


def _vault_relative_path(credential_ref: str) -> str:
    if not re.fullmatch(r"vault:atlassian/_user_session/[a-zA-Z0-9_-]+/gmail", credential_ref):
        raise GmailMcpError("Invalid Gmail credential ref")
    return credential_ref[len("vault:") :]


def _first_credential_value(credential: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(credential.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalise_message(message: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = _headers_by_name(payload.get("headers"))
    body = _safe_mail_text(_extract_body(payload), BODY_CHAR_LIMIT) if include_body else ""
    return {
        "id": str(message.get("id") or ""),
        "subject": _safe_mail_text(headers.get("subject", ""), HEADER_CHAR_LIMIT),
        "from": _safe_mail_text(headers.get("from", ""), HEADER_CHAR_LIMIT),
        "to": _safe_mail_text(headers.get("to", ""), HEADER_CHAR_LIMIT),
        "date": _safe_mail_text(headers.get("date", ""), HEADER_CHAR_LIMIT),
        "internal_date": _safe_mail_text(message.get("internalDate"), HEADER_CHAR_LIMIT),
        "snippet": _safe_mail_text(message.get("snippet"), SNIPPET_CHAR_LIMIT),
        "body": body,
    }


def _sort_messages_newest(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=_message_sort_key, reverse=True)


def _message_sort_key(item: dict[str, Any]) -> float:
    raw_internal = str(item.get("internal_date") or "").strip()
    if raw_internal.isdigit():
        return float(raw_internal)
    raw_date = str(item.get("date") or "").strip()
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date).timestamp() * 1000
        except Exception:  # noqa: BLE001
            return 0.0
    return 0.0


def _headers_by_name(raw_headers: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(raw_headers, list):
        return headers
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        if name in {"subject", "from", "to", "date"}:
            headers[name] = str(item.get("value") or "")
    return headers


def _extract_body(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    _collect_body_parts(payload, plain_parts=plain_parts, html_parts=html_parts)
    if plain_parts:
        return _clean_body("\n\n".join(plain_parts))
    if html_parts:
        return _clean_body(_strip_html("\n\n".join(html_parts)))
    return ""


def _collect_body_parts(
    part: dict[str, Any],
    *,
    plain_parts: list[str],
    html_parts: list[str],
) -> None:
    mime_type = str(part.get("mimeType") or "").lower()
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = str(body.get("data") or "")
    if data and mime_type in {"text/plain", "text/html"}:
        decoded = _decode_base64url(data)
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        else:
            html_parts.append(decoded)
    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _collect_body_parts(child, plain_parts=plain_parts, html_parts=html_parts)


def _decode_base64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode(
            "utf-8",
            errors="replace",
        )
    except Exception:  # noqa: BLE001
        return ""


def _strip_html(value: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return html.unescape(text)


def _clean_body(value: str) -> str:
    cleaned = re.sub(r"[ \t]+", " ", value)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _looks_like_write_tool(tool_name: str) -> bool:
    tokens = {token for token in re.split(r"[^a-z0-9]+", tool_name.lower()) if token}
    return bool(tokens & _WRITE_TOOL_TOKENS)


def _safe_mail_text(value: Any, limit: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redaction_replacement, text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ...[truncated]"


def _redaction_replacement(match: re.Match[str]) -> str:
    first_group = match.group(1) if match.groups() else ""
    if first_group:
        return f"{first_group}=[REDACTED_SECRET]"
    if match.group(0).lower().startswith("bearer "):
        return "Bearer [REDACTED_SECRET]"
    return "[REDACTED_SECRET]"


def _mcp_result(
    tool_name: str,
    arguments: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    count = len(items)
    text = f"Gmail read-only query completed. {count} item(s) returned."
    if count == 1 and tool_name in {
        "gmail_get_message",
        "gmail_get_latest_message",
        "gmail_get_latest_draft",
    }:
        text = _mail_detail_text("Gmail", items[0])
    return {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ],
        "structuredContent": {
            "provider": PROVIDER,
            "tool": tool_name,
            "arguments": arguments,
            "items": items,
            "read_only": True,
            "scope": READONLY_SCOPE,
            "ai_hints": _ai_hints(tool_name, items),
        },
    }


def _mail_detail_text(provider: str, item: dict[str, Any]) -> str:
    body = str(item.get("body") or item.get("snippet") or "").strip()
    lines = [
        f"{provider} message detail",
        f"Subject: {item.get('subject') or '(no subject)'}",
        f"From: {item.get('from') or '(unknown)'}",
        f"To: {item.get('to') or '(unknown)'}",
        f"Date: {item.get('date') or '(unknown)'}",
        "",
        body,
    ]
    return "\n".join(lines).strip()


def _ai_hints(tool_name: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    if tool_name not in {
        "gmail_get_message",
        "gmail_get_latest_message",
        "gmail_get_latest_draft",
    } or not items:
        return {
            "next_step": "Use gmail_get_message with an item id when the user asks for details.",
        }
    return {
        "next_step": (
            "Answer the user directly from this message. Include sender, date, "
            "subject, a concise summary, important details, and suggested follow-up actions."
        ),
        "requires_message_id_from_user": False,
    }
