import json
import os
import shutil
import subprocess

import pytest


def _build_probe_payload() -> str:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "homebrew-probe", "version": "1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    return "\n".join(json.dumps(message) for message in messages) + "\n"


@pytest.mark.skipif(
    shutil.which("uv") is None,
    reason="uv is not installed on this host; the homebrew probe test "
    "requires the uv launcher to start mcp-atlassian.",
)
def test_stdio_homebrew_probe_exits_after_stdin_close() -> None:
    env = os.environ.copy()
    env.update(
        {
            "JIRA_URL": "https://example.atlassian.net",
            "JIRA_USERNAME": "user@example.com",
            "JIRA_API_TOKEN": "x",
        }
    )

    result = subprocess.run(
        ["uv", "run", "mcp-atlassian"],
        input=_build_probe_payload(),
        capture_output=True,
        # FastMCP prints a Unicode banner on startup (box-drawing glyphs
        # like ▄▀█). Windows' default subprocess decoder is the active
        # ANSI code page (cp1252 in English locales, cp1254 on Turkish
        # installs, etc.) which cannot decode those code points, so
        # relying on ``text=True`` crashes the reader thread with a
        # ``UnicodeDecodeError`` on some hosts. Pin UTF-8 explicitly and
        # replace any still-bad bytes rather than raising — we only care
        # about the JSON-RPC lines, not the decorative output.
        encoding="utf-8",
        errors="replace",
        env=env,
        check=False,
        timeout=15,
    )

    combined_output = f"{result.stdout}\n{result.stderr}"
    jsonrpc_lines = [
        line for line in combined_output.splitlines() if line.startswith('{"jsonrpc"')
    ]

    assert result.returncode == 0, combined_output[:1000]
    assert any('"id":1' in line for line in jsonrpc_lines), combined_output[:1000]
    assert any('"id":2' in line and '"tools"' in line for line in jsonrpc_lines), (
        combined_output[:1000]
    )
