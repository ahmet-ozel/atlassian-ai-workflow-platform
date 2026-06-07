"""Parity property test: ``mcp-credential-headers.md``  Python constants.

The doc at ``platform/docs/api-contracts/mcp-credential-headers.md`` is the
canonical contract for every header name a caller must set when talking to
the ``atlassian_mcp_bitbucket`` MCP service. The Python constants (Bitbucket auth
headers in ``services/atlassian_mcp_bitbucket/.../utils/environment.py``, Jira /
Confluence URL+token header pairs in ``.../servers/dependencies.py``, the
``X-Client-Source`` header in ``libs/mcp_client/.../atlassian_client.py``)
mirror the same strings.

This test asserts the two surfaces stay in sync. When you add or rename a
header you MUST update both files; the test fails fast on drift.

Implementation note
-------------------

We do **not** import the MCP server modules - they pull heavy runtime deps
(httpx, mcp). Instead we read the source files as text and do a regex scan
for the constant assignments. That keeps the test cheap (no network, no
DB) and lets it run inside the workspace-root pytest collection without
the per-service venv.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path resolution - find the platform/ root no matter where pytest is invoked.
# ---------------------------------------------------------------------------

_THIS = Path(__file__).resolve()
_PLATFORM_ROOT = _THIS.parents[2]  # platform/tests/property/<file>.py  platform/

_DOC_PATH = _PLATFORM_ROOT / "docs" / "api-contracts" / "mcp-credential-headers.md"
_BITBUCKET_ENV_PATH = (
    _PLATFORM_ROOT
    / "services"
    / "atlassian_mcp_bitbucket"
    / "src"
    / "mcp_atlassian"
    / "utils"
    / "environment.py"
)
_DEPENDENCIES_PATH = (
    _PLATFORM_ROOT
    / "services"
    / "atlassian_mcp_bitbucket"
    / "src"
    / "mcp_atlassian"
    / "servers"
    / "dependencies.py"
)
_MCP_CLIENT_PATH = (
    _PLATFORM_ROOT
    / "libs"
    / "mcp_client"
    / "src"
    / "mcp_client"
    / "atlassian_client.py"
)

# ---------------------------------------------------------------------------
# Header strings the doc lists (case-sensitive; the wire format is
# canonical). Adding a new header requires editing this set AND the doc
# AND the matching .py constant - three-way parity is intentional.
# ---------------------------------------------------------------------------

_DOCUMENTED_HEADERS: frozenset[str] = frozenset(
    {
        # §1 - Generic / cross-service
        "X-Client-Source",
        "Authorization",
        "X-Atlassian-Cloud-Id",
        # §2 - Jira
        "X-Atlassian-Jira-Url",
        "X-Atlassian-Jira-Personal-Token",
        "X-Atlassian-Jira-Username",
        "X-Atlassian-Jira-Api-Token",
        # §3 - Confluence
        "X-Atlassian-Confluence-Url",
        "X-Atlassian-Confluence-Personal-Token",
        "X-Atlassian-Confluence-Username",
        "X-Atlassian-Confluence-Api-Token",
        # §4 - Bitbucket
        "X-Atlassian-Bitbucket-Url",
        "X-Atlassian-Bitbucket-Personal-Token",
        "X-Atlassian-Bitbucket-Cloud-Access-Token",
        "X-Atlassian-Bitbucket-Username",
        "X-Atlassian-Bitbucket-App-Password",
    }
)


# ---------------------------------------------------------------------------
# Test suite
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def doc_text() -> str:
    """Return the canonical doc content. Skip if the file is missing."""

    if not _DOC_PATH.is_file():
        pytest.skip(f"canonical doc not found at {_DOC_PATH}")
    return _DOC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_source() -> str:
    """Return the Bitbucket env constants source. Skip when missing."""

    if not _BITBUCKET_ENV_PATH.is_file():
        pytest.skip(
            f"Bitbucket env constants source not found at {_BITBUCKET_ENV_PATH}"
        )
    return _BITBUCKET_ENV_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def deps_source() -> str:
    """Return the Jira/Confluence dependency-resolver source."""

    if not _DEPENDENCIES_PATH.is_file():
        pytest.skip(
            f"Jira/Confluence resolver source not found at {_DEPENDENCIES_PATH}"
        )
    return _DEPENDENCIES_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mcp_client_source() -> str:
    """Return the ``mcp_client.atlassian_client`` source for X-Client-Source."""

    if not _MCP_CLIENT_PATH.is_file():
        pytest.skip(f"mcp_client source not found at {_MCP_CLIENT_PATH}")
    return _MCP_CLIENT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Doc  contract assertions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("header", sorted(_DOCUMENTED_HEADERS))
def test_doc_mentions_each_documented_header(doc_text: str, header: str) -> None:
    """Every header in :data:`_DOCUMENTED_HEADERS` appears verbatim in the doc."""

    assert header in doc_text, (
        f"Header {header!r} is in _DOCUMENTED_HEADERS but is missing from "
        f"{_DOC_PATH.relative_to(_PLATFORM_ROOT)}. Add a row in the matching "
        f"section of the doc, or remove the entry from this test."
    )


def test_doc_does_not_invent_undocumented_atlassian_headers(doc_text: str) -> None:
    """Every ``X-Atlassian-*`` token in the doc is in :data:`_DOCUMENTED_HEADERS`.

    Catches typos / forgotten entries: if the doc body contains a header
    name that the parity test does not know about, the test fails so an
    operator either updates the test (intentional new header) or fixes
    the typo in the doc.
    """

    # Match canonical capitalisation only - strings inside code spans and
    # tables follow the wire format, not lowercased ASGI form.
    found = set(re.findall(r"X-Atlassian-[A-Za-z0-9-]+", doc_text))

    # Restrict to top-level header strings the doc actually advertises.
    # We exclude documentary mentions of constants in other modules
    # (e.g. ``BITBUCKET_URL_HEADER``) which aren't header names.
    unknown = found - _DOCUMENTED_HEADERS
    assert not unknown, (
        f"Doc references undocumented X-Atlassian-* headers: "
        f"{sorted(unknown)}. Either add them to _DOCUMENTED_HEADERS in this "
        f"test (and to environment.py / dependencies.py) or remove them from "
        f"the doc."
    )


# ---------------------------------------------------------------------------
# Doc  Bitbucket env constants parity (5 headers)
# ---------------------------------------------------------------------------


_BITBUCKET_HEADERS: frozenset[str] = frozenset(
    {
        "X-Atlassian-Bitbucket-Url",
        "X-Atlassian-Bitbucket-Personal-Token",
        "X-Atlassian-Bitbucket-Cloud-Access-Token",
        "X-Atlassian-Bitbucket-Username",
        "X-Atlassian-Bitbucket-App-Password",
    }
)


@pytest.mark.parametrize("header", sorted(_BITBUCKET_HEADERS))
def test_bitbucket_env_module_defines_each_header_constant(
    env_source: str, header: str
) -> None:
    """Each Bitbucket header string appears as a string literal inside
    ``environment.py``. The exact constant name (BITBUCKET_*_HEADER) is
    not asserted - only the wire-format string - so renaming the Python
    symbol stays a non-breaking refactor as long as the wire format is
    preserved."""

    assert header in env_source, (
        f"Header {header!r} is documented in {_DOC_PATH.relative_to(_PLATFORM_ROOT)} "
        f"but is missing from {_BITBUCKET_ENV_PATH.relative_to(_PLATFORM_ROOT)}. "
        f"Add the constant assignment so the per-request auth resolver picks "
        f"up the header."
    )


# ---------------------------------------------------------------------------
# Doc  Jira/Confluence dependency-resolver parity
# ---------------------------------------------------------------------------


_JIRA_CONFLUENCE_RESOLVED_HEADERS: frozenset[str] = frozenset(
    {
        "X-Atlassian-Jira-Url",
        "X-Atlassian-Jira-Personal-Token",
        "X-Atlassian-Confluence-Url",
        "X-Atlassian-Confluence-Personal-Token",
    }
)


@pytest.mark.parametrize("header", sorted(_JIRA_CONFLUENCE_RESOLVED_HEADERS))
def test_dependencies_module_consults_each_documented_header(
    deps_source: str, header: str
) -> None:
    """Each documented Jira/Confluence URL+token header appears in
    ``dependencies.py`` (where the resolver registers per-request auth
    fetchers)."""

    assert header in deps_source, (
        f"Header {header!r} is documented in {_DOC_PATH.relative_to(_PLATFORM_ROOT)} "
        f"but is missing from {_DEPENDENCIES_PATH.relative_to(_PLATFORM_ROOT)}. "
        f"The dependency layer cannot resolve the per-request auth without "
        f"reading the matching header."
    )


# ---------------------------------------------------------------------------
# Doc  mcp_client X-Client-Source parity
# ---------------------------------------------------------------------------


def test_mcp_client_defines_x_client_source_constant(
    mcp_client_source: str,
) -> None:
    """``mcp_client.AtlassianClient`` exposes ``CLIENT_SOURCE_HEADER`` set to
    ``X-Client-Source``."""

    assert 'CLIENT_SOURCE_HEADER: Final[str] = "X-Client-Source"' in mcp_client_source, (
        "mcp_client.atlassian_client must define "
        "CLIENT_SOURCE_HEADER = 'X-Client-Source' so callers and the doc "
        "agree on the canonical header name."
    )


def test_mcp_client_constructor_requires_client_source(
    mcp_client_source: str,
) -> None:
    """The constructor signature lists ``client_source`` as keyword-only +
    required (no default value). Drift from this contract reopens the
    caller-identification gap."""

    # The signature is split across multiple lines; we look for the
    # specific shape produced by the implementation: ``client_source: str,``
    # without a default value (no ``=`` between ``str`` and ``,``).
    # The pattern below tolerates whitespace but rejects defaults.
    pattern = re.compile(
        r"client_source\s*:\s*str\s*,",
        re.MULTILINE,
    )
    assert pattern.search(mcp_client_source), (
        "AtlassianClient.__init__ must declare client_source as required "
        "(no default value). This is an enforcement chokepoint - "
        "see platform/docs/api-contracts/mcp-credential-headers.md §1."
    )
