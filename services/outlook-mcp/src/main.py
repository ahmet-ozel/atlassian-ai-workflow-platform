"""Read-only Outlook MCP service.

This service talks to Microsoft Graph directly. In shared deployments it
expects a per-user Vault credential ref for mailbox access; service-local env
user tokens are a disabled local-dev fallback. Only read-only tools are
registered.
"""

from __future__ import annotations

import html
import os
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


PROVIDER = os.environ.get("MAIL_MCP_PROVIDER", "outlook").strip().lower() or "outlook"
READ_ONLY = os.environ.get("MAIL_MCP_READ_ONLY", "true").strip().lower() != "false"
OAUTH_MODEL = os.environ.get("MAIL_MCP_OAUTH_MODEL", "per_user_vault").strip()
ALLOW_ENV_USER_TOKEN = (
    os.environ.get("MAIL_MCP_ALLOW_ENV_USER_TOKEN", "false").strip().lower()
    in {"1", "true", "yes"}
)
GRAPH_SCOPES = os.environ.get("MICROSOFT_SCOPES", "offline_access Mail.Read").strip()
TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID", "common").strip() or "common"
TOKEN_URL = (
    os.environ.get("MICROSOFT_TOKEN_URI")
    or f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
).strip()
GRAPH_API_BASE_URL = os.environ.get(
    "MICROSOFT_GRAPH_API_BASE_URL",
    "https://graph.microsoft.com/v1.0",
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


class OutlookMcpError(RuntimeError):
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
        "outlook_list_messages",
        "List recent Outlook messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "outlook_list_unread_messages",
        "List unread Outlook messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "outlook_search_messages",
        "Search Outlook messages by query, sender, or subject.",
        {
            "query": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
        },
    ),
    _tool(
        "outlook_get_message",
        "Get one Outlook message by id.",
        {
            "message_id": {"type": "string"},
            "id": {"type": "string"},
            "include_body": {"type": "boolean"},
        },
    ),
    _tool(
        "outlook_get_latest_message",
        "Get the latest Outlook message with full details without requiring the caller to know its id.",
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
        "outlook_list_drafts",
        "List Outlook drafts without sending or modifying anything.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}},
    ),
    _tool(
        "outlook_get_latest_draft",
        "Get the latest Outlook draft with full details without sending or modifying anything.",
        {
            "offset": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT},
            "include_body": {"type": "boolean"},
        },
    ),
]

TOOL_NAMES = {tool["name"] for tool in TOOLS}


app = FastAPI(title="outlook-mcp", version="0.2.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "provider": PROVIDER,
        "read_only": READ_ONLY,
        "per_user_vault_enabled": OAUTH_MODEL == "per_user_vault",
        "platform_oauth_client_configured": _graph_platform_configured(),
        "env_user_token_enabled": ALLOW_ENV_USER_TOKEN,
        "oauth_model": OAUTH_MODEL,
        "token_owner": "outlook-mcp",
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
        except OutlookMcpError as exc:
            return _jsonrpc_error(request_id, -32001, str(exc))
        except httpx.HTTPError as exc:
            return _jsonrpc_error(
                request_id,
                -32002,
                f"Microsoft Graph request failed: {exc.__class__.__name__}",
            )
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

    return _jsonrpc_error(request_id, -32601, f"Unknown method: {method}")


def _jsonrpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=200,
    )


def _graph_platform_configured() -> bool:
    client_id = _env("MICROSOFT_CLIENT_ID")
    client_secret = _env("MICROSOFT_CLIENT_SECRET")
    return all(value and not _is_placeholder(value) for value in (client_id, client_secret))


def _credential_ref_from_request(request: Request) -> str:
    return (
        request.headers.get("X-Credential-Ref-Outlook")
        or request.headers.get("X-Credential-Ref-Mail")
        or ""
    ).strip()


