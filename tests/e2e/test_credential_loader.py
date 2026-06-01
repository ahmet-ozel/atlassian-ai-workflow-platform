"""Unit tests for credential_loader module."""

import pytest
from pathlib import Path

from e2e.credential_loader import load_credentials, Credentials, CredentialParseError


CREDENTIALS_PATH = Path(__file__).resolve().parents[3] / "CREDENTIALS.md"


class TestLoadCredentials:
    """Tests for load_credentials function."""

    def test_parses_real_credentials_file(self):
        """Verify all fields are correctly parsed from the real CREDENTIALS.md."""
        creds = load_credentials(CREDENTIALS_PATH)

        assert isinstance(creds, Credentials)

        # Jira
        assert creds.jira_url == "https://example.atlassian.net"
        assert creds.jira_username == "user@example.com"
        assert creds.jira_api_token.startswith("ATATT3x")

        # Confluence
        assert creds.confluence_url == "https://example.atlassian.net/wiki"
        assert creds.confluence_username == "user@example.com"
        # Confluence uses same token as Jira
        assert creds.confluence_api_token == creds.jira_api_token

        # Bitbucket
        assert creds.bitbucket_workspace == "example_workspace"
        assert creds.bitbucket_repo == "smoke-test"
        assert creds.bitbucket_token_bearer.startswith("ATCTT3x")
        assert creds.bitbucket_token_basic.startswith("ATATT3x")
        assert creds.bitbucket_username == "user@example.com"

        # OpenAI
        assert creds.openai_api_key.startswith("sk-proj-")

        # SSH
        assert creds.ssh_host == "91.99.149.163"
        assert creds.ssh_user == "root"
        assert creds.ssh_key_path == "~/.ssh/id_ed25519"

    def test_missing_file_raises_error(self, tmp_path):
        """Verify clear error when file doesn't exist."""
        with pytest.raises(CredentialParseError, match="not found"):
            load_credentials(tmp_path / "NONEXISTENT.md")

    def test_empty_file_raises_error(self, tmp_path):
        """Verify clear error when file is empty."""
        empty_file = tmp_path / "CREDENTIALS.md"
        empty_file.write_text("")
        with pytest.raises(CredentialParseError, match="empty"):
            load_credentials(empty_file)

    def test_malformed_file_raises_error(self, tmp_path):
        """Verify clear error when file has no recognizable sections."""
        bad_file = tmp_path / "CREDENTIALS.md"
        bad_file.write_text("# Just a title\nNo tables here.\n")
        with pytest.raises(CredentialParseError, match="Section not found"):
            load_credentials(bad_file)

    def test_credentials_is_frozen(self):
        """Verify Credentials dataclass is immutable."""
        creds = load_credentials(CREDENTIALS_PATH)
        with pytest.raises(AttributeError):
            creds.jira_url = "http://changed.example.com"  # type: ignore

    def test_all_fields_are_non_empty_strings(self):
        """Verify no field is empty or whitespace-only."""
        creds = load_credentials(CREDENTIALS_PATH)
        for field_name in Credentials.__dataclass_fields__:
            value = getattr(creds, field_name)
            assert isinstance(value, str), f"{field_name} is not a string"
            assert value.strip(), f"{field_name} is empty or whitespace"
