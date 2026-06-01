"""Streamlit MCP client header/proxy contract tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_STREAMLIT_ROOT = Path(__file__).resolve().parents[2] / "ui" / "streamlit-app"
if str(_STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_ROOT))


def _load_module():
    path = _STREAMLIT_ROOT / "mcp_client.py"
    spec = importlib.util.spec_from_file_location("streamlit_mcp_client_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Cred:
    def __init__(self, vault_path: str) -> None:
        self.vault_path = vault_path


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _AsyncCaptureClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def post(self, path: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": "POST", "path": path, **kwargs})
        return _Resp(self.payload)

    async def get(self, path: str, **kwargs: Any) -> _Resp:
        self.calls.append({"method": "GET", "path": path, **kwargs})
        return _Resp(self.payload)


def test_direct_mcp_client_uses_jsonrpc_and_service_credential_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    monkeypatch.setattr(
        mod.st,
        "session_state",
        {
            "active_department_id": "engineering",
            "credential_bitbucket": _Cred("vault:atlassian/_user_session/s/bitbucket"),
        },
        raising=False,
    )
    fake = _AsyncCaptureClient({"jsonrpc": "2.0", "result": {"ok": True}})
    client = mod.MCPClient(base_url="http://mcp")
    client._client = fake

    asyncio.run(client.call_tool("bitbucket_list_pull_requests", {"repo": "x"}, service="bitbucket"))

    call = fake.calls[0]
    assert call["path"] == "/mcp"
    assert call["json"]["method"] == "tools/call"
    assert call["headers"]["X-Credential-Ref-Bitbucket"] == "vault:atlassian/_user_session/s/bitbucket"
    assert call["headers"]["X-Department-Id"] == "engineering"


def test_assistant_client_sends_all_bound_credential_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_module()
    monkeypatch.setattr(
        mod.st,
        "session_state",
        {
            "active_department_id": "engineering",
            "credential_jira": _Cred("vault:atlassian/_user_session/s/jira"),
            "credential_bitbucket": _Cred("vault:atlassian/_user_session/s/bitbucket"),
            "credential_confluence": _Cred("vault:atlassian/_user_session/s/confluence"),
        },
        raising=False,
    )
    fake = _AsyncCaptureClient({"reply": "ok"})
    client = mod.AssistantClient(base_url="http://assistant")
    client._client = fake

    asyncio.run(client.chat("hangi tasklar var?", dept_id="engineering"))

    headers = fake.calls[0]["headers"]
    assert headers["X-Credential-Ref-Jira"].endswith("/jira")
    assert headers["X-Credential-Ref-Bitbucket"].endswith("/bitbucket")
    assert headers["X-Credential-Ref-Confluence"].endswith("/confluence")
