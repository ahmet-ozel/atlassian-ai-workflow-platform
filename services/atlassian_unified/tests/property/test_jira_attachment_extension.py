"""Property test: File extension validation for jira_add_attachment.

Feature: platform-completion, Property 8: For any file submitted to
jira_add_attachment, it SHALL be accepted iff its extension is in
{.pdf, .md, .csv, .txt, .json}; all other extensions SHALL be rejected
with "unsupported_format" error.

Validates: Requirements 4.5
"""
from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without installing the service package.
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from tools.jira_attachment import JiraAttachmentTool  # noqa: E402


_ALLOWED = {".pdf", ".md", ".csv", ".txt", ".json"}


def _make_tool() -> JiraAttachmentTool:
    return JiraAttachmentTool(
        jira_base_url="https://example.atlassian.net",
        jira_email="bot@example.com",
        jira_api_token="test",
    )


@settings(max_examples=200, deadline=None)
@given(extension=st.sampled_from([".pdf", ".md", ".csv", ".txt", ".json", ".PDF", ".MD"]))
def test_allowed_extensions_pass(extension: str) -> None:
    """Allowed extensions (any case) are accepted."""
    tool = _make_tool()
    err = tool._validate_extension(f"file{extension}")
    assert err is None, f"Allowed extension {extension!r} was rejected: {err}"


@settings(max_examples=300, deadline=None)
@given(extension=st.from_regex(r"\.[a-zA-Z]{1,5}", fullmatch=True))
def test_extension_classification(extension: str) -> None:
    """Validation result matches whether extension is in the allowed set."""
    tool = _make_tool()
    err = tool._validate_extension(f"file{extension}")
    if extension.lower() in _ALLOWED:
        assert err is None, (
            f"Allowed extension {extension!r} was rejected: {err}"
        )
    else:
        assert err is not None, (
            f"Disallowed extension {extension!r} was accepted"
        )
        assert "unsupported_format" in err, (
            f"Rejection message missing 'unsupported_format': {err}"
        )


@settings(max_examples=100, deadline=None)
@given(filename=st.text(min_size=1, max_size=50).filter(lambda s: "." not in s))
def test_no_extension_rejected(filename: str) -> None:
    """Filenames with no extension are rejected."""
    tool = _make_tool()
    err = tool._validate_extension(filename)
    assert err is not None, (
        f"File without extension {filename!r} was unexpectedly accepted"
    )
