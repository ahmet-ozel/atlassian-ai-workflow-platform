"""
Credential loader for E2E tests.

Parses credentials.md markdown file and provides typed access to all
credential values needed by the local E2E test suite.

Requirements: R5.2, R7.2, R9-R15
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Credentials:
    """Typed container for all credentials parsed from credentials.md."""

    # Jira
    jira_url: str
    jira_username: str
    jira_api_token: str

    # Confluence
    confluence_url: str
    confluence_username: str
    confluence_api_token: str

    # Bitbucket
    bitbucket_workspace: str
    bitbucket_repo: str
    bitbucket_token_bearer: str  # Token A - ATCTT3x...
    bitbucket_token_basic: str   # Token B - ATATT3x...
    bitbucket_username: str

    # OpenAI
    openai_api_key: str

    # SSH (VPS)
    ssh_host: str
    ssh_user: str
    ssh_key_path: str


class CredentialParseError(Exception):
    """Raised when credentials.md cannot be parsed or a value is missing."""


def _extract_table_value(content: str, section_pattern: str, key: str) -> str:
    """Extract a value from a markdown table row within a specific section.

    Looks for a table row like: | Key | `value` | or | Key | value |
    within the section matched by section_pattern.
    """
    # Find the section
    section_match = re.search(section_pattern, content, re.DOTALL)
    if not section_match:
        raise CredentialParseError(
            f"Section not found for pattern: {section_pattern!r}"
        )
    section_text = section_match.group(0)

    # Try backtick-wrapped value first: | Key | `value` ... |
    backtick_pattern = rf"\|\s*{re.escape(key)}\s*\|\s*`([^`]+)`"
    backtick_match = re.search(backtick_pattern, section_text)
    if backtick_match:
        return backtick_match.group(1).strip()

    # Fallback: plain value between pipes: | Key | value |
    plain_pattern = rf"\|\s*{re.escape(key)}\s*\|\s*([^|\n]+?)\s*\|"
    plain_match = re.search(plain_pattern, section_text)
    if plain_match:
        return plain_match.group(1).strip()

    raise CredentialParseError(
        f"Key {key!r} not found in section matching {section_pattern!r}"
    )


def _extract_env_value(content: str, env_key: str) -> str:
    """Extract a value from a ```env code block line like KEY=value."""
    pattern = rf"^{re.escape(env_key)}=(.+)$"
    match = re.search(pattern, content, re.MULTILINE)
    if not match:
        raise CredentialParseError(
            f"Environment variable {env_key!r} not found in code blocks"
        )
    return match.group(1).strip()


def load_credentials(path: Path) -> Credentials:
    """Parse credentials.md and return a populated Credentials dataclass.

    Args:
        path: Path to the credentials.md file.

    Returns:
        Credentials dataclass with all fields populated.

    Raises:
        CredentialParseError: If the file is missing, malformed, or
            required values cannot be extracted.
    """
    if not path.exists():
        raise CredentialParseError(f"credentials.md not found at: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CredentialParseError(f"Cannot read credentials.md: {exc}") from exc

    if not content.strip():
        raise CredentialParseError("credentials.md is empty")

    # --- Jira ---
    # Section starts with "## Jira" and ends at next "##"
    jira_section = r"## Jira[^\n]*\n(.*?)(?=\n## |\Z)"
    jira_url = _extract_table_value(content, jira_section, "URL")
    jira_username = _extract_table_value(content, jira_section, "Username")
    jira_api_token = _extract_table_value(content, jira_section, "API Token")

    # --- Confluence ---
    confluence_section = r"## Confluence[^\n]*\n(.*?)(?=\n## |\Z)"
    confluence_url = _extract_table_value(content, confluence_section, "URL")
    confluence_username = _extract_table_value(
        content, confluence_section, "Username"
    )
    # Confluence uses same token as Jira per the doc.
    # The field may say "Jira ile aynı token" - in that case use Jira's token.
    try:
        confluence_api_token = _extract_table_value(
            content, confluence_section, "API Token"
        )
        # If the value references Jira's token, use the actual Jira token
        if "jira" in confluence_api_token.lower() and "aynı" in confluence_api_token.lower():
            confluence_api_token = jira_api_token
    except CredentialParseError:
        confluence_api_token = jira_api_token

    # --- Bitbucket ---
    bitbucket_section = r"## Bitbucket[^\n]*\n(.*?)(?=\n## (?!#)|\Z)"
    bitbucket_workspace = _extract_table_value(
        content, bitbucket_section, "Workspace Slug"
    )
    bitbucket_repo = _extract_table_value(
        content, bitbucket_section, "Test Repo"
    )

    # Token 1: Bearer (Workspace Access Token) - ATCTT3x prefix
    token1_section = r"### Token 1[^\n]*\n(.*?)(?=\n### |\n## |\Z)"
    bitbucket_token_bearer = _extract_table_value(
        content, token1_section, "Token"
    )

    # Token 2: Basic Auth (Personal API Token) - ATATT3x prefix
    token2_section = r"### Token 2[^\n]*\n(.*?)(?=\n### |\n## |\Z)"
    bitbucket_token_basic = _extract_table_value(
        content, token2_section, "Token"
    )
    bitbucket_username = _extract_table_value(
        content, token2_section, "Username"
    )

    # --- OpenAI ---
    openai_section = r"## OpenAI[^\n]*\n(.*?)(?=\n## |\Z)"
    openai_api_key = _extract_table_value(content, openai_section, "API Key")

    # --- VPS / SSH ---
    vps_section = r"## VPS[^\n]*\n(.*?)(?=\n## |\Z)"
    ssh_host = _extract_table_value(content, vps_section, "IP (IPv4)")
    ssh_user = _extract_table_value(content, vps_section, "SSH User")
    ssh_key_path = _extract_table_value(content, vps_section, "SSH Key")

    return Credentials(
        jira_url=jira_url,
        jira_username=jira_username,
        jira_api_token=jira_api_token,
        confluence_url=confluence_url,
        confluence_username=confluence_username,
        confluence_api_token=confluence_api_token,
        bitbucket_workspace=bitbucket_workspace,
        bitbucket_repo=bitbucket_repo,
        bitbucket_token_bearer=bitbucket_token_bearer,
        bitbucket_token_basic=bitbucket_token_basic,
        bitbucket_username=bitbucket_username,
        openai_api_key=openai_api_key,
        ssh_host=ssh_host,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path,
    )