def _access_token(credential_ref: str = "") -> str:
    if credential_ref:
        return _access_token_from_vault(credential_ref)
    if not ALLOW_ENV_USER_TOKEN:
        raise OutlookMcpError(
            "Outlook user credential ref is required. Connect Outlook for this user; "
            "MICROSOFT_REFRESH_TOKEN/MICROSOFT_ACCESS_TOKEN env fallback is disabled by default."
        )

    now = time.time()
    cached = str(_TOKEN_CACHE.get("access_token") or "")
    if cached and float(_TOKEN_CACHE.get("expires_at") or 0) > now + 60:
        return cached

    env_access_token = _env("MICROSOFT_ACCESS_TOKEN")
    refresh_token = _env("MICROSOFT_REFRESH_TOKEN")
    client_id = _env("MICROSOFT_CLIENT_ID")
    client_secret = _env("MICROSOFT_CLIENT_SECRET")

    can_refresh = all(
        value and not _is_placeholder(value)
        for value in (refresh_token, client_id, client_secret)
    )
    if can_refresh:
        return _refresh_access_token(refresh_token, client_id, client_secret)
    if env_access_token and not _is_placeholder(env_access_token):
        return env_access_token

    raise OutlookMcpError(
        "Microsoft Graph env user token fallback is enabled but not configured. Set "
        "MICROSOFT_REFRESH_TOKEN with MICROSOFT_CLIENT_ID/MICROSOFT_CLIENT_SECRET, "
        "or provide MICROSOFT_ACCESS_TOKEN for local development only."
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
        "microsoft_refresh_token",
    )
    client_id = _first_credential_value(
        credential,
        "client_id",
        "microsoft_client_id",
    )
    client_secret = _first_credential_value(
        credential,
        "client_secret",
        "microsoft_client_secret",
    )
    scopes = _first_credential_value(credential, "scopes", "scope", "microsoft_scopes")
    direct_access_token = _first_credential_value(
        credential,
        "access_token",
        "microsoft_access_token",
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
            scopes=scopes or GRAPH_SCOPES,
        )
        _USER_TOKEN_CACHE[credential_ref] = {
            "access_token": token,
            "expires_at": expires_at,
        }
        return token
    if direct_access_token and not _is_placeholder(direct_access_token):
        return direct_access_token
    raise OutlookMcpError(
        "Microsoft Graph OAuth credential is incomplete. Store refresh_token "
        "plus client_id/client_secret in the user credential, or store "
        "access_token for a short-lived direct check."
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
    *,
    scopes: str = GRAPH_SCOPES,
) -> tuple[str, float]:
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": scopes,
            },
        )
    if response.status_code >= 400:
        message = _error_message(response)
        raise OutlookMcpError(f"Microsoft OAuth token refresh failed: {message}")
    data = response.json()
    access_token = str(data.get("access_token") or "")
    if not access_token:
        raise OutlookMcpError("Microsoft OAuth token refresh did not return access_token")
    expires_in = int(data.get("expires_in") or 3600)
    return access_token, time.time() + max(60, expires_in)


