"""Phase 8 static checks for Mail Chat / Mail MCP integration."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator


def test_mail_services_are_schema_valid_manifest_entries(repo_root: Path) -> None:
    schema = json.loads(
        (repo_root / "config" / "services.manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (repo_root / "config" / "services.manifest.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(schema).iter_errors(manifest))
    assert not errors, [error.message for error in errors]

    by_name = {entry["name"]: entry for entry in manifest["services"]}
    for name, port in (("gmail-mcp", "8110"), ("outlook-mcp", "8120")):
        assert name in by_name
        entry = by_name[name]
        assert entry["compose_service_name"] == name
        assert entry["compose_profile"] == name
        assert entry["env_example_path"].endswith(".env.example")
        assert (repo_root / entry["env_example_path"]).is_file()
        assert entry["health_endpoint"] == "/healthz"
        assert port in (entry.get("test_command") or "")


def test_compose_config_contains_mail_mcp_routes(repo_root: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available")

    result = subprocess.run(
        ["docker", "compose", "-f", "infra/docker-compose.yml", "config", "--quiet"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout

    compose_source = (repo_root / "infra" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "gmail-mcp:" in compose_source
    assert "outlook-mcp:" in compose_source
    assert "GMAIL_MCP_BASE_URL" in compose_source
    assert "OUTLOOK_MCP_BASE_URL" in compose_source
    assert "http://gmail-mcp:8110" in compose_source
    assert "http://outlook-mcp:8120" in compose_source
    assert "../services/gmail-mcp/.env" not in compose_source
    assert "../services/outlook-mcp/.env" not in compose_source
    for env_name in (
        "MAIL_MCP_OAUTH_MODEL",
        "MAIL_MCP_ALLOW_ENV_USER_TOKEN",
        "GOOGLE_TOKEN_URI",
        "GMAIL_API_BASE_URL",
        "MICROSOFT_GRAPH_API_BASE_URL",
        "VAULT_ADDR",
        "VAULT_TOKEN",
    ):
        assert env_name in compose_source
    for env_name in (
        "GOOGLE_CLIENT_ID:",
        "GOOGLE_CLIENT_SECRET:",
        "GOOGLE_REDIRECT_URI:",
        "GOOGLE_SCOPES:",
        "MICROSOFT_TENANT_ID:",
        "MICROSOFT_CLIENT_ID:",
        "MICROSOFT_CLIENT_SECRET:",
        "MICROSOFT_REDIRECT_URI:",
        "MICROSOFT_SCOPES:",
    ):
        assert env_name not in compose_source
    assert "GOOGLE_REFRESH_TOKEN:" not in compose_source
    assert "GOOGLE_ACCESS_TOKEN:" not in compose_source
    assert "MICROSOFT_REFRESH_TOKEN:" not in compose_source
    assert "MICROSOFT_ACCESS_TOKEN:" not in compose_source


def test_streamlit_mail_chat_smoke_contract(repo_root: Path) -> None:
    page_source = (repo_root / "ui/streamlit-app/pages/4_mail_chat.py").read_text(
        encoding="utf-8"
    )
    app_source = (repo_root / "ui/streamlit-app/app.py").read_text(encoding="utf-8")

    assert '"Mail Chat", "pages/4_mail_chat.py"' in app_source
    assert "mail_chat_history" in page_source
    assert "_assistant_mail_answer" in page_source
    assert "_asks_for_message_id" in page_source
    assert "_direct_mail_mcp_answer" in page_source
    assert 'mode="mail"' in page_source
    assert "plan_and_call_mail_mcp" in page_source
    assert "vault:atlassian/_user_session/{session_id}/{provider}" in page_source

    compose_source = (repo_root / "infra/docker-compose.yml").read_text(encoding="utf-8")
    streamlit_block = compose_source.split("  streamlit-ui:", 1)[1].split(
        "\n  task-intake-service:",
        1,
    )[0]
    assert "- path: ../.env" in streamlit_block
    assert "- path: ../.env.local" in streamlit_block
    assert "OPENAI_API_KEY:" not in streamlit_block

    assistant_block = compose_source.split("  assistant-service:", 1)[1].split(
        "\n  admin-dashboard-api:",
        1,
    )[0]
    assert "- path: ../.env" in assistant_block
    assert "- path: ../.env.local" in assistant_block
    assert "OPENAI_API_KEY:" not in assistant_block
    assert "st.session_state" in page_source
    assert not re.search(
        r"st\.session_state[^\n]*(access_token|refresh_token|gmail_token|outlook_token)",
        page_source,
        flags=re.IGNORECASE,
    )


def test_mail_env_examples_do_not_contain_real_secrets(repo_root: Path) -> None:
    suspicious_patterns = [
        re.compile(r"ya29\\.[A-Za-z0-9_-]{20,}"),
        re.compile(r"1//[A-Za-z0-9_-]{20,}"),
        re.compile(r"[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}\\.[A-Za-z0-9_-]{20,}"),
    ]
    for relative in (
        "services/gmail-mcp/.env.example",
        "services/outlook-mcp/.env.example",
    ):
        body = (repo_root / relative).read_text(encoding="utf-8")
        assert "MAIL_MCP_OAUTH_MODEL=per_user_vault" in body
        assert "MAIL_MCP_ALLOW_ENV_USER_TOKEN=false" in body
        assert "changeme" not in body.lower()
        for pattern in suspicious_patterns:
            assert not pattern.search(body), f"{relative} looks like it contains a real secret"


def test_mail_secret_material_is_not_committed(repo_root: Path) -> None:
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in gitignore
    assert "!**/.env.example" in gitignore
    for relative in ("services/gmail-mcp/.env", "services/outlook-mcp/.env"):
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0, f"{relative} must not be tracked"


def test_mail_body_and_secret_redaction_guards_exist(repo_root: Path) -> None:
    checked_sources = [
        repo_root / "services/gmail-mcp/src/main.py",
        repo_root / "services/outlook-mcp/src/main.py",
        repo_root / "ui/streamlit-app/mail_llm.py",
    ]

    for path in checked_sources:
        body = path.read_text(encoding="utf-8")
        assert "BODY_CHAR_LIMIT" in body or "_BODY_CHAR_LIMIT" in body
        assert "REDACTED_SECRET" in body
        assert "...[truncated]" in body
        assert "access[_-]?token" in body
        assert "refresh[_-]?token" in body


def test_mail_write_tools_blocked_at_all_layers(repo_root: Path) -> None:
    ui_client = (repo_root / "ui/streamlit-app/mail_mcp.py").read_text(encoding="utf-8")
    assistant_dispatch = (
        repo_root / "services/assistant-service/src/mcp_tool_dispatch.py"
    ).read_text(encoding="utf-8")
    gmail_mcp = (repo_root / "services/gmail-mcp/src/main.py").read_text(encoding="utf-8")
    outlook_mcp = (repo_root / "services/outlook-mcp/src/main.py").read_text(encoding="utf-8")

    assert "_ensure_read_only_tool" in ui_client
    assert "_ensure_read_only_mail_tool" in assistant_dispatch
    for source in (gmail_mcp, outlook_mcp):
        assert "_looks_like_write_tool" in source
        assert "write tool blocked" in source
        for token in ("send", "delete", "archive", "reply", "move"):
            assert token in source


def test_mail_services_do_not_log_token_body_or_header_values(repo_root: Path) -> None:
    for relative in (
        "services/gmail-mcp/src/main.py",
        "services/outlook-mcp/src/main.py",
    ):
        body = (repo_root / relative).read_text(encoding="utf-8")
        assert "logging" not in body
        assert "logger" not in body.lower()
        assert "print(" not in body
        assert "response.request.headers" not in body
        assert "Authorization" in body