def _graph_get(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    credential_ref: str = "",
) -> dict[str, Any]:
    token = _access_token(credential_ref)
    with httpx.Client(base_url=GRAPH_API_BASE_URL, timeout=REQUEST_TIMEOUT) as client:
        response = client.get(
            path,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "ConsistencyLevel": "eventual",
                "Prefer": 'outlook.body-content-type="text"',
            },
        )
    if response.status_code >= 400:
        raise OutlookMcpError(
            f"Microsoft Graph HTTP {response.status_code}: {_error_message(response)}"
        )
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
            return str(error.get("message") or error.get("code") or error)[:300]
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
        raise OutlookMcpError("outlook-mcp is configured with MAIL_MCP_READ_ONLY=false")
    if tool_name not in {"outlook_list_drafts", "outlook_get_latest_draft"} and _looks_like_write_tool(tool_name):
        raise OutlookMcpError(f"Mail MCP write tool blocked in read-only mode: {tool_name}")

    if tool_name == "outlook_list_messages":
        limit = _limit(arguments.get("limit"))
        items = _list_messages(limit=limit, credential_ref=credential_ref)
    elif tool_name == "outlook_list_unread_messages":
        limit = _limit(arguments.get("limit"))
        items = _list_messages(
            limit=limit,
            filter_expr="isRead eq false",
            credential_ref=credential_ref,
        )
    elif tool_name == "outlook_search_messages":
        limit = _limit(arguments.get("limit"))
        query = _search_query(arguments)
        items = _list_messages(limit=limit, search=query, credential_ref=credential_ref)
    elif tool_name == "outlook_get_message":
        message_id = str(arguments.get("message_id") or arguments.get("id") or "").strip()
        include_body = bool(arguments.get("include_body", True))
        if not message_id:
            tool_name = "outlook_get_latest_message"
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
    elif tool_name == "outlook_get_latest_message":
        include_body = bool(arguments.get("include_body", True))
        items = [
            _get_latest_message(
                arguments,
                include_body=include_body,
                credential_ref=credential_ref,
            )
        ]
    elif tool_name == "outlook_list_drafts":
        limit = _limit(arguments.get("limit"))
        items = _list_messages(limit=limit, folder="drafts", credential_ref=credential_ref)
        for item in items:
            item["is_draft"] = True
    elif tool_name == "outlook_get_latest_draft":
        include_body = bool(arguments.get("include_body", True))
        items = [
            _get_latest_draft(
                arguments,
                include_body=include_body,
                credential_ref=credential_ref,
            )
        ]
    else:
        raise OutlookMcpError(f"Unsupported Outlook tool: {tool_name}")

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
    search: str = "",
    filter_expr: str = "",
    inbox: bool = False,
    folder: str = "",
    credential_ref: str = "",
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "$top": limit,
        "$orderby": "receivedDateTime desc",
        "$select": "id,subject,from,toRecipients,receivedDateTime,bodyPreview",
    }
    if filter_expr:
        params["$filter"] = filter_expr
    if search:
        params.pop("$orderby", None)
        params["$search"] = f'"{search}"'

    if folder:
        path = f"/me/mailFolders/{folder}/messages"
    elif inbox:
        path = "/me/mailFolders/inbox/messages"
    else:
        path = "/me/messages"
    listing = _graph_get(path, params=params, credential_ref=credential_ref)
    raw_items = listing.get("value") if isinstance(listing.get("value"), list) else []
    return _sort_messages_newest(
        [_normalise_message(item, include_body=False) for item in raw_items[:limit]]
    )


def _get_latest_message(
    arguments: dict[str, Any],
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    offset = _offset(arguments.get("offset"))
    search = _search_query(arguments)
    inbox = bool(arguments.get("inbox"))
    filter_expr = "isRead eq false" if bool(arguments.get("unread")) else ""
    candidates = _list_messages(
        limit=max(offset, min(MAX_LIMIT, 10)),
        search=search,
        filter_expr=filter_expr,
        inbox=inbox,
        credential_ref=credential_ref,
    )
    candidates = _sort_messages_newest(candidates)
    if len(candidates) < offset:
        raise OutlookMcpError("No Outlook message matched the latest-message request")
    message_id = str(candidates[offset - 1].get("id") or "")
    if not message_id:
        raise OutlookMcpError("Latest Outlook message did not include an id")
    return _get_message(
        message_id,
        include_body=include_body,
        credential_ref=credential_ref,
    )


def _get_latest_draft(
    arguments: dict[str, Any],
    *,
    include_body: bool,
    credential_ref: str = "",
) -> dict[str, Any]:
    offset = _offset(arguments.get("offset"))
    candidates = _list_messages(
        limit=max(offset, min(MAX_LIMIT, 10)),
        folder="drafts",
        credential_ref=credential_ref,
    )
    candidates = _sort_messages_newest(candidates)
    if len(candidates) < offset:
        raise OutlookMcpError("No Outlook draft matched the latest-draft request")
    message_id = str(candidates[offset - 1].get("id") or "")
    if not message_id:
        raise OutlookMcpError("Latest Outlook draft did not include an id")
    draft = _get_message(
        message_id,
        include_body=include_body,
        credential_ref=credential_ref,
    )
    draft["is_draft"] = True
    return draft


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
    select = "id,subject,from,toRecipients,receivedDateTime,bodyPreview"
    if include_body:
        select += ",body"
    message = _graph_get(
        f"/me/messages/{message_id}",
        params={"$select": select},
        credential_ref=credential_ref,
    )
    return _normalise_message(message, include_body=include_body)


def _read_vault_credential(credential_ref: str) -> dict[str, str]:
    relative = _vault_relative_path(credential_ref)
    if VAULT_BACKEND == "local-dev":
        return _read_local_dev_credential(credential_ref)
    if not VAULT_ADDR or not VAULT_TOKEN:
        raise OutlookMcpError(
            "Outlook credential ref was provided, but VAULT_ADDR/VAULT_TOKEN "
            "are not configured on outlook-mcp."
        )
    url = f"{VAULT_ADDR}/v1/{VAULT_KV_MOUNT}/data/{relative}"
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        response = client.get(url, headers={"X-Vault-Token": VAULT_TOKEN})
    if response.status_code == 404:
        raise OutlookMcpError("Outlook OAuth credential was not found in Vault")
    if response.status_code >= 400:
        raise OutlookMcpError(f"Vault read failed: HTTP {response.status_code}")
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
            raise OutlookMcpError("Microsoft OAuth credential was not found in Vault") from exc
        except Exception as exc:  # noqa: BLE001
            raise OutlookMcpError(f"Vault read failed: {type(exc).__name__}") from exc
        return {str(key): str(value) for key, value in data.items()}

    try:
        from vault_client import VaultPath, make_client  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise OutlookMcpError(
            "Outlook local-dev Vault backend is selected, but vault_client is "
            "not installed in the outlook-mcp image."
        ) from exc

    try:
        if _VAULT_CLIENT is None:
            _VAULT_CLIENT = make_client(os.environ)
        data = _VAULT_CLIENT.read(VaultPath.parse(credential_ref))
    except KeyError as exc:
        raise OutlookMcpError("Microsoft OAuth credential was not found in Vault") from exc
    except Exception as exc:  # noqa: BLE001
        raise OutlookMcpError(f"Vault read failed: {type(exc).__name__}") from exc
    return {str(key): str(value) for key, value in data.items()}


def _vault_relative_path(credential_ref: str) -> str:
    if not re.fullmatch(r"vault:atlassian/_user_session/[a-zA-Z0-9_-]+/outlook", credential_ref):
        raise OutlookMcpError("Invalid Outlook credential ref")
    return credential_ref[len("vault:") :]


def _first_credential_value(credential: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(credential.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalise_message(message: dict[str, Any], *, include_body: bool) -> dict[str, Any]:
    from_address = _email_address(message.get("from"))
    to_addresses = [
        _email_address(recipient)
        for recipient in message.get("toRecipients") or []
        if isinstance(recipient, dict)
    ]
    body = ""
    if include_body:
        raw_body = message.get("body") if isinstance(message.get("body"), dict) else {}
        body = _safe_mail_text(_clean_body(_body_content(raw_body)), BODY_CHAR_LIMIT)
    return {
        "id": str(message.get("id") or ""),
        "subject": _safe_mail_text(message.get("subject"), HEADER_CHAR_LIMIT),
        "from": _safe_mail_text(from_address, HEADER_CHAR_LIMIT),
        "to": _safe_mail_text(", ".join(address for address in to_addresses if address), HEADER_CHAR_LIMIT),
        "date": _safe_mail_text(message.get("receivedDateTime"), HEADER_CHAR_LIMIT),
        "snippet": _safe_mail_text(message.get("bodyPreview"), SNIPPET_CHAR_LIMIT),
        "body": body,
    }


def _sort_messages_newest(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=_message_sort_key, reverse=True)


def _message_sort_key(item: dict[str, Any]) -> str:
    return str(item.get("date") or "")


def _email_address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    address = value.get("emailAddress")
    if not isinstance(address, dict):
        return ""
    email_value = str(address.get("address") or "")
    name_value = str(address.get("name") or "")
    if name_value and email_value:
        return f"{name_value} <{email_value}>"
    return email_value or name_value


def _body_content(body: dict[str, Any]) -> str:
    content = str(body.get("content") or "")
    content_type = str(body.get("contentType") or "").lower()
    if content_type == "html":
        return _strip_html(content)
    return content


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
    text = f"Outlook read-only query completed. {count} item(s) returned."
    if count == 1 and tool_name in {
        "outlook_get_message",
        "outlook_get_latest_message",
        "outlook_get_latest_draft",
    }:
        text = _mail_detail_text("Outlook", items[0])
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
            "scope": GRAPH_SCOPES,
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
        "outlook_get_message",
        "outlook_get_latest_message",
        "outlook_get_latest_draft",
    } or not items:
        return {
            "next_step": "Use outlook_get_message with an item id when the user asks for details.",
        }
    return {
        "next_step": (
            "Answer the user directly from this message. Include sender, date, "
            "subject, a concise summary, important details, and suggested follow-up actions."
        ),
        "requires_message_id_from_user": False,
    }
